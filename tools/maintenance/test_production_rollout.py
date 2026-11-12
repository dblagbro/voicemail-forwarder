import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deployment_runtime as d
import production_rollout as p
import quiescent_stop as stop


class RolloutRecoveryTests(unittest.TestCase):
    def test_configuration_permission_edit_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp).resolve();path=directory/'config.json';path.write_bytes(b'{}');path.chmod(0o600)
            metadata=d.metadata(path)
            child={'configuration_path':str(path),'configuration_mode':metadata['mode'],
                   'configuration_uid':metadata['uid'],'configuration_gid':metadata['gid']}
            p.check_configuration_metadata(directory,child)
            path.chmod(0o640)
            with self.assertRaises(d.ConfigurationConflict):p.check_configuration_metadata(directory,child)
            self.assertEqual(path.stat().st_mode & 0o777,0o640)

    def test_overlapping_operation_rejected_and_lock_released_on_error(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(p,'LOCK_PATH',Path(tmp)/'operation.lock'):
            @p.exclusive_operation
            def second():return 'owned'
            @p.exclusive_operation
            def first():
                with self.assertRaisesRegex(RuntimeError,'Another voicemail operation'):second()
                raise ValueError('synthetic failure')
            with self.assertRaises(ValueError):first()
            self.assertEqual(second(),'owned')

    def test_rebased_compose_recovery_resumes_each_interruption(self):
        # The original forward generation has stale unrelated-service bytes.
        # Recovery must resume the newer reverse generation even while its
        # canonical pathname is absent, preserving the concurrent edit.
        for boundary in ['candidate_file_ready','original_retained','candidate_published']:
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp).resolve(); path=directory/'compose.yml'
                original=b'old image; unrelated v1'
                deployed=b'new image; unrelated v1'
                concurrent=b'new image; unrelated v2'
                restored=b'old image; unrelated v2'
                path.write_bytes(concurrent)
                doc={'path':str(path),'token':'a'*16,'metadata':d.metadata(path)}
                rebase={**doc,'token':'b'*16}
                for name,data in [('compose-before.private.yml',original),('compose-after.private.yml',deployed),
                                  ('compose-rebase-before.private.yml',concurrent),('compose-rebase-after.private.yml',restored)]:
                    (directory/name).write_bytes(data)
                def interrupt(phase):
                    if phase==boundary:raise RuntimeError('synthetic interruption')
                with self.assertRaises(RuntimeError):d.publish(rebase,concurrent,restored,'forward',interrupt)
                p.compose_restore(directory,{'compose_publication':doc,'compose_rebase':rebase})
                self.assertEqual(path.read_bytes(),restored)
                p.compose_restore(directory,{'compose_publication':doc,'compose_rebase':rebase})
                self.assertEqual(path.read_bytes(),restored)

    def test_retained_intent_retries_but_preserves_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'intent.json'
            p.retain(path,b'first');p.retain(path,b'first')
            with self.assertRaises(d.ConfigurationConflict):p.retain(path,b'different')
            self.assertEqual(path.read_bytes(),b'first')

    def test_stop_journal_retry_does_not_execute_inside_paused_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp).resolve()/'stop';directory.mkdir(mode=0o700)
            child={'container_id':'owned','image':'old','phase':'prepared'}
            p.q.journal(directory,child)
            with patch.object(stop,'owned_state') as owned,patch.object(p.q,'executor_wait_site') as execute:
                value=p.make_stop(directory,{}, {'Id':'owned','Image':'old'})
                self.assertEqual(value['container_id'],'owned');owned.assert_called_once();execute.assert_not_called()
            with self.assertRaises(RuntimeError):p.make_stop(directory,{}, {'Id':'foreign','Image':'old'})

    def test_missing_configuration_requires_owned_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp).resolve();data=directory/'data';data.mkdir()
            childdir=directory/'original_stop';childdir.mkdir()
            original=json.dumps({'system':{},'mailboxes':[]}).encode()
            (childdir/'original-config.json').write_bytes(original)
            path=data/'config.json';path.write_bytes(original)
            stat=path.stat()
            child={'maintenance_version':1,'configuration_path':str(path),'token':'a'*16,
                   'configuration_mode':0o600,'configuration_uid':stat.st_uid,'configuration_gid':stat.st_gid}
            state={'data':str(data),'original_stop':child}
            def interrupt(phase):
                if phase=='original_retained':raise RuntimeError('synthetic interruption')
            with self.assertRaises(RuntimeError):stop.publication(childdir,child).apply(interrupt)
            self.assertEqual(p.recovery_configuration(directory,state,None),(None,original))
            stop.publication(childdir,child).previous.unlink()
            with self.assertRaises(d.ConfigurationConflict):p.recovery_configuration(directory,state,None)


if __name__=='__main__':unittest.main()
