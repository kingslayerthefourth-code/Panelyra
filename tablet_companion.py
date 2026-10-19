#!/usr/bin/env python3
"""Paired HTTPS companion for Panelyra's Android Reader + Downloader."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import plistlib
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import tempfile
import shutil
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit
from sync_library import OriginalLibrary
from tablet_delivery import DeliveryStore
import usb_sync


APP_DIR = Path(__file__).resolve().parent
STATE_DIR = APP_DIR / "data" / "tablet_companion"
CERT_FILE = STATE_DIR / "companion.crt"
KEY_FILE = STATE_DIR / "companion.key"
PAIR_CODE_FILE = STATE_DIR / "pair-code.json"
SYNC_REQUEST_FILE = STATE_DIR / "sync-request.json"
SYNC_STATUS_FILE = STATE_DIR / "tablet-sync-status.json"
CERT_HOST_FILE = STATE_DIR / "certificate-host.txt"
DEFAULT_PORT = 8588
MAX_CHAPTERS = 10000
PERFORMANCE = {"rapid": "rapid", "reliable": "reliable"}
IDENTITY_LOCK = threading.RLock()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{uuid.uuid4().hex}.tmp')
    try:
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as handle:
            json.dump(value,handle,indent=2,sort_keys=True);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def ensure_certificate(ip_address: str) -> None:
    with IDENTITY_LOCK:
        _ensure_certificate(ip_address)


def _ensure_certificate(ip_address: str) -> None:
    try:
        certificate_host = CERT_HOST_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        certificate_host = ""
    # The certificate is the Mac's durable identity, not the current DHCP address.
    if CERT_FILE.is_file() and KEY_FILE.is_file():
        status = certificate_status()
        if status['expired']:
            raise ValueError('The saved companion certificate expired. Pause work, then use Renew companion certificate in Connections and pair the tablet again. Existing identity files were preserved.')
        return
    if CERT_FILE.exists() or KEY_FILE.exists():
        raise ValueError('Only part of the saved companion identity exists. Restore both certificate and key from a backup; existing files were preserved.')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    config = STATE_DIR / "openssl.cnf"
    config.write_text(
        "\n".join(
            (
                "[req]",
                "distinguished_name=dn",
                "x509_extensions=extensions",
                "prompt=no",
                "[dn]",
                "CN=Panelyra Companion",
                "[extensions]",
                f"subjectAltName=IP:{ip_address},IP:127.0.0.1",
                "keyUsage=digitalSignature,keyEncipherment",
                "extendedKeyUsage=serverAuth",
            )
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "825", "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-config", str(config),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    KEY_FILE.chmod(0o600)
    CERT_HOST_FILE.write_text(ip_address, encoding="utf-8")
    config.unlink(missing_ok=True)


def certificate_fingerprint() -> str:
    der = ssl.PEM_cert_to_DER_cert(CERT_FILE.read_text(encoding="utf-8"))
    return hashlib.sha256(der).hexdigest()


def certificate_status() -> dict:
    """Inspect the durable identity, including its key, without replacing it."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    decoded = ssl._ssl._test_decode_cert(str(CERT_FILE))
    expires = ssl.cert_time_to_seconds(decoded['notAfter'])
    begins = ssl.cert_time_to_seconds(decoded['notBefore'])
    return {'fingerprint': certificate_fingerprint(), 'expires': expires,
            'days_remaining': int((expires - time.time()) / 86400),
            'expired': time.time() >= expires or time.time() < begins}


