"""Single-service voicemail rollout with durable recovery and no database rewind.

Run only through the capacity guard and a systemd ExecStopPost recovery command.
Preparation is read-only to production. All private configuration, snapshots and
journals stay in new operator-owned directories; only sanitized receipts leave.
"""
import argparse
import copy
import datetime
import fcntl
import functools
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import deployment_runtime as d
import quiesce_backup as q
import quiescent_stop as stop
from cutover_files import Publication, ConfigurationConflict, write_new, read_regular
from ingress_fence import Fence, marked_http_status
from verify_restore import records

LOCK_PATH=Path('/run/lock/codex-voicemail-rollout.lock')


def exclusive_operation(function):
    @functools.wraps(function)
    def locked(*args,**kwargs):
        fd=os.open(LOCK_PATH,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            metadata=os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1
                    or metadata.st_uid!=os.geteuid() or metadata.st_mode & 0o077):
                raise RuntimeError('Unexpected operation lock ownership or mode')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Another voicemail operation is active') from None
            return function(*args,**kwargs)
        finally:os.close(fd)
    return locked


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def retain(path, raw):
    """Resume a complete private intent file; never replace differing bytes."""
    try:
        existing=read_regular(path)[0]
    except FileNotFoundError:
        write_new(path,raw)
        return
    if existing!=raw:
        raise ConfigurationConflict('Retained private intent changed')


def save(directory, state, phase=None, hook=None):
    if phase:
        state['phase'] = phase
    q.journal(directory, state)
    if hook and phase:
        hook(phase, state)


def fence(state):
    return Fence(state['token'],state['port'],state['mark'])


def ready(state):
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        try:
            if marked_http_status(state['port'],state['mark'])==200:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(.2)
    raise RuntimeError('Voicemail readiness did not pass')


def data_identity(data, logs):
    q.database_check(data/'state.db')
    return q.digest(json.dumps({'rows':records(data/'state.db'),
        'attachments':q.tree_manifest(logs/'attachments')},sort_keys=True).encode())


def original_inspect(directory):
    return json.loads(read_regular(directory/'container.private.json')[0])


def owned_current(directory, state):
    current = d.find(state['name'])
    if current is None:
        return None, None
    if current['Id']==state['original_id']:
        stop.owned_state(state['original_stop'])
        return current,'initial'
    labels = current['Config'].get('Labels') or {}
    role=labels.get(d.ROLE)
    if labels.get(d.LABEL)!=state['token'] or role not in ('candidate','original'):
        raise RuntimeError('Foreign service occupies the canonical container name')
    spec=state[role+'_spec']
    policy=current['HostConfig']['RestartPolicy']
    if policy not in [spec['HostConfig']['RestartPolicy'],state['restart_policy']]:
        raise RuntimeError('Concurrent restart policy change')
    d.verify(current,spec,state[role+'_image'],restart=policy)
    return current,role


def make_stop(directory, state, current, *, raw=None, metadata=None):
    if (directory/'journal.json').exists():
        child=q.load(directory)
        if child['container_id']!=current['Id'] or child['image']!=current['Image']:
            raise RuntimeError('Retained stop journal belongs to another container')
        stop.owned_state(child)
        return child
    directory.mkdir(mode=0o700,exist_ok=True)
    q.private_directory(directory)
    config=Path(state['data'])/'config.json'
    if raw is None:
        raw,stat=read_regular(config)
        metadata={'mode':stat.st_mode & 0o777,'uid':stat.st_uid,'gid':stat.st_gid}
        if d.metadata(config)['acl']:
            raise ConfigurationConflict('Application configuration ACL changed; preserve for review')
    stop.maintenance_config(raw,explicit=True)
    retained=directory/'original-config.json'
    if retained.exists():
        if read_regular(retained)[0]!=raw:
            raise ConfigurationConflict('Partial stop preparation differs from current configuration')
    else:write_new(retained,raw)
    child={'phase':'prepared','maintenance_version':1,'container_id':current['Id'],
        'image':current['Image'],'configuration_fingerprint':q.config_fingerprint(current),
        'configuration_path':str(config),'configuration_mode':metadata['mode'],
        'configuration_uid':metadata['uid'],'configuration_gid':metadata['gid'],
        'restart_policy':current['HostConfig']['RestartPolicy'],
        'token':'rollout-'+secrets.token_hex(8),'py_spy':state['py_spy'],
        'executor_wait_site':q.executor_wait_site(current['Id'])}
    q.journal(directory,child)
    return child


