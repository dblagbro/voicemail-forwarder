import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deployment_runtime as d

OLD='sha256:'+'1'*64
NEW='sha256:'+'2'*64
TOKEN='a'*16


class RuntimeDeploymentTests(unittest.TestCase):
    def original(self):
        return {'Config':{'Image':'retained:tag','Env':['SYNTHETIC=private-value'],
            'Cmd':['gunicorn'],'Labels':{'custom':'preserved'}},
            'HostConfig':{'NetworkMode':'host','AutoRemove':False,'Privileged':False,
                'RestartPolicy':{'Name':'unless-stopped','MaximumRetryCount':0},'Memory':1073741824},
            'Mounts':[{'Destination':'/app/'+n,'Source':'/fixture/'+n,'Type':'bind','RW':True}
                      for n in ['data','logs']]}

    def test_template_preserves_runtime_and_marks_atomic_creation(self):
        original=self.original(); saved=copy.deepcopy(original)
        spec=d.template(original,NEW,TOKEN,'candidate','3'*64)
        self.assertEqual(original,saved)
        self.assertEqual(spec['Config']['Env'],original['Config']['Env'])
        self.assertEqual(spec['Mounts'],original['Mounts'])
        self.assertEqual(spec['HostConfig']['Memory'],original['HostConfig']['Memory'])
        self.assertEqual(spec['Config']['Labels'][d.LABEL],TOKEN)
        self.assertEqual(spec['HostConfig']['RestartPolicy']['Name'],'no')
        d.verify(dict(spec,Image=NEW),spec,NEW)

    def test_runtime_drift_fails_without_logging_values(self):
        spec=d.template(self.original(),NEW,TOKEN,'candidate','3'*64)
        for area,key,value in [('Config','Env',['SYNTHETIC=changed-private']),
                               ('HostConfig','Memory',1)]:
            actual=copy.deepcopy(spec);actual['Image']=NEW;actual[area][key]=value
            with self.assertRaises(RuntimeError) as error:d.verify(actual,spec,NEW)
            self.assertNotIn('changed-private',str(error.exception))
        actual=dict(copy.deepcopy(spec),Image=NEW)
        actual['Mounts'][0]['Source']='/foreign/data'
        with self.assertRaises(RuntimeError):d.verify(actual,spec,NEW)

    def test_unsupported_mounts_and_dependencies_rejected(self):
        for change in ['auto','network','volume']:
            value=self.original()
            if change=='auto':value['HostConfig']['AutoRemove']=True
            elif change=='network':value['HostConfig']['NetworkMode']='bridge'
            else:value['Mounts'][0]['Type']='volume'
            with self.assertRaises(RuntimeError):d.template(value,NEW,TOKEN,'candidate','3'*64)

    def test_docker_unspecified_oom_default_only(self):
        original=self.original();original['HostConfig']['OomKillDisable']=None
        spec=d.template(original,NEW,TOKEN,'candidate','3'*64)
        actual=dict(copy.deepcopy(spec),Image=NEW);actual['HostConfig']['OomKillDisable']=False
        d.verify(actual,spec,NEW)
        actual['HostConfig']['OomKillDisable']=True
        with self.assertRaises(RuntimeError):d.verify(actual,spec,NEW)

    def test_image_edit_preserves_unrelated_bytes_comments_and_crlf(self):
        raw=('services:\r\n  unrelated:\r\n    image: '+OLD+'\r\n'
             '  voicemail-forwarder:\r\n    image: retained:tag # keep this\r\n'
             '    restart: unless-stopped\r\n  later:\r\n    image: later:tag\r\n').encode()
        changed=d.patch_image(raw,'voicemail-forwarder',NEW)
        self.assertEqual(changed,raw.replace(b'retained:tag',NEW.encode()))
        self.assertEqual(d.patch_image(changed,'voicemail-forwarder','retained:tag',retained_reference=True),raw)

    def test_ambiguous_compose_definitions_refused(self):
        for raw in [b'services:\n  voicemail-forwarder:\n    image: a\n    image: b\n',
            b'services:\n  voicemail-forwarder:\n    image: a\n  voicemail-forwarder:\n    image: b\n',
            b'services:\n  voicemail-forwarder: {image: a}\n']:
            with self.assertRaises(d.ConfigurationConflict):d.patch_image(raw,'voicemail-forwarder',NEW)

    def test_semantic_drift_blocks_before_publication(self):
        raw=b'services:\n  voicemail-forwarder:\n    image: old:tag\n'
        model={'services':{'voicemail-forwarder':{'image':'old:tag','restart':'unless-stopped'}}}
        changed=copy.deepcopy(model);changed['services']['voicemail-forwarder'].update(image=NEW,restart='always')
        with patch.object(d,'rendered',side_effect=[model,changed]), self.assertRaises(d.ConfigurationConflict):
            d.image_edit(raw,Path('/fixture/compose.yml'),'fixture','voicemail-forwarder',NEW,model['services']['voicemail-forwarder'])

    def test_each_file_boundary_restores_metadata_and_original(self):
        for boundary in ['candidate_file_ready','original_retained','candidate_published']:
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp).resolve()/'compose.yml';path.write_bytes(b'original');path.chmod(0o640)
                state={'path':str(path),'token':TOKEN,'metadata':d.metadata(path)}
                def interrupt(phase):
                    if phase==boundary:raise RuntimeError('synthetic interruption')
                with self.assertRaises(RuntimeError):d.publish(state,b'original',b'candidate','forward',interrupt)
                d.restore_document(state,b'original',b'candidate')
                self.assertEqual(path.read_bytes(),b'original')
                self.assertEqual(d.metadata(path),state['metadata'])

    def test_foreign_file_edit_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()/'compose.yml';path.write_bytes(b'original')
            state={'path':str(path),'token':TOKEN,'metadata':d.metadata(path)}
            path.write_bytes(b'unrelated operator edit')
            with self.assertRaises(d.ConfigurationConflict):d.publish(state,b'original',b'candidate','forward')
            self.assertEqual(path.read_bytes(),b'unrelated operator edit')


if __name__=='__main__':unittest.main()
