"""Only emulator-5556. Uses a synthetic library; never pairs a physical tablet."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from tablet_push import PushClient

root = Path(__file__).resolve().parents[1]
adb = Path(os.environ['ANDROID_HOME']) / 'platform-tools/adb'
serial = 'emulator-5556'
def run(*args, check=True):
    return subprocess.run([str(adb), '-s', serial, *args], capture_output=True, check=check)
assert run('shell','getprop','ro.kernel.qemu').stdout.strip() == b'1', 'Refusing to run on a physical tablet'
run('install', '-r', str(root/'android/app/build/outputs/apk/debug/app-debug.apk'))
run('install', '-r', str(root/'android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'))
# This emulator was created for this test; never clear a real user's library.
run('shell','pm','clear','com.panelyra.tablet')
run('shell','pm','grant','com.panelyra.tablet','android.permission.POST_NOTIFICATIONS')
run('forward','tcp:19446','tcp:8590')
report = root / 'validation';report.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='push-fixture-') as tmp, (report/'android-acceptance.log').open('wb') as log:
    temporary = Path(tmp)
    image = temporary/'cover.png';Image.new('RGB',(120,200),'teal').save(image)
    cbz = temporary/'chapter.cbz'
    with zipfile.ZipFile(cbz,'w') as archive:
        for n in range(1,4):archive.write(image,f'{n:02}.png')
    process = subprocess.Popen([str(adb),'-s',serial,'shell','am','instrument','-w','-e','class',
        'com.panelyra.tablet.ReceiveAcceptanceTest#receivesInBackgroundAndRemembersPairing',
        'com.panelyra.tablet.test/androidx.test.runner.AndroidJUnitRunner'],stdout=log,stderr=subprocess.STDOUT)
    def wait(phase):
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            result=run('exec-out','run-as','com.panelyra.tablet','cat','files/receive-acceptance.json',check=False)
            try:
                value=json.loads(result.stdout)
                if value['phase']==phase:return value
            except ValueError:pass
            if process.poll() is not None:raise RuntimeError((report/'android-acceptance.log').read_text())
            time.sleep(.3)
        raise TimeoutError(f'Android phase {phase}: '+(report/'android-acceptance.log').read_text())
    try:
        state=wait('ready')
        client=PushClient('127.0.0.1:19446');client.pair(state['code'])
        metadata=dict(kind='chapter',series_key='receive-fixture',series_title='Receive Fixture',chapter='1',title='Chapter 1',size=cbz.stat().st_size,sha256=hashlib.sha256(cbz.read_bytes()).hexdigest())
        client.send_file(cbz,metadata,lambda n:None,lambda:None)
        cover=dict(kind='cover',series_key='receive-fixture',cover_provenance='supplied',size=image.stat().st_size,sha256=hashlib.sha256(image.read_bytes()).hexdigest())
        client.send_file(image,cover,lambda n:None,lambda:None)
        wait('restarted')
        client.request('GET','/v1/status')
        client.send_file(cbz,metadata,lambda n:None,lambda:None)
        run('shell','run-as','com.panelyra.tablet','touch','files/host-reconnected')
        wait('background')
        client.send_file(cbz,dict(metadata,chapter='2',title='Chapter 2'),lambda n:None,lambda:None)
        wait('done')
        process.wait(timeout=30)
        output=(report/'android-acceptance.log').read_text()
        assert 'OK (1 test)' in output,output
        (report/'tablet-receive.png').write_bytes(run('exec-out','run-as','com.panelyra.tablet','cat','files/receive-screen.png').stdout)
        (report/'android-acceptance.json').write_text(json.dumps({'passed':True,'pairing':'real Android Keystore TLS','restart':'same token and certificate','duplicate':'progress preserved','background':'chapter 2 installed with app backgrounded','fixture_chapters':2},indent=2))
        print('PASS: real Android TLS pairing, chapter + cover import, service restart, duplicate/progress preservation, background receive')
    finally:
        if process.poll() is None:process.terminate()
        run('forward','--remove','tcp:19446',check=False)
