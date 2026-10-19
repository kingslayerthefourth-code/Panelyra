import json
import os
import subprocess
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sdk = Path(os.environ.get('ANDROID_HOME', str(root / 'tools/sdk')))
adb = str(sdk / 'platform-tools/adb')
fixture_root = Path(os.environ.get('PANELYRA_TEST_FIXTURE', str(root / 'fixture')))
fixture = json.loads((fixture_root / 'acceptance.json').read_text())
import sys
sys.path.insert(0, str(root))
import tablet_companion
tablet_companion.PAIR_CODE_FILE = fixture_root / 'data/tablet_companion/pair-code.json'
fixture['code'] = tablet_companion.current_pair_code()[0]
subprocess.run([adb, '-s', 'emulator-5554', 'reverse', 'tcp:19443', 'tcp:19443'], check=True)
for apk in ('android/app/build/outputs/apk/debug/app-debug.apk', 'android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'):
    subprocess.run([adb, '-s', 'emulator-5554', 'install', '-r', str(root / apk)], check=True)
subprocess.run([adb, '-s', 'emulator-5554', 'shell', 'pm', 'grant', 'com.panelyra.tablet', 'android.permission.POST_NOTIFICATIONS'], check=True)
result = subprocess.run([adb, '-s', 'emulator-5554', 'shell', 'am', 'instrument', '--user', '0', '-w', '-r',
                         '-e', 'address', fixture['address'], '-e', 'code', fixture['code'],
                         '-e', 'class', 'com.panelyra.tablet.TabletAcceptanceTest#selectedWifiChaptersAppearWithoutPickerAndReaderNavigates', 'com.panelyra.tablet.test/androidx.test.runner.AndroidJUnitRunner'], capture_output=True, text=True)
print(result.stdout); print(result.stderr)
if 'FAILURES' in result.stdout or 'OK (' not in result.stdout: raise SystemExit(1)
