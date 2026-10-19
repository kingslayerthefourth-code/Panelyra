"""Real engine-owned listener startup/stop on loopback; no LAN advertisement or USB."""
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine_lifecycle_regression import owner,wait_for
from panelyra import engine
from panelyra.services.worker_runtime import engine_environment,worker_pid
import tablet_companion as companion


class StartupTests(unittest.TestCase):
    def test_hidden_or_unidentified_companion_archives_are_not_ready(self):
        from foundation_regression import png
        with tempfile.TemporaryDirectory() as temporary:
            state=companion.CompanionState(app_dir=Path(temporary))
            job=companion.CompanionJob('a'*32,'https://fixture/book/chapter-1',1,'reliable')
            root=state.jobs_dir/job.job_id/'library'
            for path,identified in [(root/'book/1',True),(root/'book/.receiving-2',True),(root/'book/3',False)]:
                path.mkdir(parents=True)
                with zipfile.ZipFile(path/'chapter-pages.cbz','w') as archive:archive.writestr('001.png',png())
                if identified:(path/'metadata.json').write_text(json.dumps({'url':'https://fixture/book/chapter-1','image_count':1}))
            self.assertEqual([artifact.chapter for artifact in state._collect(job)],['1'])

    def test_expiry_and_explicit_renewal_preserve_key_jobs_and_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);certs=root/'data/tablet_companion'
            names={'STATE_DIR':certs,'CERT_FILE':certs/'companion.crt','KEY_FILE':certs/'companion.key',
                   'CERT_HOST_FILE':certs/'host','PAIR_CODE_FILE':certs/'pair.json'}
            with patch.multiple(companion,**names):
                companion.ensure_certificate('127.0.0.1')
                original=companion.CERT_FILE.read_bytes();key=companion.KEY_FILE.read_bytes()
                status=companion.certificate_status();self.assertFalse(status['expired'])
                (certs/'state.json').write_text(json.dumps({'device_token':'fixture','jobs':[{'fixture':True}]}))
                with self.assertRaises(ValueError):companion.renew_certificate(root)
                engine.pause(root,True)
                with patch.object(companion.time,'time',return_value=status['expires']+1):
                    with self.assertRaisesRegex(ValueError,'expired'):companion.ensure_certificate('192.168.1.50')
                backup=companion.renew_certificate(root)
                self.assertEqual((backup/'companion.crt').read_bytes(),original)
                self.assertEqual(companion.KEY_FILE.read_bytes(),key)
                self.assertNotEqual(companion.CERT_FILE.read_bytes(),original)
                self.assertEqual(json.loads((certs/'state.json').read_text()),{'device_token':'','device_name':'','jobs':[{'fixture':True}]})
                self.assertFalse(companion.certificate_status()['expired'])

    def test_owned_listener_starts_and_closes_on_quit_or_owner_loss(self):
        for action in ('quit','owner-loss'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);log=root/'service.log';child=None
                try:
                    with owner(root),log.open('w') as out:
                        child=subprocess.Popen([sys.executable,'-m','panelyra.automation.companion_service','--root',str(root),
                                                '--bind','127.0.0.1','--port','0','--no-advertise'],cwd=Path(__file__).resolve().parents[1],
                                               env=engine_environment(root),stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
                        wait_for(lambda:'https://127.0.0.1:' in log.read_text() or child.poll() is not None,10)
                        self.assertIsNone(child.poll(),log.read_text())
                        port=int(re.search(r'https://127.0.0.1:(\d+)',log.read_text()).group(1))
                        self.assertEqual(worker_pid(root,'companion'),child.pid)
                        cert=(root/'data/tablet_companion/companion.crt').read_text()
                        pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest()
                        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
                        connection=http.client.HTTPSConnection('127.0.0.1',port,context=context,timeout=3)
                        try:
                            connection.connect();self.assertEqual(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest(),pin)
                            connection.request('GET','/api/v1/health')
                            with connection.getresponse() as response:self.assertEqual(json.load(response)['status'],'ok')
                        finally:connection.close()
                        if action=='quit':engine.stop_workers(root,{})
                    child.wait(timeout=8)
                    self.assertEqual(child.returncode,130,log.read_text())
                    self.assertIsNone(worker_pid(root,'companion'))
                    with socket.socket() as probe:self.assertNotEqual(probe.connect_ex(('127.0.0.1',port)),0)
                finally:
                    if child and child.poll() is None:child.kill();child.wait()

    def test_corrupt_companion_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);file=root/'data/tablet_companion/state.json';file.parent.mkdir(parents=True);file.write_text('{damaged')
            with self.assertRaisesRegex(ValueError,'preserved'):companion.CompanionState(app_dir=root)
            self.assertEqual(file.read_text(),'{damaged')

    def test_capture_launch_is_managed_and_interruption_not_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);calls=[]
            class Process:
                pid=99999999
                def wait(self):return 130
            def factory(command,**kwargs):calls.append((command,kwargs));return Process()
            with owner(root):
                state=companion.CompanionState(app_dir=root,process_factory=factory)
                job=state.create_job({'start_url':'https://reader.example/chapter-1','chapter_count':1})
                wait_for(lambda:job.status=='interrupted')
            command,options=calls[0]
            self.assertIn('panelyra.automation.managed_capture',command)
            self.assertIn('--root',command)
            self.assertEqual(options['env']['PANELYRA_ENGINE_ROOT'],str(root.resolve()))
            self.assertIn('retained',job.error)

    def test_short_capture_cannot_be_reported_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            state=companion.CompanionState(app_dir=Path(temporary))
            job=companion.CompanionJob('a'*32,'https://reader.example/chapter-1',2,'reliable',status='running')
            state.jobs[job.job_id]=job
            class Process:
                def wait(self):return 0
            state.processes[job.job_id]=Process()
            artifact=companion.Artifact('b'*32,'book','1','Chapter 1','book/1/chapter-pages.cbz',50,'c'*64)
            with patch.object(state,'_collect',return_value=[artifact]):state._monitor(job.job_id)
            self.assertEqual(job.status,'failed');self.assertIn('1 of 2',job.error)

    def test_partial_startup_failure_closes_already_bound_tls_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);certs=root/'data/tablet_companion';servers=[]
            names={'APP_DIR':root,'STATE_DIR':certs,'CERT_FILE':certs/'cert.pem','KEY_FILE':certs/'key.pem',
                   'CERT_HOST_FILE':certs/'host','PAIR_CODE_FILE':certs/'pair.json'}
            original=companion.CompanionServer
            def server(*args):
                value=original(*args);servers.append(value);return value
            with patch.multiple(companion,**names),patch.object(companion,'CompanionServer',side_effect=server),\
                 patch.object(companion,'PairChallengeServer',side_effect=OSError('test port unavailable')):
                with self.assertRaises(OSError):companion.main(['--bind','127.0.0.1','--port','0','--no-advertise','--no-usb-watch'])
            self.assertEqual(len(servers),1);self.assertEqual(servers[0].socket.fileno(),-1)


if __name__=='__main__':unittest.main(verbosity=2)
