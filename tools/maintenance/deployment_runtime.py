"""Exact-runtime recreation and scoped Compose publication for voicemail.

Private inspect/config values never appear in errors. The caller owns the
durable rollout journal, idle boundary, backups, activation and recovery.
"""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import re
import socket
from urllib.parse import urlencode

from cutover_files import ConfigurationConflict, Publication, read_regular, move_absent
import quiesce_backup as q

LABEL = 'codex.voicemail.rollout'
ROLE = 'codex.voicemail.role'


class UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=30)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def api(method, path, body=None, expected=200):
    connection = UnixConnection()
    try:
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=payload,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read()
        if response.status != expected:
            raise RuntimeError('Docker API returned status ' + str(response.status))
        return json.loads(raw) if raw else None
    finally:
        connection.close()


def create(name, spec):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}', name):
        raise ValueError('Invalid container name')
    version = api('GET', '/version')['ApiVersion']
    if not re.fullmatch(r'1\.\d+', version):
        raise RuntimeError('Unexpected Docker API version')
    result = api('POST', '/v' + version + '/containers/create?' + urlencode({'name': name}),
                 {**spec['Config'], 'HostConfig': spec['HostConfig']}, expected=201)
    if not re.fullmatch(r'[a-f0-9]{64}', result.get('Id', '')):
        raise RuntimeError('Docker create did not return a container identity')
    # A caller can recover even if create succeeds just before it loses control:
    # the generation and role labels are part of the atomic create request.
    return result['Id']


def find(name):
    ids = q.run(['docker','ps','-aq','--filter','name=^/' + name + '$'], text=True).split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError('Ambiguous service identity')
    return q.inspect(ids[0])


def validate_template(original):
    host = original['HostConfig']
    if host['NetworkMode'] != 'host' or host['AutoRemove'] or host['Privileged']:
        raise RuntimeError('Only the existing unprivileged host-network service is supported')
    if host.get('PortBindings') or host.get('VolumesFrom') or host.get('Links'):
        raise RuntimeError('Unexpected port or container dependency configuration')
    mounts = original['Mounts']
    if (len(mounts) != 2 or {m['Destination'] for m in mounts} != {'/app/data','/app/logs'}
            or any(m['Type']!='bind' or not m['RW'] for m in mounts)):
        raise RuntimeError('Only the existing two writable bind mounts are supported')
    for mount in mounts:
        path = Path(mount['Source'])
        if not path.is_absolute() or path.resolve() != path:
            raise RuntimeError('Mount source is not a real absolute path')


def template(original, image, token, role, config_hash):
    validate_template(original)
    if (not re.fullmatch(r'sha256:[a-f0-9]{64}', image)
            or not re.fullmatch(r'[a-f0-9]{16}', token)
            or role not in ('candidate','original')
            or not re.fullmatch(r'[a-f0-9]{64}', config_hash)):
        raise ValueError('Invalid image, generation, role or Compose hash')
    result = {k:copy.deepcopy(original[k]) for k in ['Config','HostConfig','Mounts']}
    result['Config']['Image'] = image
    labels = result['Config'].setdefault('Labels', {})
    labels.update({LABEL:token, ROLE:role, 'com.docker.compose.image':image,
                   'com.docker.compose.config-hash':config_hash})
    result['HostConfig']['RestartPolicy'] = {'Name':'no','MaximumRetryCount':0}
    return result


def verify(actual, spec, image, restart=None):
    expected_host = copy.deepcopy(spec['HostConfig'])
    # Docker normalizes the unspecified OOM switch (null) to its default false
    # on create. Both retain the kernel OOM killer; true is never equivalent.
    if expected_host.get('OomKillDisable') is None and actual['HostConfig'].get('OomKillDisable') is False:
        expected_host['OomKillDisable']=False
    if restart is not None:
        expected_host['RestartPolicy'] = restart
    if actual['Image'] != image or actual['Config'] != spec['Config']:
        changed = sorted(k for k in set(actual['Config']) | set(spec['Config'])
                         if actual['Config'].get(k) != spec['Config'].get(k))
        raise RuntimeError('Runtime image/configuration mismatch; differing keys: ' + ','.join(changed))
    if actual['HostConfig'] != expected_host:
        changed = sorted(k for k in set(actual['HostConfig']) | set(expected_host)
                         if actual['HostConfig'].get(k) != expected_host.get(k))
        raise RuntimeError('Host configuration mismatch; differing keys: ' + ','.join(changed))
    by_destination = lambda mounts: sorted(mounts, key=lambda m:m['Destination'])
    if by_destination(actual['Mounts']) != by_destination(spec['Mounts']):
        raise RuntimeError('Runtime mounts changed')