def freeze(directory, state, key, hook=None):
    child=state[key]; deadline=time.monotonic()+state['idle_timeout']
    while time.monotonic()<deadline:
        current=stop.owned_state(child)
        if not current['State']['Paused']:
            q.run(['docker','pause',child['container_id']])
        child['phase']='paused'; save(directory,state)
        if q.frozen_idle_sample(directory/key,child):
            save(directory,state)
            return
        q.run(['docker','unpause',child['container_id']])
        child['phase']='prepared'; save(directory,state)
        time.sleep(.3)
    raise RuntimeError('No verified idle boundary; no worker was force-killed')


def check_configuration_metadata(directory,child):
    expected={'mode':child['configuration_mode'],'uid':child['configuration_uid'],
              'gid':child['configuration_gid'],'acl':{}}
    try:actual=d.metadata(Path(child['configuration_path']))
    except FileNotFoundError:
        publication=stop.publication(directory,child)
        if publication._read(publication.previous)==publication.original:
            return
        raise ConfigurationConflict('Missing application configuration is not an owned publication') from None
    if actual!=expected:
        raise ConfigurationConflict('Application configuration ownership, mode or ACL changed')


def stop_service(directory,state,key,hook=None):
    child=state[key]
    def checkpoint(phase):
        child['phase']=phase
        save(directory,state,key+'_'+phase,hook)
    current=stop.owned_state(child)
    if not current['State']['Running']:
        stop.stop_idle(directory/key,child,checkpoint)
        return
    if child['phase'] in ['term_intent','worker_term_queued','term_released']:
        if current['State']['Paused']:
            idle=child['idle_sample']
            if current['State']['Pid']!=idle['master_pid']:
                raise RuntimeError('Stopped worker identity changed')
            for pid in [idle['worker_pid'],idle['master_pid']]:os.kill(pid,signal.SIGTERM)
            q.run(['docker','unpause',child['container_id']])
            checkpoint('term_released')
        stop.await_stopped(directory/key,child,checkpoint)
        return
    if not current['State']['Paused'] or not child.get('idle_sample',{}).get('accepted'):
        check_configuration_metadata(directory/key,child)
        freeze(directory,state,key,hook)
    check_configuration_metadata(directory/key,child)
    raw=read_regular(directory/key/'original-config.json')[0]
    # An already fenced warm candidate needs no identical-file publication.
    if stop.maintenance_config(raw,explicit=True)==raw:
        if read_regular(Path(child['configuration_path']))[0]!=raw:
            raise ConfigurationConflict('Warm candidate configuration changed')
        stop.restart_policy(child['container_id'],{'Name':'no'})
        checkpoint('term_intent')
        idle=child['idle_sample']
        current=stop.owned_state(child)
        if current['State']['Pid']!=idle['master_pid']:
            raise RuntimeError('Frozen worker changed')
        for pid in [idle['worker_pid'],idle['master_pid']]:os.kill(pid,signal.SIGTERM)
        q.run(['docker','unpause',child['container_id']])
        checkpoint('term_released')
        stop.await_stopped(directory/key,child,checkpoint)
    else:
        stop.stop_idle(directory/key,child,checkpoint)


