"""Local process/SQLite fixtures. No websites, browsers or tablets are contacted."""
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from panelyra import engine
from panelyra.services import reading_updates as updates
from panelyra.services.library_store import connect, db_path, transaction
from panelyra.services.worker_runtime import (engine_environment, engine_pid, worker_pid,
    held_pid, interruptible_worker, WorkerInterrupted, worker_lease)


@contextmanager
def owner(root):
    path=root/'data/engine/owner.lock';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w+') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        lease.write(str(os.getpid()));lease.flush()
        yield


def wait_for(predicate,seconds=8):
    deadline=time.monotonic()+seconds
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('Fixture did not reach its checkpoint')
        time.sleep(.03)


def fixture(root,kind):
    if kind=='backend':
        from panelyra.automation import manga_backend as backend
        try:
            with patch.object(backend,'BINARY',Path(__file__)),patch.object(backend,'build_command',return_value=[sys.executable,str(Path(__file__).resolve()),'--binary-fixture',str(root)]):
                backend.run_staged('https://unused.example/book','1',root/'staging',browser_consent=True)
        except SystemExit:
            (root/'interrupted').write_text('backend cancelled without fallback')
        finally:
            (root/'finalized').write_text('backend cleanup ran')
        return
    try:
        with interruptible_worker(root,kind):
            (root/'ready').write_text(str(os.getpid()))
            try:
                while True:time.sleep(.05)
            finally:
                (root/'finalized').write_text('source cleanup ran')
    except WorkerInterrupted:
        (root/'interrupted').write_text('no retry/fallback')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.children=[]
    def tearDown(self):
        for child in self.children:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        self.tmp.cleanup()
    def start_fixture(self,kind):
        from panelyra.services.worker_runtime import WORKERS
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--fixture',kind,str(self.root),WORKERS[kind][1]],
                               cwd=ROOT,env=engine_environment(self.root),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.children.append(child)
        wait_for(lambda:(self.root/'ready').exists() or child.poll() is not None)
        if child.poll() is not None:self.fail(child.communicate()[1].decode())
        return child

    def test_held_lease_required_not_stale_pid_text(self):
        path=self.root/'data/engine/owner.lock';path.parent.mkdir(parents=True)
        path.write_text(str(os.getpid()))
        self.assertIsNone(engine_pid(self.root))
        with owner(self.root):self.assertEqual(engine_pid(self.root),os.getpid())
        self.assertIsNone(engine_pid(self.root))
        with self.assertRaises(ValueError):engine_environment(self.root)

    def test_pause_quit_signal_is_scoped_and_cleanup_runs(self):
        with owner(self.root):
            child=self.start_fixture('capture')
            self.assertEqual(worker_pid(self.root,'capture'),child.pid)
            os.kill(child.pid,signal.SIGSTOP)
            stopping={};self.assertTrue(engine.stop_workers(self.root,stopping))
            child.wait(timeout=5)
            self.assertTrue((self.root/'finalized').exists())
            self.assertTrue((self.root/'interrupted').exists())
            self.assertFalse(engine.stop_workers(self.root,stopping))
            self.assertEqual(stopping,{})

    def test_engine_loss_interrupts_worker_without_explicit_signal(self):
        with owner(self.root):child=self.start_fixture('capture')
        child.wait(timeout=5)
        self.assertTrue((self.root/'finalized').exists())
        self.assertTrue((self.root/'interrupted').exists())

    def test_queue_loss_interrupts_backend_even_while_engine_remains(self):
        with owner(self.root):
            with worker_lease(self.root,'downloads'),patch.dict(os.environ,PANELYRA_QUEUE_PID=str(os.getpid())):
                child=self.start_fixture('backend')
                binary_pid=int((self.root/'ready').read_text())
            self.assertEqual(engine_pid(self.root),os.getpid())
            child.wait(timeout=5)
        self.assertTrue((self.root/'finalized').exists())
        self.assertTrue((self.root/'interrupted').exists())
        with self.assertRaises(ProcessLookupError):os.kill(binary_pid,0)

    def test_unrelated_pid_in_held_worker_file_never_signalled(self):
        with worker_lease(self.root,'capture'):
            self.assertEqual(held_pid(self.root/'data/engine/capture-worker.lock'),os.getpid())
            self.assertIsNone(worker_pid(self.root,'capture'))
            with patch('panelyra.engine.os.kill',side_effect=AssertionError('Wrong process')):
                self.assertFalse(engine.stop_workers(self.root,{}))

    def test_paused_background_batch_exits_retaining_jobs(self):
        from panelyra.automation.download_queue import enqueue,read_queue
        job=enqueue(self.root,'https://source.example/book','1-30','Book')
        engine.pause(self.root,True)
        self.assertFalse(engine.batch_active(self.root))
        started=time.monotonic()
        with patch('tablet_push.PushWorker.start',side_effect=AssertionError('Paused sender')):
            engine.run(self.root,background=True)
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual(read_queue(self.root)['jobs'][0]['id'],job['id'])
        self.assertEqual(read_queue(self.root)['jobs'][0]['status'],'queued')
        self.assertIsNone(engine_pid(self.root))

    def test_ui_queues_discovery_but_cannot_launch_workers(self):
        with patch.object(updates,'catalog',return_value={'book':{}}),patch.object(updates.subprocess,'Popen',side_effect=AssertionError('UI spawned worker')):
            first=updates.request(self.root,'book');second=updates.request(self.root,'book')
            updates.ensure_worker(self.root)
        self.assertEqual(first,second)
        self.assertEqual(updates.state(self.root)['jobs'][0]['state'],'queued')

    def test_discovery_checkpoint_survives_pause_and_skips_completed_series(self):
        entries={key:{} for key in ('first','second','third')};calls=[]
        with patch.object(updates,'catalog',return_value=entries):updates.request(self.root)
        def check(root,key,entry,fetch):
            calls.append(key)
            if key=='second':engine.pause(self.root,True)
            return {'status':'ok','checked_at':time.time(),'chapters':[]}
        with patch.object(updates,'catalog',return_value=entries),patch.object(updates,'check',side_effect=check):
            updates.run(self.root)
            state=updates.state(self.root)
            self.assertEqual(state['jobs'][0]['state'],'interrupted')
            self.assertEqual(state['jobs'][0]['done_series'],['first','second'])
            engine.pause(self.root,False)
            updates.run(self.root)
        self.assertEqual(calls,['first','second','third'])
        self.assertEqual(updates.state(self.root)['jobs'][0]['state'],'finished')

    def test_abrupt_discovery_interruption_does_not_become_source_failure(self):
        with patch.object(updates,'catalog',return_value={'book':{}}):updates.request(self.root)
        with patch.object(updates,'catalog',return_value={'book':{}}),patch.object(updates,'check',side_effect=WorkerInterrupted()):
            with self.assertRaises(WorkerInterrupted):updates.run(self.root)
        self.assertEqual(updates.state(self.root)['jobs'][0]['state'],'interrupted')
        self.assertEqual(updates.state(self.root)['results'],{})

    def test_schema_upgrade_preserves_foundation_and_legacy_json(self):
        with transaction(db_path(self.root)) as db:
            db.execute("INSERT INTO progress VALUES('old',?,1)",('progress stays',))
            db.execute('DROP TABLE discovery_jobs');db.execute('DROP TABLE discovery_results')
            db.execute('PRAGMA user_version=1')
        legacy=self.root/'data/updates/discovery.json';legacy.parent.mkdir(parents=True)
        legacy.write_text('{damaged legacy must stay')
        self.assertEqual(updates.state(self.root),{'jobs':[],'results':{}})
        with connect(db_path(self.root)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)
            self.assertEqual(db.execute('SELECT payload FROM progress').fetchone()[0],'progress stays')
        self.assertEqual(legacy.read_text(),'{damaged legacy must stay')

    def test_many_pending_checks_not_truncated_and_rolled_back_atomically(self):
        with patch.object(updates,'catalog',return_value={str(n):{} for n in range(65)}):
            for n in range(65):updates.request(self.root,str(n))
        self.assertEqual(len(updates.state(self.root)['jobs']),65)
        def fail(value):value['jobs'].clear();raise RuntimeError('power loss')
        with self.assertRaises(RuntimeError):updates.mutate(self.root,fail)
        self.assertEqual(len(updates.state(self.root)['jobs']),65)

    def test_capture_workers_cannot_exceed_four(self):
        import manhwa_ocr_downloader as capture
        self.assertTrue(all(p['download_workers']<=4 for p in capture.CAPTURE_PERFORMANCE_PROFILES.values()))
        self.assertFalse(issubclass(WorkerInterrupted,Exception))

    def test_companion_is_explicit_and_session_scoped(self):
        from panelyra.services import companion_runtime as service
        with patch.object(service.subprocess,'Popen') as spawn:
            with owner(self.root):
                service.ensure_worker(self.root);spawn.assert_not_called()
                engine.pause(self.root,True)
                with self.assertRaises(ValueError):service.request(self.root)
                engine.pause(self.root,False);service.request(self.root);service.ensure_worker(self.root)
                self.assertEqual(spawn.call_count,1)
                command=spawn.call_args.args[0]
                self.assertIn('panelyra.automation.companion_service',command)
                service.record(self.root,owner=-1,status='queued');service.ensure_worker(self.root)
                self.assertEqual(spawn.call_count,1)

    def test_legacy_listener_lease_obeys_engine_loss(self):
        with owner(self.root):child=self.start_fixture('companion')
        child.wait(timeout=5)
        self.assertTrue((self.root/'finalized').exists())


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--fixture':fixture(Path(sys.argv[3]),sys.argv[2])
    elif len(sys.argv)>1 and sys.argv[1]=='--binary-fixture':
        (Path(sys.argv[2])/'ready').write_text(str(os.getpid()))
        while True:time.sleep(.05)
    else:unittest.main(verbosity=2)