def renew_certificate(root: Path) -> Path:
    """Explicit offline renewal. Back up identity/state; never silently drop a pin."""
    from panelyra.engine import policy
    from panelyra.services.worker_runtime import worker_pid
    if not policy(root) or worker_pid(root, 'companion') or worker_pid(root, 'capture'):
        raise ValueError('Pause background work and wait for the companion/Capture to stop before renewing.')
    expected = Path(root).resolve() / 'data/tablet_companion'
    if STATE_DIR.resolve() != expected:
        raise ValueError('Companion identity path does not match this installation.')
    with IDENTITY_LOCK:
        # A mismatched/missing key is a restore problem, not permission to replace identity.
        certificate_status()
        state_file = STATE_DIR/'state.json'
        state = json.loads(state_file.read_text()) if state_file.is_file() else None
        backup = STATE_DIR / 'identity-backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        for path in (CERT_FILE, KEY_FILE, STATE_DIR/'state.json', PAIR_CODE_FILE):
            if path.is_file():
                shutil.copy2(path, backup/path.name)
                (backup/path.name).chmod(0o600)
        with tempfile.TemporaryDirectory(prefix='.renew-', dir=STATE_DIR) as temporary:
            candidate = Path(temporary)/'companion.crt'
            subprocess.run(['/usr/bin/openssl', 'req', '-new', '-x509', '-key', str(KEY_FILE),
                            '-days', '825', '-subj', '/CN=Panelyra Companion', '-out', str(candidate)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(candidate, KEY_FILE)
            candidate.chmod(0o600)
            os.replace(candidate, CERT_FILE)
        # Existing tablet pin intentionally refuses the renewed certificate. Require
        # the code-authenticated pairing flow again; retain jobs/library/progress.
        if state is not None:
            state['device_token'] = ''
            state['device_name'] = ''
            atomic_json(state_file, state)
        atomic_json(PAIR_CODE_FILE, {'code': '', 'expires': 0, 'attempts': 0})
        return backup


def current_pair_code() -> tuple[str, int]:
    with IDENTITY_LOCK:
        return _current_pair_code()


def _current_pair_code() -> tuple[str, int]:
    try:
        saved = json.loads(PAIR_CODE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    code = str(saved.get("code") or "")
    expires = int(saved.get("expires") or 0)
    if not code or time.time() >= expires or int(saved.get("attempts") or 0) >= 5:
        code = "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(12))
        expires = int(time.time() + 300)
        atomic_json(PAIR_CODE_FILE, {"code": code, "expires": expires, "attempts": 0})
    return code, expires


def sync_request_generation() -> int:
    try:
        return int(json.loads(SYNC_REQUEST_FILE.read_text(encoding="utf-8")).get("generation") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def request_wifi_sync() -> int:
    generation = max(time.time_ns(), sync_request_generation() + 1)
    atomic_json(SYNC_REQUEST_FILE, {"generation": generation, "requested_at": time.time()})
    return generation


@dataclass
class Artifact:
    artifact_id: str
    series: str
    chapter: str
    title: str
    filename: str
    size: int
    sha256: str
    source_url: str = ''


@dataclass
class CompanionJob:
    job_id: str
    start_url: str
    chapter_count: int
    performance: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    pid: int | None = None
    exit_code: int | None = None
    error: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    run_id: str = ''

    def public(self, log_tail: str = "") -> dict[str, Any]:
        value = asdict(self)
        value["log_tail"] = log_tail
        for artifact in value["artifacts"]:
            artifact["download_url"] = (
                f"/api/v1/jobs/{self.job_id}/artifacts/{artifact['artifact_id']}"
            )
        return value


class CompanionState:
    def __init__(
        self,
        *,
        app_dir: Path = APP_DIR,
        process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ) -> None:
        self.app_dir = app_dir.resolve()
        self.state_dir = self.app_dir / "data" / "tablet_companion"
        self.jobs_dir = self.state_dir / "jobs"
        self.state_file = self.state_dir / "state.json"
        self.process_factory = process_factory
        self.lock = threading.RLock()
        self.accepting=True
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.jobs: dict[str, CompanionJob] = {}
        self.library = OriginalLibrary(self.app_dir)
        self.deliveries = DeliveryStore(self.app_dir, self.library)
        saved = self._load()
        self.pairing_secret = str(saved.get("pairing_secret") or secrets.token_urlsafe(24))
        self.device_token = str(saved.get("device_token") or "")
        self.device_name = str(saved.get("device_name") or "")
        self.pair_sessions: dict[str, tuple[str, float, str]] = {}
        self._persist()

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
            for original in value.get("jobs", []):
                raw = dict(original)
                artifacts = [Artifact(**item) for item in raw.pop("artifacts", [])]
                job = CompanionJob(**raw, artifacts=artifacts)
                if job.status in {"queued", "running", "cancelling"}:
                    job.status = "interrupted"
                    job.error = "The companion restarted before this job completed."
                self.jobs[job.job_id] = job
            return value
        except FileNotFoundError:return {}
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise ValueError('The saved companion state is unreadable and was preserved. Restore it or use the newer Receive from Mac connection.') from error

    def _persist(self) -> None:
        with self.lock:
            atomic_json(
                self.state_file,
                {
                    "pairing_secret": self.pairing_secret,
                    "device_token": self.device_token,
                    "device_name": self.device_name,
                    "jobs": [asdict(job) for job in self.jobs.values()],
                },
            )

    def pair(self, secret: str, device_name: str) -> str | None:
        if not hmac.compare_digest(secret, self.pairing_secret):
            return None
        with self.lock:
            self.device_token = secrets.token_urlsafe(32)
            self.device_name = device_name.strip()[:80] or "Huawei tablet"
            self.pairing_secret = secrets.token_urlsafe(24)
            self._persist()
            return self.device_token

    def unpair(self) -> None:
        with self.lock:
            self.device_token = ""
            self.device_name = ""
            self.pairing_secret = secrets.token_urlsafe(24)
            self._persist()

    def authorized(self, header: str) -> bool:
        supplied = header.removeprefix("Bearer ").strip()
        return bool(self.device_token and hmac.compare_digest(supplied, self.device_token))

    def current_code(self) -> tuple[str, int]:
        return current_pair_code()

    @staticmethod
    def _proof(code: str, role: str, client_nonce: str, server_nonce: str, fingerprint: str) -> str:
        message = f"panelyra-pair-v2|{role}|{client_nonce}|{server_nonce}|{fingerprint}".encode()
        return hmac.new(code.encode(), message, hashlib.sha256).hexdigest()

    def pair_start(self, client_nonce: str, device_name: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-f0-9]{32,64}", client_nonce):
            raise ValueError("Invalid pairing challenge.")
        with self.lock:
            try:
                on_disk = json.loads(PAIR_CODE_FILE.read_text(encoding="utf-8"))
                pair_code = str(on_disk.get("code") or "")
                pair_expires = float(on_disk.get("expires") or 0)
                pair_attempts = int(on_disk.get("attempts") or 0)
            except (OSError, ValueError, TypeError):
                raise ValueError("Pairing code is unavailable.")
            if not pair_code or time.time() >= pair_expires or pair_attempts >= 5:
                raise ValueError("Pairing code expired. Refresh it on the Mac.")
            server_nonce = secrets.token_hex(24)
            self.pair_sessions[client_nonce] = (server_nonce, time.time() + 60, device_name[:80])
            return {"server_nonce": server_nonce, "proof": self._proof(
                pair_code, "server", client_nonce, server_nonce, certificate_fingerprint())}

    def pair_finish(self, client_nonce: str, server_nonce: str, proof: str) -> str | None:
        with self.lock:
            session = self.pair_sessions.pop(client_nonce, None)
            if not session or session[0] != server_nonce or time.time() > session[1]:
                return None
            try:
                on_disk = json.loads(PAIR_CODE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            code = str(on_disk.get("code") or "")
            attempts = int(on_disk.get("attempts") or 0) + 1
            expires = float(on_disk.get("expires") or 0)
            atomic_json(PAIR_CODE_FILE, {"code": code, "expires": expires, "attempts": attempts})
            expected = self._proof(code, "client", client_nonce, server_nonce, certificate_fingerprint())
            if time.time() >= expires or attempts > 5 or not hmac.compare_digest(expected, proof):
                return None
            self.device_token = secrets.token_urlsafe(32)
            self.device_name = session[2] or "Huawei tablet"
            atomic_json(PAIR_CODE_FILE, {"code": "", "expires": 0, "attempts": 0})
            self.pair_sessions.clear()
            self._persist()
            return self.device_token

    def create_job(self, payload: dict[str, Any]) -> CompanionJob:
        from panelyra.engine import policy
        if policy(self.app_dir):raise ValueError('Background work is paused on the Mac. Resume it in Settings before capturing.')
        start_url = str(payload.get("start_url", "")).strip()
        parsed = urlsplit(start_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("A valid HTTP or HTTPS chapter URL is required.")
        try:
            chapter_count = int(payload.get("chapter_count", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("Chapter count must be a number.") from exc
        if not 1 <= chapter_count <= MAX_CHAPTERS:
            raise ValueError(f"Chapter count must be between 1 and {MAX_CHAPTERS}.")
        performance = str(payload.get("performance", "reliable")).lower()
        if performance not in PERFORMANCE:
            raise ValueError("Performance must be rapid or reliable.")
        with self.lock:
            if not self.accepting:raise RuntimeError('The companion is stopping. Reconnect after Panelyra resumes.')
            if any(job.status in {"queued", "running", "cancelling"} for job in self.jobs.values()):
                raise RuntimeError("The Mac capture engine is already busy.")
            job = CompanionJob(uuid.uuid4().hex, start_url, chapter_count, performance)
            self.jobs[job.job_id] = job
            self._persist()
        try:self._launch(job)
        except Exception as error:
            with self.lock:
                if job.status=='queued':job.status='failed';job.error=str(error)[:300];self._persist()
            raise
        return job

    def _launch(self, job: CompanionJob) -> None:
        with self.lock:
            if not self.accepting or job.status!='queued':raise RuntimeError('Capture was stopped before launch.')
            self._launch_owned(job)

    def _launch_owned(self, job: CompanionJob) -> None:
        from panelyra.services.worker_runtime import engine_environment,held_pid,WORKERS
        environment=engine_environment(self.app_dir)
        if held_pid(self.app_dir/WORKERS['capture'][0]):
            with self.lock:
                job.status='failed';job.error='Another Capture is active. Wait for it to finish, then retry.';self._persist()
            raise RuntimeError(job.error)
        job_dir = self.jobs_dir / job.job_id
        output_dir = job_dir / "library"
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = job_dir / "capture.log"
        command = [
            sys.executable,
            '-m','panelyra.automation.managed_capture','--root',str(self.app_dir),
            "--start-url", job.start_url,
            "--max-chapters", str(job.chapter_count),
            "--output-dir", str(output_dir),
            "--reader-layout", "auto",
            "--performance-profile", PERFORMANCE[job.performance],
            "--no-save-images",
            "--skip-ocr",
        ]
        from automation_runtime import RunRecord, RunStore
        run = RunStore(RunRecord.create('download', {'start_url':job.start_url,
                       'max_chapters':job.chapter_count,'companion_job':job.job_id}, log_path), self.app_dir/'runs')
        run.capture.command(run.record.run_id, command)
        job.run_id = run.record.run_id
        environment['PANELYRA_RUN_RECORD'] = str(run.path)
        log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        try:
            process = self.process_factory(
                command,
                cwd=str(self.app_dir),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        except Exception:
            log_handle.close()
            with self.lock:
                job.status = "failed"
                job.error = "The desktop Capture engine could not be started."
                job.updated_at = time.time()
                self._persist()
            raise
        log_handle.close()
        run.update(pid=process.pid)
        with self.lock:
            job.status = "running"
            job.pid = process.pid
            job.updated_at = time.time()
            self.processes[job.job_id] = process
            self._persist()
        threading.Thread(target=self._monitor, args=(job.job_id,), daemon=True).start()

    def _monitor(self, job_id: str) -> None:
        process = self.processes[job_id]
        exit_code = process.wait()
        with self.lock:
            job = self.jobs[job_id]
            job.exit_code = exit_code
            job.updated_at = time.time()
            self.processes.pop(job_id, None)
            if job.status == "cancelling":
                job.artifacts = self._collect(job)
                job.status = "cancelled"
            elif job.status=='interrupted' or exit_code in (130,143,-signal.SIGTERM):
                job.artifacts=self._collect(job);job.status='interrupted'
                job.error=f'Capture interrupted; {len(job.artifacts)} verified chapters retained.'
            elif exit_code != 0:
                job.artifacts = self._collect(job)
                job.status = "failed"
                job.error = "Desktop Capture failed. Review the companion log for details."
            else:
                job.artifacts = self._collect(job)
                if len(job.artifacts)==job.chapter_count:
                    job.status = "completed"
                else:
                    job.status = "failed"
                    job.error = f"Capture saved {len(job.artifacts)} of {job.chapter_count} chapters. It did not complete the requested selection."
            if job.run_id:
                from panelyra.services.capture_store import CaptureStore
                store=CaptureStore(self.app_dir)
                record=store.read(job.run_id)
                if record and record.get('pid') == process.pid:
                    store.update(job.run_id, {'status':'stopped' if job.status=='cancelled' else job.status,
                                             'stop_reason':job.error})
            self._persist()

    def _collect(self, job: CompanionJob) -> list[Artifact]:
        root = self.jobs_dir / job.job_id / "library"
        artifacts: list[Artifact] = []
        for cbz in sorted(root.rglob("chapter-pages.cbz")):
            try:
                cbz.resolve().relative_to(root.resolve())
                if any(part.startswith('.') for part in cbz.relative_to(root).parts):
                    continue  # Valid-looking archives in staging are not committed chapters.
                with zipfile.ZipFile(cbz) as archive:
                    members = archive.infolist()
                    if not members or len(members) > 2000 or sum(m.file_size for m in members) > 1_000_000_000:
                        continue
                    if any(m.filename.startswith("/") or ".." in Path(m.filename).parts for m in members):
                        continue
                    if not any(Path(m.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"} for m in members):
                        continue
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
                continue
            chapter = cbz.parent.name
            series = cbz.parent.parent.name
            metadata_path = cbz.parent / "metadata.json"
            title = f"{series.replace('-', ' ').title()} · Chapter {chapter}"
            metadata = {}
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                title = str(metadata.get("title") or title)
            except (OSError, ValueError):
                continue
            expected=metadata.get('saved_image_count') or metadata.get('image_count')
            if not isinstance(expected,int) or expected != len(members) or not metadata.get('url') or metadata.get('identity_verified') is False:
                continue
            try:
                digest = self.library.verified_digest(cbz)
            except (ValueError, OSError):
                continue
            artifacts.append(
                Artifact(
                    artifact_id=uuid.uuid5(uuid.NAMESPACE_URL, f'{job.job_id}/{cbz.relative_to(root)}/{digest}').hex,
                    series=series,
                    chapter=chapter,
                    title=title,
                    filename=str(cbz.relative_to(root)),
                    size=cbz.stat().st_size,
                    sha256=digest,
                    source_url=str(metadata.get('url') or metadata.get('source_url') or ''),
                )
            )
        return artifacts

    def get_job(self, job_id: str) -> CompanionJob | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.run_id and job.status not in {'cancelled','cancelling'}:
                from panelyra.services.capture_store import CaptureStore
                record = CaptureStore(self.app_dir).read(job.run_id)
                if record:
                    job.pid = record.get('pid')
                    job.status = {'starting':'running','stopped':'cancelled'}.get(record['status'],record['status'])
                    job.error = record.get('stop_reason','')
            if job and job.status in {'running', 'cancelling','interrupted','failed'}:
                ready = self._collect(job)
                if ready != job.artifacts:
                    job.artifacts = ready
                    self._persist()
            if job and job.status == 'completed':
                job.artifacts = self._collect(job)
                if len(job.artifacts) != job.chapter_count:
                    job.status='failed';job.error='Capture checkpoint and retained artifact count do not agree; files were preserved.'
                self._persist()
            return job

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            process = self.processes.get(job_id)
            if job and job.run_id:
                from panelyra.services.capture_store import CaptureStore
                from panelyra.services.worker_runtime import worker_pid
                store=CaptureStore(self.app_dir)
                record=store.read(job.run_id)
                store.update(job.run_id, {'status':'stopped','stage':'stopped','stop_reason':'Cancelled from tablet.'})
                pid=worker_pid(self.app_dir,'capture')
                if record and pid == record.get('pid'):
                    try:os.kill(pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                job.status='cancelled';job.updated_at=time.time();self._persist()
                return True
            if not job or not process or job.status != "running":
                return False
            job.status = "cancelling"
            job.updated_at = time.time()
            self._persist()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        return True

    def interrupt_jobs(self):
        """Final checkpoint before the engine-owned listener exits."""
        from panelyra.services.worker_runtime import worker_pid
        with self.lock:
            self.accepting=False
            for job in self.jobs.values():
                if job.status not in {'queued','running','cancelling'}:continue
                job.status='cancelled' if job.status=='cancelling' else 'interrupted'
                job.error='The companion stopped; completed chapters were retained.'
                job.updated_at=time.time();job.artifacts=self._collect(job)
                if job.pid and worker_pid(self.app_dir,'capture')==job.pid:
                    try:os.kill(job.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
            self._persist()

    def artifact_path(self, job_id: str, artifact_id: str) -> Path | None:
        job = self.jobs.get(job_id)
        if not job or job.status not in {"completed", "failed", "cancelled", "interrupted"}:
            return None
        artifact = next((item for item in job.artifacts if item.artifact_id == artifact_id), None)
        if artifact is None:
            return None
        root = (self.jobs_dir / job_id / "library").resolve()
        candidate = (root / artifact.filename).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def log_tail(self, job_id: str, limit: int = 12_000) -> str:
        path = self.jobs_dir / job_id / "capture.log"
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - limit))
                return handle.read(limit).decode("utf-8", errors="replace")
        except OSError:
            return ""


class CompanionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: CompanionState):
        self.state = state
        super().__init__(address, CompanionHandler)

    def finish_request(self, request, client_address):
        # TLS handshakes belong to request threads, not the listening thread.
        request.settimeout(45)
        try:
            if isinstance(request, ssl.SSLSocket): request.do_handshake()
            super().finish_request(request, client_address)
        except (ssl.SSLError, TimeoutError, ConnectionResetError, BrokenPipeError):
            request.close()


class CompanionHandler(BaseHTTPRequestHandler):
    server: CompanionServer
    protocol_version = 'HTTP/1.1'

    def log_message(self, format: str, *args: object) -> None:
        print(f"[tablet] {self.address_string()} {format % args}")

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not 0 <= length <= 64_000:
            raise ValueError("Request body must be between 0 and 64000 bytes.")
        value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if not isinstance(value, dict):
            raise ValueError("JSON object required.")
        return value

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.server.state.authorized(self.headers.get("Authorization", "")):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Pairing required."})
        return False

    def _send_file(self, source: Path, content_type: str, digest: str | None = None) -> None:
        from panelyra.engine import policy
        if policy(self.server.state.app_dir):
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,{'error':'Background transfers are paused on the Mac. Resume them in Settings.'})
            return
        size = source.stat().st_size
        offset = 0
        range_header = self.headers.get("Range", "")
        if range_header and self.headers.get('If-Range', f'"{digest}"') != f'"{digest}"':
            range_header = ''
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-", range_header)
            if not match or int(match.group(1)) >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            offset = int(match.group(1))
        self.send_response(HTTPStatus.PARTIAL_CONTENT if offset else HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size - offset))
        self.send_header("Accept-Ranges", "bytes")
        if offset:
            self.send_header("Content-Range", f"bytes {offset}-{size - 1}/{size}")
        if digest:
            self.send_header("ETag", f'"{digest}"')
        self.end_headers()
        with source.open("rb") as handle:
            handle.seek(offset)
            try:
                while chunk := handle.read(256 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path == "/api/v1/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "paired": bool(self.server.state.device_token)})
            return
        if not self._authorized():
            return
        if path == '/api/v3/identity':
            self._send_json(HTTPStatus.OK, {'identity': certificate_fingerprint(), 'name': socket.gethostname(), 'version': '1.4.0'})
            return
        if path == '/api/v3/transfers/pending':
            self._send_json(HTTPStatus.OK, {'transfers': self.server.state.deliveries.list(pending=True, transport='pull')})
            return
        transfer_match = re.fullmatch(r'/api/v3/transfers/([a-f0-9]{32})', path)
        transfer_file = re.fullmatch(r'/api/v3/transfers/([a-f0-9]{32})/chapters/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)', path)
        transfer_cover = re.fullmatch(r'/api/v3/transfers/([a-f0-9]{32})/covers/([a-zA-Z0-9._-]+)', path)
        if transfer_match or transfer_file or transfer_cover:
            try:
                transfer_id = (transfer_match or transfer_file or transfer_cover).group(1)
                if self.server.state.deliveries.get(transfer_id).get('transport', 'pull') != 'pull':
                    raise ValueError('This transfer is managed by the Mac sender')
                if transfer_match:
                    value = self.server.state.deliveries.get(transfer_match.group(1))
                    self._send_json(HTTPStatus.OK, self.server.state.deliveries.public(value))
                else:
                    file, digest = self.server.state.deliveries.file(*(transfer_file or transfer_cover).groups())
                    self._send_file(file, 'application/vnd.comicbook+zip' if transfer_file else 'application/octet-stream', digest)
            except (OSError, ValueError, KeyError, StopIteration) as exc:
                self._send_json(HTTPStatus.CONFLICT, {'error': str(exc)})
            return
        if path == "/api/v2/library/sync-request":
            self._send_json(HTTPStatus.OK, {"generation": sync_request_generation()})
            return
        if path == "/api/v2/library":
            self._send_json(HTTPStatus.OK, self.server.state.library.catalog())
            return
        chapter_match = re.fullmatch(r"/api/v2/library/chapters/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)", path)
        if chapter_match:
            file = self.server.state.library.chapter_file(*chapter_match.groups())
            if file is None or not file.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Chapter not in the validated catalog."})
                return
            self._send_file(file, "application/vnd.comicbook+zip", self.server.state.library.verified_digest(file))
            return
        cover_match = re.fullmatch(r"/api/v2/library/covers/([a-zA-Z0-9._-]+)", path)
        if cover_match:
            file = self.server.state.library.cover_file(cover_match.group(1))
            if file is None or not file.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Cover not found."})
                return
            self._send_file(file, mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            return
        job_match = re.fullmatch(r"/api/v1/jobs/([a-f0-9]{32})", path)
        artifact_match = re.fullmatch(
            r"/api/v1/jobs/([a-f0-9]{32})/artifacts/([a-f0-9]{32})", path
        )
        if job_match:
            job = self.server.state.get_job(job_match.group(1))
            if job:
                self._send_json(HTTPStatus.OK, job.public(self.server.state.log_tail(job.job_id)))
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found."})
            return
        if artifact_match:
            path_value = self.server.state.artifact_path(*artifact_match.groups())
            if path_value is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Artifact not found."})
                return
            self._send_file(path_value, 'application/vnd.comicbook+zip', self.server.state.library.verified_digest(path_value))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})


    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path == "/api/v2/pair/start":
            try:
                payload = self._json_body()
                result = self.server.state.pair_start(str(payload.get("client_nonce", "")), str(payload.get("device_name", "")))
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/v2/pair/finish":
            try:
                payload = self._json_body()
                token = self.server.state.pair_finish(str(payload.get("client_nonce", "")), str(payload.get("server_nonce", "")), str(payload.get("proof", "")))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                token = None
            if not token:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Pairing proof rejected or expired."})
            else:
                self._send_json(HTTPStatus.OK, {"token": token, "device_name": self.server.state.device_name})
            return
        if path == "/api/v1/pair":
            try:
                payload = self._json_body()
                token = self.server.state.pair(
                    str(payload.get("pairing_secret", "")), str(payload.get("device_name", ""))
                )
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                token = None
            if not token:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Pairing code rejected."})
            else:
                self._send_json(HTTPStatus.OK, {"token": token, "device_name": self.server.state.device_name})
            return
        if not self._authorized():
            return
        if path == '/api/v3/tablet' or path == '/api/v3/transfers' or re.fullmatch(r'/api/v3/transfers/[a-f0-9]{32}/[a-z]+', path):
            try:
                body = self._json_body()
                if path == '/api/v3/tablet':
                    result = self.server.state.deliveries.tablet(body)
                elif path == '/api/v3/transfers':
                    result = self.server.state.deliveries.create(body.get('chapters', []), bool(body.get('all_missing')))
                else:
                    identity, action = path.split('/')[-2:]
                    if self.server.state.deliveries.get(identity).get('transport', 'pull') != 'pull':
                        raise ValueError('This transfer is managed by the Mac sender')
                    result = self.server.state.deliveries.update(identity, action, body,summary=True)
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {'error': str(exc)})
            return
        if path == "/api/v2/library/sync-status":
            try:
                payload = self._json_body()
                message = str(payload.get("message") or "").strip()[:300]
                if not message:
                    raise ValueError("A sync status message is required.")
                atomic_json(SYNC_STATUS_FILE, {"message": message, "updated_at": time.time(),
                                                    "device": self.server.state.device_name})
                self._send_json(HTTPStatus.OK, {"status": "recorded"})
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/v1/jobs":
            try:
                job = self.server.state.create_job(self._json_body())
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._send_json(HTTPStatus.ACCEPTED, job.public())
            return
        cancel_match = re.fullmatch(r"/api/v1/jobs/([a-f0-9]{32})/cancel", path)
        if cancel_match:
            if self.server.state.cancel(cancel_match.group(1)):
                self._send_json(HTTPStatus.ACCEPTED, {"status": "cancelling"})
            else:
                self._send_json(HTTPStatus.CONFLICT, {"error": "Job is not running."})
            return
        if path == "/api/v1/unpair":
            self.server.state.unpair()
            self._send_json(HTTPStatus.OK, {"status": "unpaired"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})


class PairChallengeHandler(BaseHTTPRequestHandler):
    """Transport for a code-authenticated certificate challenge only."""

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/v2/pair/start":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("Invalid request size.")
            payload = json.loads(self.rfile.read(length))
            result = self.server.state.pair_start(str(payload.get("client_nonce", "")), str(payload.get("device_name", "")))
            result["certificate_sha256"] = certificate_fingerprint()
            body = json.dumps(result).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_GET(self) -> None:  # noqa: N802
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        pass


class PairChallengeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: CompanionState):
        self.state = state
        super().__init__(address, PairChallengeHandler)


def pairing_payload(host: str, port: int, state: CompanionState) -> dict[str, str]:
    return {
        "version": "1",
        "base_url": f"https://{host}:{port}",
        "pairing_secret": state.pairing_secret,
        "certificate_sha256": certificate_fingerprint(),
    }


def install_login_agent() -> Path:
    label = "com.panelyra.tablet-companion"
    destination = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    log = STATE_DIR / "companion.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    program = {
        "Label": label,
        "ProgramArguments": [sys.executable, str(APP_DIR / "tablet_companion.py")],
        "WorkingDirectory": str(APP_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    temporary = destination.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(program))
    os.replace(temporary, destination)
    uid = os.getuid()
    existing = subprocess.run(["/bin/launchctl", "print", f"gui/{uid}/{label}"], capture_output=True)
    if existing.returncode == 0:
        return destination
    result = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(destination)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not start login companion")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Panelyra tablet companion")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--pairing-json", action="store_true", help="Print the QR payload and exit.")
    parser.add_argument("--pairing-qr", type=Path, help="Write the pairing QR as a PNG and exit.")
    parser.add_argument("--pairing-code", action="store_true", help="Print the short-lived code and address.")
    parser.add_argument("--install-login-agent", action="store_true", help="Start companion at Mac sign-in.")
    parser.add_argument("--usb-devices", action="store_true", help="List connected Android devices.")
    parser.add_argument("--approve-usb", metavar="SERIAL", help="Approve this connected USB tablet.")
    parser.add_argument("--usb-once", action="store_true", help="Copy missing chapters to approved USB tablet.")
    parser.add_argument("--request-wifi-sync", action="store_true", help="Ask the paired tablet to sync when available.")
    parser.add_argument('--no-usb-watch',action='store_true',help='Do not start an independent USB watcher (engine-owned LAN mode).')
    parser.add_argument('--bind',default='0.0.0.0',help='Local interface; use 127.0.0.1 for local tests.')
    parser.add_argument('--no-advertise',action='store_true',help='Do not advertise this listener on the LAN.')
    args = parser.parse_args(argv)
    if args.install_login_agent:
        print(install_login_agent())
        return 0
    if args.usb_devices:
        print(json.dumps({"devices": usb_sync.connected_devices(), "approved": usb_sync.approved_serial()}))
        return 0
    if args.approve_usb:
        usb_sync.approve(args.approve_usb)
        print(json.dumps({"approved": args.approve_usb}))
        return 0
    if args.usb_once:
        print(json.dumps(usb_sync.sync_once()))
        return 0
    if args.request_wifi_sync:
        print(json.dumps({"generation": request_wifi_sync()}))
        return 0
    host = local_ip()
    ensure_certificate(host)
    if args.pairing_code:
        code, expires = current_pair_code()
        try:
            saved = json.loads((STATE_DIR / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        print(json.dumps({"code": code, "base_url": f"https://{host}:{args.port}", "expires_at": expires,
                          "paired_device": saved.get("device_name", "")}))
        return 0
    state = CompanionState(app_dir=APP_DIR)
    payload = pairing_payload(host, args.port, state)
    if args.pairing_json:
        print(json.dumps(payload))
        return 0
    if args.pairing_qr:
        try:
            import qrcode
        except ImportError as exc:
            raise SystemExit("Install requirements.txt to generate QR images.") from exc
        args.pairing_qr.parent.mkdir(parents=True, exist_ok=True)
        qrcode.make(json.dumps(payload, separators=(",", ":"))).save(args.pairing_qr)
        print(args.pairing_qr)
        return 0
    server=None;challenge_server=None;challenge_thread=None;advertiser=None
    try:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(CERT_FILE, KEY_FILE)
        server=CompanionServer((args.bind,args.port),state)
        server.socket=context.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False)
        challenge_server=PairChallengeServer((args.bind,args.port+1 if args.port else 0),state)
        challenge_thread=threading.Thread(target=challenge_server.serve_forever,daemon=True);challenge_thread.start()
        if not args.no_usb_watch:threading.Thread(target=usb_sync.watch,daemon=True).start()
        if not args.no_advertise and Path('/usr/bin/dns-sd').is_file():
            try:
                advertiser=subprocess.Popen(['/usr/bin/dns-sd','-R',f"Panelyra {socket.gethostname().split('.')[0]}",'_panelyra._tcp','local',str(server.server_port),
                                             f'identity={certificate_fingerprint()}','version=1.4.0'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except OSError:pass
        print(f'Panelyra tablet companion: https://{args.bind if args.bind!="0.0.0.0" else host}:{server.server_port}',flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:server.server_close()
        if challenge_server is not None:
            if challenge_thread is not None and challenge_thread.is_alive():challenge_server.shutdown()
            challenge_server.server_close()
        if advertiser is not None and advertiser.poll() is None:
            advertiser.terminate()
            try:
                advertiser.wait(timeout=3)
            except subprocess.TimeoutExpired:advertiser.kill();advertiser.wait()
        state.interrupt_jobs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