def compose_restore(directory,state):
    # Resume the newer publication first. Its missing canonical pathname must
    # never be mistaken for a gap in the older forward publication.
    if 'compose_rebase' in state:
        d.publish(state['compose_rebase'],read_regular(directory/'compose-rebase-before.private.yml')[0],
            read_regular(directory/'compose-rebase-after.private.yml')[0],'forward')
        return
    doc=state.get('compose_publication')
    if doc is None:
        return
    original=read_regular(directory/'compose-before.private.yml')[0]
    desired=read_regular(directory/'compose-after.private.yml')[0]
    current=d.publication(doc,original,desired,'forward')._read(Path(doc['path']))
    if current is None or current in (original,desired):
        d.restore_document(doc,original,desired)
        return
    # A concurrent edit to another service is preserved while reverting only
    # our own image. Any edit to this service causes a conflict instead.
    if 'compose_rebase' not in state:
        rendered=d.rendered(current,doc['path'],state['project'],state['service'])
        service=rendered['services'][state['service']]
        if service==state['original_service']:
            return
        restored,_,_=d.image_edit(current,doc['path'],state['project'],state['service'],
            state['original_image'],state['candidate_service'])
        # Restore the exact original scalar (which may be a local tag), but
        # leave all unrelated concurrent content intact.
        old_image=state['original_service']['image']
        restored=d.patch_image(restored,state['service'],old_image,retained_reference=True)
        expected=copy.deepcopy(rendered); expected['services'][state['service']]=state['original_service']
        if d.rendered(restored,doc['path'],state['project'],state['service'])!=expected:
            raise ConfigurationConflict('Compose rollback changed unrelated values')
        retain(directory/'compose-rebase-before.private.yml',current)
        retain(directory/'compose-rebase-after.private.yml',restored)
        state['compose_rebase']={**doc,'token':state['token']+'-rebase',
                                'metadata':d.metadata(Path(doc['path']))}
        save(directory,state)
    d.publish(state['compose_rebase'],read_regular(directory/'compose-rebase-before.private.yml')[0],
        read_regular(directory/'compose-rebase-after.private.yml')[0],'forward')


def snapshot_and_restore(directory,state,hook):
    snapshot=directory/'snapshot'; snapshot.mkdir(mode=0o700)
    manifest={part:q.tree_copy(Path(state[part]),snapshot/part) for part in ['data','logs']}
    q.database_check(snapshot/'data/state.db')
    raw=(json.dumps(manifest,sort_keys=True)+'\n').encode()
    write_new(snapshot/'manifest.private.json',raw)
    state['backup']={'manifest_sha256':q.digest(raw),'bytes':sum(x['bytes'] for v in manifest.values() for x in v.values()),
        'files':sum(len(v) for v in manifest.values()),'direct_to_san':True}
    save(directory,state,'snapshot_verified',hook)
    restored=Path(state['runtime'])/'restore'; restored.mkdir(mode=0o700)
    for part in ['data','logs']:q.tree_copy(snapshot/part,restored/part)
    # Read-only structural recovery in the exact original Python/SQLite runtime.
    # Its full application boot/restart proof is retained separately. Never let
    # an older image's scheduler prune restored attachments during this check.
    program="import sqlite3; c=sqlite3.connect('file:/snapshot/state.db?mode=ro',uri=True); assert c.execute('pragma integrity_check').fetchall()==[('ok',)]; assert all(c.execute('select count(*) from '+t).fetchone() is not None for t in ['seen','seen_uidl','attachments']); print('RESTORE_OK')"
    q.run(['docker','run','--rm','--network','none','--read-only','--memory','256m','--cpus','1',
        '-v',str(restored/'data')+':/snapshot:ro','--entrypoint','python',state['original_image'],'-c',program],timeout=60)
    before=data_identity(restored/'data',restored/'logs')
    config=restored/'data/config.json'
    config.write_bytes(stop.maintenance_config(read_regular(directory/'original_stop/original-config.json')[0],explicit=True))
    name='codex-voicemail-rollout-restore-'+state['token']
    state['restore_name']=name; save(directory,state,'restore_create_intent',hook)
    cid=q.run(['docker','create','--name',name,'--label','codex.voicemail.fixture=true',
        '--label',d.LABEL+'='+state['token'],'--label',d.ROLE+'=restore',
        '--network','none','--restart','no','--memory','512m','--cpus','1',
        '-v',str(restored/'data')+':/app/data','-v',str(restored/'logs')+':/app/logs',
        state['candidate_image']],text=True).strip()
    q.run(['docker','start',cid])
    current=q.inspect(cid)
    q.http_ready({'container_id':cid,'image':state['candidate_image'],
                  'configuration_fingerprint':q.config_fingerprint(current)})
    if data_identity(restored/'data',restored/'logs')!=before:
        raise RuntimeError('Candidate restore altered deduplication or attachments')
    cleanup_restore(state)
    for part in ['data','logs']:
        if q.tree_manifest(snapshot/part)!=manifest[part]:raise RuntimeError('Immutable snapshot changed')
    state['backup']['independent_restore_verified']=True
    save(directory,state,'restore_verified',hook)


