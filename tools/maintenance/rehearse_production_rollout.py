"""Exercise the production operator on empty-mailbox, test-port TLS fixtures.

Only the fixture setup/verification is specialized. Forward work and systemd
ExecStopPost recovery call the same production operator used for deployment.
Run the rehearsal foreground command through the maintained capacity guard.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import deployment_runtime as d
import production_rollout as p
import quiesce_backup as q
import rehearse_ingress_activation as r


PHASES=['none','ingress_fenced','original_stop_config_original_retained',
        'original_stopped','snapshot_verified','restore_verified','original_removed',
        'candidate_created','candidate_warm','compose_candidate_file_ready',
        'compose_original_retained','compose_candidate_published',
        'activation_candidate_file_ready','activation_original_retained',
        'activation_candidate_published','configuration_activated','candidate_active',
        'fresh_data_committed','ingress_restored','concurrent_edits']


def production_key():
    current=q.inspect('voicemail-forwarder')
    return [current['Id'],q.config_fingerprint(current),current['State']['StartedAt'],
            q.digest(q.read_regular(Path('/home/dblagbro/docker/docker-compose.yml'))[0])]


def fixture_worker(case,fault):
    fixture=q.load(case/'operation')
    directory=case/'rollout'
    def interrupt(phase):
        if phase==fault:
            q.write_new(case/'fault-injected.json',json.dumps({'stage':phase,'signal':'SIGKILL'}).encode())
            os.kill(os.getpid(),signal.SIGKILL)
    def hook(phase,state):
        if phase=='ingress_fenced':
            if r.proxy_status(fixture,True)!=502 or r.proxy_status(fixture,False)!=401:
                raise RuntimeError('TLS proxy bypassed the backend fence')
        interrupt(phase)
        if phase=='candidate_active':
            r.seed(Path(fixture['runtime']),'after-activation')
            fixture['after_activation_data']=r.data_identity(Path(fixture['runtime']))
            r.persist(case/'operation',fixture)
            interrupt('fresh_data_committed')
        if phase=='ingress_restored':
            if r.proxy_status(fixture,True)!=200 or r.proxy_status(fixture,False)!=401:
                raise RuntimeError('Activated TLS/auth fixture failed')
            if fault=='concurrent_edits':
                path=Path(state['compose_path'])
                raw=q.read_regular(path)[0]
                edited=raw.replace(b'synthetic-unrelated:v1',b'synthetic-unrelated:v2')
                if edited==raw:raise RuntimeError('Concurrent-edit witness absent')
                temp=path.with_name('compose-concurrent.tmp')
                q.write_new(temp,edited);os.replace(temp,path);q.sync_directory(path.parent)
                config=Path(state['data'])/'config.json'
                value=json.loads(q.read_regular(config)[0]);value['system']['synthetic_concurrent_edit']='preserved'
                desired=(json.dumps(value,indent=2)+'\n').encode()
                temp=config.with_name('config-concurrent.tmp')
                q.write_new(temp,desired);os.replace(temp,config);q.sync_directory(config.parent)
                fixture['concurrent_configuration_sha256']=q.digest(desired)
                r.persist(case/'operation',fixture)
                interrupt('concurrent_edits')
    p.worker(directory,hook)


def fixture_recover(case):
    if (case/'rollout/journal.json').exists():p.recover(case/'rollout',force=True)


def cleanup(case):
    fixture=q.load(case/'operation')
    if not (case/'rollout/journal.json').exists():
        r.cleanup(case/'operation');return
    fixture_recover(case)
    state=q.load(case/'rollout')
    current,role=p.owned_current(case/'rollout',state)
    if role not in ('initial','original') or state['phase']!='recovered':
        raise RuntimeError('Fixture did not recover; preserve it')
    r.safe_config(q.read_regular(Path(state['data'])/'config.json')[0])
    for item in [current,r.owned(fixture,'proxy')]:
        if item is None:continue
        if item['State']['Running']:
            q.run(['docker','update','--restart=no',item['Id']])
            q.run(['docker','kill','--signal=TERM',item['Id']])
            q.run(['docker','wait',item['Id']],timeout=60)
        p.remove_stopped(q.inspect(item['Id']))


def compose_fixture(case,fixture):
    runtime=Path(fixture['runtime']);current=r.owned(fixture,'original')
    lines=['services:','  unrelated:','    image: synthetic-unrelated:v1','  voicemail-forwarder:',
           '    container_name: '+fixture['original_name'],'    image: '+fixture['original_image'],
           '    restart: unless-stopped','    network_mode: host','    mem_limit: 512m','    cpus: 1',
           '    command: '+json.dumps(current['Config']['Cmd']),'    volumes:']
    lines+=['      - '+json.dumps(str(runtime/name)+':/app/'+name) for name in ['data','logs']]
    raw=('\n'.join(lines)+'\n').encode()
    path=runtime/'compose.yml';q.write_new(path,raw,0o640)
    q.write_new(case/'compose-original.private.yml',raw)
    state=p.prepare(SimpleNamespace(generation=case/'rollout',runtime_root=runtime/'operator',
        container=current['Id'],expected_container=current['Id'],expected_fingerprint=q.config_fingerprint(current),
        original_image=fixture['original_image'],candidate_image=fixture['candidate_image'],
        compose=path,expected_compose=q.digest(raw),project='codex-voicemail-'+fixture['token'],
        service='voicemail-forwarder',port=fixture['backend_port'],py_spy=fixture['py_spy'],
        idle_timeout=30,anonymous_url=None,fixture=True))
    return state


def rehearse(args):
    r.check_ports(args.backend_port,args.proxy_port)
    before=production_key()
    if q.run(['systemctl','list-units','codex-vmail-*','--state=running,activating','--no-legend'],text=True).strip():
        raise RuntimeError('Another voicemail operation is active')
    network=json.loads(q.run(['docker','network','inspect',args.network]))[0]
    if args.gateway not in [x.get('Gateway') for x in network['IPAM']['Config']]:
        raise RuntimeError('Unexpected fixture gateway')
    for address in ['127.0.0.1',args.gateway]:
        with socket.socket() as sock:
            # Match Gunicorn's listener behavior while still rejecting an
            # active listener; completed probes can leave harmless TIME_WAIT.
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind((address,args.backend_port))
    q.private_directory(args.generation,create=True);q.private_directory(args.runtime_root,create=True)
    results=[]
    for index,fault in enumerate(args.faults.split(',')):
        if fault not in PHASES:raise ValueError('Unknown fault stage')
        case=args.generation/('case-'+str(index))
        try:
            fixture=r.setup_case(case,args)
            state=compose_fixture(case,fixture)
            deadline=time.monotonic()+15
            while True:
                try:
                    if r.proxy_status(fixture,True)==200 and r.proxy_status(fixture,False)==401:break
                except OSError:pass
                if time.monotonic()>deadline:raise RuntimeError('Initial TLS fixture not ready')
                time.sleep(.2)
            helper=str(Path(__file__).resolve())
            recovery=' '.join([sys.executable,helper,'recover','--generation',str(case)])
            with (case/'unit.private.log').open('wb') as log:
                result=subprocess.run(['systemd-run','--wait','--collect',
                    '--unit=codex-vmail-operator-'+fixture['token'],'--property=Type=oneshot',
                    '--property=TimeoutStartSec=300','--property=TimeoutStopSec=180',
                    '--property=ExecStopPost='+recovery,sys.executable,helper,'worker',
                    '--generation',str(case),'--fault',fault],stdout=log,stderr=subprocess.STDOUT,timeout=500)
            final=q.load(case/'rollout');fixture=q.load(case/'operation')
            if final['phase']!='recovered':raise RuntimeError('Systemd backstop did not recover fixture')
            injected=(case/'fault-injected.json').exists()
            if injected!=(fault!='none') or (result.returncode==0)!=(fault=='none'):
                raise RuntimeError('Requested fault was not exercised')
            expected=fixture.get('after_activation_data',fixture['before_data'])
            if r.data_identity(Path(fixture['runtime']))!=expected:
                raise RuntimeError('Fresh deduplication or attachment data changed')
            raw=q.read_regular(case/'compose-original.private.yml')[0]
            if fault=='concurrent_edits':raw=raw.replace(b'synthetic-unrelated:v1',b'synthetic-unrelated:v2')
            if q.read_regular(Path(final['compose_path']))[0]!=raw:
                raise RuntimeError('Canonical Compose rollback lost an edit')
            current,role=p.owned_current(case/'rollout',final)
            actual_config=q.read_regular(Path(final['data'])/'config.json')[0]
            expected_config=fixture.get('concurrent_configuration_sha256',
                q.digest(q.read_regular(case/'rollout/original_stop/original-config.json')[0]))
            if q.digest(actual_config)!=expected_config:raise RuntimeError('Configuration rollback lost an edit')
            auth=r.proxy_status(fixture,True);anon=r.proxy_status(fixture,False)
            if auth!=200 or anon!=401:raise RuntimeError('Recovered TLS/auth fixture failed')
            if any(p.fence(final).inspect_owned(family) is not None for family in [4,6]):
                raise RuntimeError('Fixture firewall fence remains')
            results.append({'fault_stage':fault,'controller_sigkill':injected,'original_resumed':True,
                'exact_runtime_verified':True,'compose_restored_preserving_unrelated_edits':True,
                'application_configuration_restored':True,'fresh_data_preserved':bool(fixture.get('after_activation_data')),
                'tls_authenticated_200':True,'tls_anonymous_401':True,'owned_fences_removed':True,
                'original_image':current['Image'],'backup_restore_verified':final.get('backup',{}).get('independent_restore_verified',False)})
            print(json.dumps(results[-1]),flush=True)
        finally:
            if (case/'operation/journal.json').exists():cleanup(case)
        if production_key()!=before:raise RuntimeError('Concurrent production change; stop rehearsal')
    receipt={'checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'production_modified':False,'deployed':False,'synthetic_data_only':True,
        'same_production_operator':True,'real_provider_acceptance':False,'candidate_image':args.candidate_image,
        'original_image':args.original_image,'cases':results,'owned_fixtures_removed':True,
        'retained_generations_removed':False,'limitations':'Test-only ports and synthetic data; no live provider transaction or reboot recovery proof.'}
    q.write_new(args.generation/'production-operator.sanitized.json',(json.dumps(receipt,indent=2)+'\n').encode())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['rehearse','worker','recover','cleanup'])
    parser.add_argument('--generation',type=Path,required=True)
    parser.add_argument('--runtime-root',type=Path)
    parser.add_argument('--original-image');parser.add_argument('--candidate-image');parser.add_argument('--nginx-image')
    parser.add_argument('--network',default='docker_default');parser.add_argument('--gateway',default='172.18.0.1')
    parser.add_argument('--backend-port',type=int,default=18089);parser.add_argument('--proxy-port',type=int,default=18090)
    parser.add_argument('--py-spy',default='/home/dblagbro/.local/bin/py-spy')
    parser.add_argument('--fault',default='none');parser.add_argument('--faults',default=','.join(PHASES))
    args=parser.parse_args()
    if os.geteuid()!=0 or not __debug__:parser.error('Root and unoptimized Python required')
    os.umask(0o077)
    if args.mode=='worker':fixture_worker(args.generation,args.fault)
    elif args.mode=='recover':fixture_recover(args.generation)
    elif args.mode=='cleanup':cleanup(args.generation)
    else:rehearse(args)


if __name__=='__main__':main()
