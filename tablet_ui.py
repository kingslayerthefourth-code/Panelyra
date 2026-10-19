"""Mac selection and delivery controls; USB/import stay optional."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st
from tablet_delivery import DeliveryStore
from tablet_push import PushClient, load_device, save_device, config_path


def ensure_companion(app_dir: Path) -> bool:
    from panelyra.services.companion_runtime import request
    request(app_dir)
    try:
        with socket.create_connection(('127.0.0.1', 8588), timeout=.2): return True
    except OSError: pass
    return False


@st.cache_resource
def library_store(app_dir: str) -> DeliveryStore:
    return DeliveryStore(Path(app_dir))


@st.fragment(run_every=3)
def delivery_status(app_dir: Path) -> None:
    store = library_store(str(app_dir))
    from panelyra.engine import policy
    if policy(app_dir):st.info('Background transfers are paused in Settings → General. Queued chapters are kept; resume there when ready to send.')
    for issue in store.issues():st.warning(f"Delivery record {issue['origin']} needs attention: {issue['error']}. The original file was retained.")
    tablet = store.tablet()
    live = time.time() - tablet.get('updated_at', 0) < 40
    if live:
        st.success(f"{tablet.get('device', 'Huawei')} is available · Panelyra {tablet.get('version', '')}")
    elif not load_device(app_dir):
        st.caption('Tablet not currently responding. Selected deliveries stay queued; open Panelyra on the Huawei to receive them.')
    for transfer in reversed(store.list()[-8:]):
        chapters = transfer['chapters']
        books = ', '.join(dict.fromkeys(chapter['series_title'] for chapter in chapters))
        done, total = len(transfer['acknowledged']), len(chapters)
        st.write(f"{books} — {transfer['status'].title()} · {done}/{total} chapters")
        received = transfer['received_bytes']
        if transfer['status'] == 'ready': received = transfer['total_bytes']
        st.progress(min(1., received / max(1, transfer['total_bytes'])))
        st.caption(f"{received / 1048576:.1f}/{transfer['total_bytes'] / 1048576:.1f} MB")
        if transfer['bytes_per_second']>0:st.caption(f"{transfer['bytes_per_second']/1048576:.2f} MB/s · about {transfer['eta_seconds']} seconds remaining")
        if transfer.get('error'): st.warning(transfer['error'])
        if transfer['status'] not in ('ready', 'cancelled'):
            left, right = st.columns(2)
            action = 'resume' if transfer['status'] in ('paused', 'failed') else 'pause'
            if left.button('Retry' if transfer['status'] == 'failed' else action.title(), key=f"delivery-{action}-{transfer['transfer_id']}"):
                store.update(transfer['transfer_id'], action, {}); st.rerun(scope='fragment')
            if right.button('Cancel delivery', key=f"delivery-cancel-{transfer['transfer_id']}"):
                store.update(transfer['transfer_id'], 'cancel', {}); st.rerun(scope='fragment')


def render_legacy_connection(app_dir: Path) -> None:
    st.subheader("Tablet connection")
    st.caption("Pair a device once, then send chapters from Library → Chapter actions.")
    from tablet_companion import CERT_FILE, certificate_status, renew_certificate
    if CERT_FILE.exists():
        try:
            certificate = certificate_status()
            if certificate['expired'] or certificate['days_remaining'] < 30:
                st.warning('Companion certificate expired or expires soon. Pause background work, renew it below, then pair the tablet again. Books and progress stay intact.')
            else:
                st.caption(f"Companion identity certificate: {certificate['days_remaining']} days remaining. IP changes do not replace it.")
            with st.expander('Companion identity recovery'):
                st.caption('Renewal changes the certificate pin and requires pairing again. A local backup of the old certificate, key and connection state is retained. It does not remove books or progress.')
                confirmed = st.checkbox('I understand I must pair again', key='confirm_companion_renew')
                if st.button('Renew companion certificate', disabled=not confirmed):
                    try:
                        backup = renew_certificate(app_dir)
                        st.session_state.tablet_setup_open = True
                        st.success(f'Renewed. Backup: {backup}. Unpause and use Start / reconnect, then pair with the new code.')
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        st.error(str(error))
        except (OSError, ValueError) as error:
            st.error(f'Companion identity needs recovery: {error}. Restore the saved certificate and matching key together; no files were reset.')
    if st.button("Start / reconnect tablet companion", key="start_tablet_companion"):
        try:
            ensure_companion(app_dir)
            st.session_state.tablet_setup_open = True
        except ValueError as error:st.warning(str(error))
    from panelyra.services.companion_runtime import state as companion_state
    service=companion_state(app_dir)
    if service.get('status')=='failed':st.error(service.get('error','Companion failed; use Start / reconnect.'))
    if st.session_state.get("tablet_setup_open"):
        saved = {}
        try: saved = json.loads((app_dir / 'data/tablet_companion/state.json').read_text())
        except (OSError, ValueError): pass
        if saved.get('device_token'):
            st.success(f"Paired with {saved.get('device_name', 'tablet')}")
        else:
            try:
                from tablet_companion import current_pair_code, ensure_certificate, local_ip
                address = local_ip()
                ensure_certificate(address)
                code, _ = current_pair_code()
                st.code(f'{address}:8588', language=None)
                st.code(code, language=None)
                st.caption('On your tablet: Reader → Connections & transfers → Pair with short code. Expires after five minutes.')
            except (OSError, ValueError, subprocess.SubprocessError):
                st.error('Companion could not start. Check data/tablet_companion/companion.log.')


def render_chapter_delivery(app_dir: Path, series: str, chapter: str, *, available: bool) -> None:
    saved = {}
    try: saved = json.loads((app_dir / 'data/tablet_companion/state.json').read_text())
    except (OSError, ValueError): pass
    device = load_device(app_dir)
    paired = bool(device or saved.get('device_token'))
    if not paired:
        st.caption("Pair your tablet in Settings → Connections to send chapters.")
    if st.button("Send Original chapter to tablet", disabled=not paired or not available, key="send_selected_chapter"):
        try:
            if device:
                transfer = library_store(str(app_dir)).create([{'series_key': series, 'chapter': chapter}], transport='push', target_pin=device['pin'])
                from panelyra.engine import policy
                st.success('Queued. Transfers are paused in Settings → General; resume there to send.' if policy(app_dir) else 'Queued for your tablet. Keep Receive mode on; track delivery in Settings → Connections.')
            else:
                ensure_companion(app_dir)
                transfer = library_store(str(app_dir)).create([{'series_key': series, 'chapter': chapter}], True)
                st.success(f"Queued {len(transfer['chapters'])} chapter(s). Open Panelyra on your tablet to receive them.")
        except (ValueError, OSError) as error:
            st.warning(str(error))


def render_delivery_panel(app_dir: Path) -> None:
    """Compatibility entry for external callers; device setup now lives in Settings."""
    render_connections(app_dir)


def render_connections(app_dir: Path) -> None:
    st.subheader("Tablet connection")
    st.caption("Send chapters from your Mac over Wi-Fi. Your Mac firewall can stay unchanged.")
    device = load_device(app_dir)
    if device:
        st.success(f"Pairing saved · {device.get('device', 'Tablet')}")
        st.caption("Pairing does not expire. The tablet must be on the same Wi-Fi with Receive mode running to accept chapters.")
        address = st.text_input("Tablet address", value=device['address'], key='push_saved_address', help='If Wi-Fi gives your tablet a new address, copy it from Receive mode. Its saved identity will still be checked.')
        if st.button("Check connection", key='push_check'):
            try:
                with st.spinner('Connecting to tablet…'):
                    client = PushClient(address, device['pin'], device['token'])
                    client.request('GET', '/v1/status')
                    device['address'] = client.address; save_device(app_dir, device)
                st.success('Tablet is reachable and ready to receive.')
            except Exception as error:
                st.error(f"Could not connect: {error}. Open Receive mode on the tablet and check its address.")
        with st.expander('Send selected chapters'):
            if st.button('Choose chapters', key='push_choose'): st.session_state.push_choose_open = True
            if st.session_state.get('push_choose_open'):
                catalog = library_store(str(app_dir)).library.catalog()
                lookup = {f"{c['series_key']}/{c['chapter']}": c for c in catalog['chapters']}
                chosen = st.multiselect('Saved chapters', list(lookup), format_func=lambda key: f"{lookup[key]['series_title']} · Chapter {lookup[key]['chapter']}", key='push_chapters')
                if st.button('Send selected chapters', type='primary', disabled=not chosen, key='push_send'):
                    try:
                        selection = [{'series_key': lookup[k]['series_key'], 'chapter': lookup[k]['chapter']} for k in chosen]
                        library_store(str(app_dir)).create(selection, transport='push', target_pin=device['pin'])
                        st.success(f'Queued {len(selection)} chapters. Already verified copies are skipped by the tablet.')
                    except (OSError, ValueError) as error: st.error(str(error))
        with st.expander('Connection options'):
            if st.button('Remove tablet from this Mac', key='push_remove'):
                config_path(app_dir).unlink(missing_ok=True)
                st.info('On the tablet, use Forget Mac before connecting again. Saved chapters stay untouched.')
                st.rerun()
    else:
        st.write('On your tablet, open **Connections & transfers → Receive from Mac**. Enter the address and setup code shown there.')
        st.caption('Requires Panelyra for Android 1.5.0 or newer. Only unused setup codes expire; saved pairings remain connected.')
        with st.form('push_pair_form'):
            address = st.text_input('Tablet address', placeholder='192.168.1.20:8590')
            code = st.text_input('Tablet setup code', type='password', help='12 characters. Spaces are optional.')
            submitted = st.form_submit_button('Connect to tablet', type='primary')
        if submitted:
            try:
                with st.spinner('Verifying tablet and saving pairing…'):
                    device = PushClient(address).pair(code)
                    save_device(app_dir, device)
                st.rerun()
            except Exception as error:
                st.error(f'Could not connect: {error}')
    with st.expander('Older connection method'):
        render_legacy_connection(app_dir)
    delivery_status(app_dir)