def cleanup_restore(state):
    if not state.get('restore_name'):return
    current=d.find(state['restore_name'])
    if current is None:return
    labels=current['Config'].get('Labels') or {}
    expected={d.LABEL:state['token'],d.ROLE:'restore','codex.voicemail.fixture':'true'}
    if (any(labels.get(k)!=v for k,v in expected.items()) or current['Image']!=state['candidate_image']
            or current['HostConfig']['NetworkMode']!='none'):
        raise RuntimeError('Foreign restore fixture; preserve it')
    mounts={m['Destination']:(m['Type'],m['Source'],m['RW']) for m in current['Mounts']}
    if mounts!={'/app/'+part:('bind',str(Path(state['runtime'])/'restore'/part),True) for part in ['data','logs']}:
        raise RuntimeError('Restore fixture mounts changed; preserve it')
    if current['State']['Running']:
        q.run(['docker','kill','--signal=TERM',current['Id']])
        q.run(['docker','wait',current['Id']],timeout=60)
    current=q.inspect(current['Id'])
    if current['State']['Running'] or current['State'].get('OOMKilled') or current['State']['ExitCode']!=0:
        raise RuntimeError('Restore fixture did not stop cleanly')
    q.run(['docker','rm',current['Id']])


def remove_stopped(current):
    if current['State']['Running'] or current['State']['Paused']:
        raise RuntimeError('Refuse to remove a running service')
    if current['State'].get('OOMKilled') or current['State']['ExitCode']!=0:
        raise RuntimeError('Service did not stop cleanly')
    q.run(['docker','rm',current['Id']])  # No force, volumes or data removal.


