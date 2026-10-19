"""Upgrade test resets ONLY the isolated emulator's synthetic test library."""
import json
import os
import sqlite3
import subprocess
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sdk = Path(os.environ.get('ANDROID_HOME', str(root / 'tools/sdk')))
adb = str(sdk / 'platform-tools/adb')
fixture_root = Path(os.environ.get('PANELYRA_TEST_FIXTURE', str(root / 'fixture')))
def run(*args): return subprocess.run([adb, '-s', 'emulator-5554', *args], check=True, capture_output=True, text=True)
run('shell', 'am', 'force-stop', 'com.panelyra.tablet')
run('shell', 'pm', 'clear', '--user', '0', 'com.panelyra.tablet')
run('install', '-r', '-d', str(root / 'releases/Panelyra-Tablet-v1.3.1-debug.apk'))
schema = json.loads((root / 'android/app/schemas/com.panelyra.tablet.data.PanelyraDatabase/1.json').read_text())['database']
fixture = fixture_root / 'upgrade-fixture.db'
if fixture.exists(): fixture.unlink()
connection = sqlite3.connect(fixture)
for entity in schema['entities']:
    connection.execute(entity['createSql'].replace('${TABLE_NAME}', entity['tableName']))
    for index in entity.get('indices', []): connection.execute(index['createSql'].replace('${TABLE_NAME}', entity['tableName']))
connection.execute('CREATE TABLE room_master_table (id INTEGER PRIMARY KEY,identity_hash TEXT)')
connection.execute('INSERT INTO room_master_table VALUES (42,?)', (schema['identityHash'],))
connection.execute('PRAGMA user_version=1')
connection.execute("INSERT INTO series VALUES ('fixture-book','Fixture Book',NULL,1)")
remote = '/sdcard/Android/data/com.panelyra.tablet/files/Panelyra/fixture-book'
import hashlib
cbz = fixture_root / 'manhwa/fixture-book/05/chapter-pages.cbz'
digest = hashlib.sha256(cbz.read_bytes()).hexdigest()
connection.execute('INSERT INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('fixture-book','05',5,'Chapter 5',f'{remote}/05/chapter-pages.cbz',digest,3,cbz.stat().st_size,'',2,45,'fit',1,1,1))
connection.commit(); connection.close()
run('push', str(fixture), '/data/local/tmp/panelyra-upgrade-fixture.db')
run('shell', 'run-as', 'com.panelyra.tablet', 'mkdir', '-p', 'databases')
run('shell', 'run-as', 'com.panelyra.tablet', 'cp', '/data/local/tmp/panelyra-upgrade-fixture.db', 'databases/panelyra-tablet.db')
run('shell', 'mkdir', '-p', remote)
run('push', str(fixture_root / 'manhwa/fixture-book') + '/.', remote)
run('install', '-r', str(root / 'android/app/build/outputs/apk/debug/app-debug.apk'))
run('install', '-r', str(root / 'android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'))
run('shell', 'pm', 'grant', 'com.panelyra.tablet', 'android.permission.POST_NOTIFICATIONS')
result = run('shell', 'am', 'instrument', '--user', '0', '-w', '-r', '-e', 'class',
             'com.panelyra.tablet.TabletAcceptanceTest#upgradeRestoresOrphansAndPreservesSurvivingProgress',
             'com.panelyra.tablet.test/androidx.test.runner.AndroidJUnitRunner')
print(result.stdout)
if 'OK (' not in result.stdout: raise SystemExit(1)