def patch_image(raw, service, image, *, retained_reference=False):
    """Replace one simple image scalar; retain every unrelated byte/comment."""
    image_pattern = r'[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*' if retained_reference else r'sha256:[a-f0-9]{64}'
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', service) or not re.fullmatch(image_pattern,image):
        raise ValueError('Invalid service or exact image')
    text = raw.decode('utf-8')
    headers = list(re.finditer(r'^  '+re.escape(service)+r':[ \t]*(?:#[^\r\n]*)?\r?$',text,re.M))
    if len(headers) != 1:
        raise ConfigurationConflict('Require one plain two-space service definition')
    start = headers[0].end()
    next_header = re.search(r'^(?:[^ \r\n#]|  [^ \r\n#])',text[start:],re.M)
    end = start + next_header.start() if next_header else len(text)
    block = text[start:end]
    matches = list(re.finditer(r'^(    image:[ \t]*)([^\r\n]+)(\r?)$',block,re.M))
    if len(matches) != 1:
        raise ConfigurationConflict('Require one plain service image scalar')
    match = matches[0]
    # Rendering below rejects anchors, aliases, duplicate keys and all semantic
    # effects other than the selected image change. Preserve an inline comment.
    old_value = match[2]
    comment = re.search(r'[ \t]+#[^\r\n]*$',old_value)
    new_value = image + (comment.group() if comment else '')
    result = text[:start+match.start(2)] + new_value + text[start+match.end(2):]
    return result.encode('utf-8')


def compose_command(path, project):
    return ['docker','compose','--project-directory',str(Path(path).parent),
            '-p',project,'-f','-','config']


def rendered(raw, path, project, service):
    result = json.loads(q.run(compose_command(path, project)+['--format','json',service],
                              input=raw, stderr=__import__('subprocess').DEVNULL))
    if set(result.get('services',{})) != {service}:
        raise ConfigurationConflict('Unexpected service dependency in Compose rendering')
    return result


def image_edit(raw, path, project, service, image, expected_service):
    before = rendered(raw,path,project,service)
    if before['services'][service] != expected_service:
        raise ConfigurationConflict('Selected service configuration changed concurrently')
    desired = patch_image(raw, service, image)
    after = rendered(desired,path,project,service)
    expected = copy.deepcopy(before)
    expected['services'][service]['image'] = image
    if after != expected:
        raise ConfigurationConflict('Compose edit affected more than the selected image')
    hashed = q.run(compose_command(path,project)+['--hash',service],input=desired).decode().split()
    if len(hashed)!=2 or hashed[0]!=service or not re.fullmatch(r'[a-f0-9]{64}',hashed[1]):
        raise RuntimeError('Unexpected Compose configuration hash')
    return desired, hashed[1], after['services'][service]


def metadata(path):
    _, value = read_regular(path)
    return {'mode':value.st_mode & 0o7777,'uid':value.st_uid,'gid':value.st_gid,
            'acl':{k:base64.b64encode(os.getxattr(path,k)).decode()
                   for k in os.listxattr(path) if 'acl' in k.lower()}}


def publication(state, original, desired, suffix):
    m = state['metadata']
    return Publication(Path(state['path']),original,desired,state['token']+'-'+suffix,
                       m['mode'],m['uid'],m['gid'])


def publish(state, original, desired, suffix, hook=None):
    p = publication(state,original,desired,suffix)
    if p._read(p.canonical) == original and metadata(p.canonical) != state['metadata']:
        raise ConfigurationConflict('Configuration ownership, mode or ACL changed')
    def checkpoint(phase):
        if phase == 'candidate_file_ready':
            for key,value in state['metadata']['acl'].items():
                os.setxattr(p.pending,key,base64.b64decode(value))
            if metadata(p.pending) != state['metadata']:
                raise ConfigurationConflict('Replacement metadata differs')
        if hook:
            hook(phase)
    p.apply(checkpoint)
    if metadata(p.canonical) != state['metadata']:
        raise ConfigurationConflict('Published metadata differs')


def restore_document(state, original, desired):
    forward = publication(state,original,desired,'forward')
    current = forward._read(forward.canonical)
    if current == original:
        return
    if current is None and forward._read(forward.previous) == original:
        move_absent(forward.previous,forward.canonical)
        os.chmod(forward.canonical,state['metadata']['mode'])
        for key,value in state['metadata']['acl'].items():
            os.setxattr(forward.canonical,key,base64.b64decode(value))
        return
    publish(state,desired,original,'reverse')