@exclusive_operation
def worker(directory,hook=None):
    state=q.load(directory)
    if state['phase']!='prepared':raise RuntimeError('Forward rollout is single-use; recover interrupted work')
    current,role=owned_current(directory,state)
    if role!='initial':raise RuntimeError('Original service changed before rollout')
    if read_regular(Path(state['data'])/'config.json')[0]!=read_regular(directory/'original_stop/original-config.json')[0]:
        raise ConfigurationConflict('Application configuration changed after preparation')
    if d.rendered(read_regular(Path(state['compose_path']))[0],state['compose_path'],state['project'],state['service'])['services'][state['service']]!=state['original_service']:
        raise ConfigurationConflict('Selected Compose service changed after preparation')
    fence(state).preflight(); save(directory,state,'fence_intent',hook); fence(state).apply()
    ready(state); q.run(['ss','-K','sport = :'+str(state['port'])])
    save(directory,state,'ingress_fenced',hook)
    stop_service(directory,state,'original_stop',hook)
    save(directory,state,'original_stopped',hook)
    snapshot_and_restore(directory,state,hook)
    state['warm_identity']=data_identity(Path(state['data']),Path(state['logs']))
    save(directory,state,'original_remove_intent',hook)
    current,role=owned_current(directory,state)
    if role!='initial':raise RuntimeError('Original identity changed before replacement')
    remove_stopped(current); save(directory,state,'original_removed',hook)
    save(directory,state,'candidate_create_intent',hook)
    cid=d.create(state['name'],state['candidate_spec'])
    state['candidate_id']=cid; save(directory,state,'candidate_created',hook)
    d.verify(q.inspect(cid),state['candidate_spec'],state['candidate_image'])
    q.run(['docker','start',cid]); ready(state)
    if data_identity(Path(state['data']),Path(state['logs']))!=state['warm_identity']:
        raise RuntimeError('Candidate warmup changed live records or attachments')
    save(directory,state,'candidate_warm',hook)
    # Rebase onto unrelated concurrent Compose edits before our publication.
    raw=read_regular(Path(state['compose_path']))[0]
    desired,config_hash,service=d.image_edit(raw,state['compose_path'],state['project'],state['service'],
        state['candidate_image'],state['original_service'])
    if config_hash!=state['candidate_hash'] or service!=state['candidate_service']:
        raise ConfigurationConflict('Selected service hash changed')
    write_new(directory/'compose-before.private.yml',raw)
    write_new(directory/'compose-after.private.yml',desired)
    state['compose_publication']={'path':state['compose_path'],'token':state['token'],
                                 'metadata':d.metadata(Path(state['compose_path']))}
    save(directory,state,'compose_publish_intent',hook)
    d.publish(state['compose_publication'],raw,desired,'forward',
        lambda phase:save(directory,state,'compose_'+phase,hook))
    save(directory,state,'compose_published',hook)
    save(directory,state,'activation_intent',hook)
    # From this point recovery must preserve the current live data. No restore
    # helper ever copies the earlier snapshot back over these mounts.
    stop.publication(directory/'original_stop',state['original_stop'],True).apply(
        lambda phase:save(directory,state,'activation_'+phase,hook))
    save(directory,state,'configuration_activated',hook)
    stop.restart_policy(cid,state['restart_policy'])
    current,role=owned_current(directory,state)
    if role!='candidate' or not current['State']['Running']:raise RuntimeError('Candidate activation failed')
    ready(state); save(directory,state,'candidate_active',hook)
    fence(state).remove(); save(directory,state,'ingress_restored',hook)
    if state.get('anonymous_url'):
        request=urllib.request.Request(state['anonymous_url'],method='GET')
        try:
            with urllib.request.urlopen(request,timeout=10) as response:status=response.status
        except urllib.error.HTTPError as error:status=error.code
        if status!=401:raise RuntimeError('Public Basic Auth boundary changed')
    save(directory,state,'committed',hook)
    sanitized(directory,state)


def recovery_configuration(directory,state,current):
    """Choose fresh configuration and handle our own interrupted file publication."""
    path=Path(state['data'])/'config.json'
    original=read_regular(directory/'original_stop/original-config.json')[0]
    fenced=stop.maintenance_config(original,explicit=True)
    try:raw=read_regular(path)[0]
    except FileNotFoundError:
        forward=stop.publication(directory/'original_stop',state['original_stop'])
        reverse=stop.publication(directory/'original_stop',state['original_stop'],True)
        if forward._read(forward.previous)!=original or reverse._read(reverse.previous) not in (None,fenced):
            raise ConfigurationConflict('Missing configuration is not an owned publication gap')
        raw=None
    desired=original if raw in (None,fenced) else raw
    value=json.loads(desired)
    if not isinstance(value,dict) or not isinstance(value.get('system'),dict) or not isinstance(value.get('mailboxes'),list):
        raise ConfigurationConflict('Invalid concurrent application configuration; preserve for review')
    return raw,desired


@exclusive_operation
def recover(directory,force=False):
    state=q.load(directory)
    if state['phase']=='recovered' or (state['phase']=='committed' and not force):return
    try:
        cleanup_restore(state)
        current,role=owned_current(directory,state)
        fence(state).apply()
        if role=='initial':
            def checkpoint(phase):
                state['original_stop']['phase']=phase; save(directory,state)
            stop.recover_original(directory/'original_stop',state['original_stop'],checkpoint)
        else:
            if 'recovery_config_ready' not in state:
                raw,desired=recovery_configuration(directory,state,current)
                retain(directory/'recovery-config.private.json',desired)
                state['recovery_config_ready']=True
                state['configuration_was_missing']=raw is None
                save(directory,state)
            desired=read_regular(directory/'recovery-config.private.json')[0]
            if role=='candidate' and current['State']['Status']=='created':
                # A controller can die between atomic create and first start.
                # This container never ran; no idle worker or stop journal exists.
                remove_stopped(current); save(directory,state,'candidate_removed')
                current,role=None,None
            if role=='candidate':
                if 'candidate_stop' not in state:
                    if state['configuration_was_missing']:
                        m=state['original_stop']
                        child=make_stop(directory/'candidate_stop',state,current,raw=desired,
                            metadata={'mode':m['configuration_mode'],'uid':m['configuration_uid'],'gid':m['configuration_gid']})
                    else:child=make_stop(directory/'candidate_stop',state,current)
                    state['candidate_stop']=child; save(directory,state)
                if state['configuration_was_missing'] and not Path(state['data'],'config.json').exists():
                    freeze(directory,state,'candidate_stop')
                    child=state['candidate_stop']
                    write_new(Path(child['configuration_path']),desired,child['configuration_mode'],
                              (child['configuration_uid'],child['configuration_gid']))
                stop_service(directory,state,'candidate_stop')
                current,role=owned_current(directory,state)
                remove_stopped(current); save(directory,state,'candidate_removed')
            path=Path(state['data'])/'config.json'
            if 'recovery_app_publication' not in state:
                actual=read_regular(path)[0]
                if actual!=desired:
                    retain(directory/'recovery-app-before.private.json',actual)
                    state['recovery_app_publication']={'path':str(path),'token':state['token']+'-recover-app',
                        'metadata':d.metadata(path)}
                    save(directory,state,'application_restore_intent')
            if 'recovery_app_publication' in state:
                d.publish(state['recovery_app_publication'],read_regular(directory/'recovery-app-before.private.json')[0],
                          desired,'forward')
            compose_restore(directory,state)
            current,role=owned_current(directory,state)
            if current is None:
                save(directory,state,'original_recreate_intent')
                cid=d.create(state['name'],state['original_spec'])
                state['recreated_original_id']=cid; save(directory,state,'original_recreated')
                current,role=owned_current(directory,state)
            if role!='original':raise RuntimeError('Unexpected service during recovery')
            stop.restart_policy(current['Id'],state['restart_policy'])
            if not current['State']['Running']:q.run(['docker','start',current['Id']])
        compose_restore(directory,state)
        ready(state)
        current,role=owned_current(directory,state)
        if role not in ('initial','original') or not current['State']['Running'] or current['State']['Paused']:
            raise RuntimeError('Original service did not recover')
        fence(state).remove(); save(directory,state,'recovered'); sanitized(directory,state)
    except BaseException as error:
        state['recovery_error_type']=type(error).__name__
        save(directory,state)
        raise


def sanitized(directory,state):
    current,role=owned_current(directory,state)
    result={'checked_utc':now(),'phase':state['phase'],'fixture':state['fixture'],
        'deployed':state['phase']=='committed' and not state['fixture'],
        'current_id':current['Id'],'current_image':current['Image'],'current_role':role,
        'original_image':state['original_image'],'candidate_image':state['candidate_image'],
        'running':current['State']['Running'],'configuration_sha256':q.config_fingerprint(current),
        'canonical_compose_sha256':q.digest(read_regular(Path(state['compose_path']))[0]),
        'snapshot_restored_over_live_data':False,
        'backup':state.get('backup'),'real_provider_acceptance':False,
        'restart_policy':current['HostConfig']['RestartPolicy'],
        'owned_fences_removed':all(fence(state).inspect_owned(family) is None for family in [4,6])}
    path=directory/('rollout-'+state['phase']+'.sanitized.json')
    if not path.exists():write_new(path,(json.dumps(result,indent=2)+'\n').encode())


@exclusive_operation
def prepare(args):
    directory=args.generation; os.umask(0o077)
    current=q.inspect(args.container)
    if (current['Id']!=args.expected_container or current['Image']!=args.original_image
            or q.config_fingerprint(current)!=args.expected_fingerprint
            or not current['State']['Running'] or current['State']['Paused']):
        raise RuntimeError('Expected live original runtime changed')
    d.validate_template(current)
    name=current['Name'].lstrip('/')
    fixture=name.startswith('codex-voicemail-') and (current['Config'].get('Labels') or {}).get('codex.voicemail.fixture')=='true'
    if args.fixture != fixture or (not fixture and (name!='voicemail-forwarder' or args.port!=8089)):
        raise RuntimeError('Unexpected service or fixture authorization')
    if fixture and not 18000<=args.port<=18999:raise RuntimeError('Fixture port outside test range')
    if not fixture:
        url=urlsplit(args.anonymous_url or '')
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise RuntimeError('Production requires a credential-free HTTPS boundary probe')
    if not fixture and directory.parent.stat().st_dev==Path('/').stat().st_dev:
        raise RuntimeError('Immutable production backup must stream directly to SAN')
    platforms=[]
    for image in [args.original_image,args.candidate_image]:
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',image):
            raise RuntimeError('Exact retained image unavailable')
        info=json.loads(q.run(['docker','image','inspect',image]))[0]
        if info['Id']!=image:raise RuntimeError('Retained image identity differs')
        platforms.append((info['Os'],info['Architecture'],info.get('Variant')))
    if platforms[0]!=platforms[1] or platforms[0][0]!='linux':
        raise RuntimeError('Candidate platform differs from the running original')
    mounts={m['Destination']:Path(m['Source']) for m in current['Mounts']}
    ids=q.run(['docker','ps','-q'],text=True).split()
    others=json.loads(q.run(['docker','inspect',*ids])) if ids else []
    for other in others:
        if other['Id']==current['Id']:continue
        for mount in other['Mounts']:
            if mount.get('RW') and mount['Type']=='bind':
                source=Path(mount['Source']).resolve()
                if any(source==p or source in p.parents or p in source.parents for p in mounts.values()):
                    raise RuntimeError('Another running container has a writable overlapping mount')
    raw=read_regular(args.compose)[0]
    if q.digest(raw)!=args.expected_compose:raise ConfigurationConflict('Canonical Compose changed')
    model=d.rendered(raw,args.compose,args.project,args.service)
    service=model['services'][args.service]
    if set(service)-{'image','container_name','command','entrypoint','restart','mem_limit','network_mode','volumes','cpus'}:
        raise ConfigurationConflict('Unreviewed Compose service fields')
    if (service.get('container_name')!=name or service.get('network_mode')!='host'
            or service.get('command')!=current['Config']['Cmd']
            or (service.get('entrypoint') or None)!=current['Config']['Entrypoint']
            or service.get('restart')!=current['HostConfig']['RestartPolicy']['Name']
            or int(service.get('mem_limit',0))!=current['HostConfig']['Memory']
            or round(float(service.get('cpus',0))*1_000_000_000)!=current['HostConfig']['NanoCpus']):
        raise ConfigurationConflict('Canonical service does not match deployed settings')
    binds={v['target']:(v['source'],bool(v.get('read_only',False))) for v in service.get('volumes',[])}
    if binds!={k:(str(v),False) for k,v in mounts.items()}:raise ConfigurationConflict('Canonical bind mounts differ')
    if service.get('environment') or service.get('depends_on'):
        raise ConfigurationConflict('Unexpected environment override or dependencies')
    if json.loads(q.run(['docker','image','inspect',service['image']]))[0]['Id']!=args.original_image:
        raise ConfigurationConflict('Canonical image tag does not resolve to the live image')
    original_hash=q.run(d.compose_command(args.compose,args.project)+['--hash',args.service],input=raw).decode().split()[1]
    _,candidate_hash,candidate_service=d.image_edit(raw,args.compose,args.project,args.service,args.candidate_image,service)
    config=read_regular(mounts['/app/data']/'config.json')[0]
    if d.metadata(mounts['/app/data']/'config.json')['acl']:
        raise ConfigurationConflict('Application configuration ACL requires a supported publication adapter')
    stop.maintenance_config(config,explicit=True)  # Validate shape without exporting values.
    if fixture:
        from rehearse_ingress_activation import safe_config
        safe_config(config)
    q.independent_paths([mounts['/app/data'],mounts['/app/logs'],directory,args.runtime_root])
    q.private_directory(directory,create=True); q.private_directory(args.runtime_root,create=True)
    write_new(directory/'container.private.json',(json.dumps(current)+'\n').encode())
    token=secrets.token_hex(8)
    state={'phase':'prepared','token':token,'mark':secrets.randbelow(0xFFFFFFFE)+1,
        'name':name,'original_id':current['Id'],'original_image':args.original_image,
        'candidate_image':args.candidate_image,'project':args.project,'service':args.service,
        'compose_path':str(args.compose),'original_service':service,'candidate_service':candidate_service,
        'candidate_hash':candidate_hash,'original_hash':original_hash,'fixture':fixture,
        'port':args.port,'data':str(mounts['/app/data']),'logs':str(mounts['/app/logs']),
        'runtime':str(args.runtime_root),'restart_policy':current['HostConfig']['RestartPolicy'],
        'py_spy':args.py_spy,'idle_timeout':args.idle_timeout,'anonymous_url':args.anonymous_url}
    state['original_spec']=d.template(current,args.original_image,token,'original',original_hash)
    state['candidate_spec']=d.template(current,args.candidate_image,token,'candidate',candidate_hash)
    state['original_stop']=make_stop(directory/'original_stop',state,current)
    save(directory,state)
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','worker','recover'])
    parser.add_argument('--generation',type=Path,required=True)
    parser.add_argument('--runtime-root',type=Path)
    parser.add_argument('--container',default='voicemail-forwarder')
    parser.add_argument('--expected-container')
    parser.add_argument('--expected-fingerprint')
    parser.add_argument('--original-image')
    parser.add_argument('--candidate-image')
    parser.add_argument('--compose',type=Path)
    parser.add_argument('--expected-compose')
    parser.add_argument('--project',default='docker')
    parser.add_argument('--service',default='voicemail-forwarder')
    parser.add_argument('--port',type=int,default=8089)
    parser.add_argument('--py-spy',default='/home/dblagbro/.local/bin/py-spy')
    parser.add_argument('--idle-timeout',type=int,default=900)
    parser.add_argument('--anonymous-url')
    parser.add_argument('--fixture',action='store_true')
    parser.add_argument('--force',action='store_true',help='Explicit rollback after commit')
    args=parser.parse_args()
    if os.geteuid()!=0 or not __debug__:parser.error('Root and unoptimized Python required')
    if args.mode=='prepare':prepare(args)
    elif args.mode=='worker':worker(args.generation)
    else:recover(args.generation,force=args.force)


if __name__=='__main__':main()
