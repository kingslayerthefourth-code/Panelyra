"""Approved-device USB copy into Panelyra's validated Android import inbox."""

from __future__ import annotations

import json
import fcntl
import os
import sys
import re
import subprocess
import time
from pathlib import Path

from sync_library import OriginalLibrary


APP_DIR = Path(__file__).resolve().parent
ADB = APP_DIR / "tools" / "platform-tools" / "adb"
APPROVAL = APP_DIR / "data" / "tablet_companion" / "approved-usb.json"
USB_STATUS = APPROVAL.parent / "usb-sync-status.json"
USB_LOCK = APPROVAL.parent / "usb-sync.lock"
REMOTE = "/sdcard/Android/data/com.panelyra.tablet/files/Panelyra"


def _adb(*args: str, serial: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    if not ADB.is_file():
        raise RuntimeError("Bundled Android platform tools are missing.")
    command = [str(ADB)] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def connected_devices() -> list[dict[str, str]]:
    result = _adb("devices", "-l", timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ADB could not list tablets.")
    devices = []
    for line in result.stdout.splitlines()[1:]:
        match = re.match(r"^([^\s]+)\s+(device|unauthorized|offline)\b(.*)$", line)
        if match:
            devices.append({"serial": match.group(1), "status": match.group(2), "details": match.group(3).strip()})
    # ADB cannot see a charging/MTP-only Huawei, but macOS can. Report the
    # actual cable state so the UI can explain the missing debugging step.
    if sys.platform == "darwin":
        try:
            hardware = subprocess.run(
                ["/usr/sbin/ioreg", "-p", "IOUSB", "-c", "IOUSBHostDevice", "-l", "-w0"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            for block in hardware.stdout.split("+-o ")[1:]:
                if not re.search(r'"USB Vendor Name"\s*=\s*"HUAWEI"', block, re.IGNORECASE):
                    continue
                serial = re.search(r'"USB Serial Number"\s*=\s*"([^"]+)"', block)
                product = re.search(r'"USB Product Name"\s*=\s*"([^"]+)"', block)
                identity = serial.group(1) if serial else "Huawei tablet"
                if not any(device["serial"] == identity for device in devices):
                    devices.append({"serial": identity, "status": "cable_only",
                                    "details": product.group(1) if product else "HUAWEI USB device"})
        except (OSError, subprocess.TimeoutExpired):
            pass
    return devices


def approved_serial() -> str:
    try:
        return str(json.loads(APPROVAL.read_text(encoding="utf-8")).get("serial") or "")
    except (OSError, ValueError):
        return ""


def approve(serial: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9._:-]{4,100}", serial):
        raise ValueError("Invalid USB device serial.")
    if not any(item["serial"] == serial and item["status"] == "device" for item in connected_devices()):
        raise ValueError("Select a connected tablet after approving USB debugging on its screen.")
    APPROVAL.parent.mkdir(parents=True, exist_ok=True)
    temporary = APPROVAL.with_suffix(".tmp")
    temporary.write_text(json.dumps({"serial": serial}), encoding="utf-8")
    temporary.replace(APPROVAL)


def _status(**fields: object) -> None:
    USB_STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = USB_STATUS.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"updated_at": time.time(), **fields}), encoding="utf-8")
    os.replace(temporary, USB_STATUS)


def sync_once() -> dict:
    USB_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with USB_LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "USB transfer already running", "copied": 0}
        try:
            return _sync_once_unlocked()
        except Exception as exc:
            _status(status="failed", error=str(exc))
            raise


def _sync_once_unlocked() -> dict:
    serial = approved_serial()
    if not serial:
        _status(status="waiting", detail="No USB tablet approved", copied=0)
        return {"status": "No USB tablet approved", "copied": 0}
    if not any(item["serial"] == serial and item["status"] == "device" for item in connected_devices()):
        _status(status="waiting", detail="Approved tablet disconnected", copied=0)
        return {"status": "Approved tablet disconnected", "copied": 0}
    library=OriginalLibrary(APP_DIR)
    catalog=library.catalog()
    index_result = _adb("shell", "cat", f"{REMOTE}/sync-index.json", serial=serial, timeout=15)
    try:
        index = json.loads(index_result.stdout) if index_result.returncode == 0 else {}
    except ValueError:
        index = {}
    _adb("shell", "mkdir", "-p", f"{REMOTE}/inbox", serial=serial, timeout=15)
    entries = []
    missing = [item for item in catalog["chapters"] if index.get(f"{item['series_key']}/{item['chapter']}") != item["sha256"]]
    _status(status="copying", copied=0, total=len(missing), detail="Preparing USB transfer")
    for item in missing:
        key = f"{item['series_key']}/{item['chapter']}"
        name = f"{item['series_key']}__{item['chapter']}.cbz"
        source=library.chapter_file(item['series_key'],item['chapter'])
        if not source.is_file() or source.stat().st_size!=item['size'] or library.verified_digest(source)!=item['sha256']:
            raise RuntimeError(f'USB source changed or is missing: {key}. Nothing was marked complete.')
        remote_file = f"{REMOTE}/inbox/{name}"
        result = _adb("push", str(source), remote_file + ".partial", serial=serial, timeout=600)
        if result.returncode:
            raise RuntimeError(f"USB copy failed at {key}: {result.stderr.strip()}")
        result = _adb("shell", "mv", remote_file + ".partial", remote_file, serial=serial, timeout=15)
        if result.returncode:
            raise RuntimeError(f"USB finalization failed at {key}.")
        entries.append({**item, "filename": name})
        _status(status="copying", copied=len(entries), total=len(missing), detail=f"Copied {key}")
    if entries:
        manifest = APPROVAL.parent / "usb-manifest.json"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        result = _adb("push", str(manifest), f"{REMOTE}/inbox/usb-manifest.json.partial", serial=serial, timeout=60)
        if result.returncode:
            raise RuntimeError("Could not send USB import manifest.")
        result = _adb("shell", "mv", f"{REMOTE}/inbox/usb-manifest.json.partial",
                      f"{REMOTE}/inbox/usb-manifest.json", serial=serial, timeout=15)
        if result.returncode:
            raise RuntimeError("Could not finalize USB import manifest.")
        _adb("shell", "am", "start", "-n", "com.panelyra.tablet/.MainActivity", serial=serial, timeout=15)
    _status(status="complete", copied=len(entries), total=len(missing), detail="Tablet import can now validate these chapters")
    return {"status": "USB copy complete", "copied": len(entries), "invalid": catalog["errors"]}


def watch() -> None:
    last_serial = ""
    last_sync = 0.0
    while True:
        try:
            serial = approved_serial()
            attached = serial and any(item["serial"] == serial and item["status"] == "device" for item in connected_devices())
            if attached and (serial != last_serial or time.time() - last_sync > 900):
                sync_once()
                last_sync = time.time()
            last_serial = serial if attached else ""
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"[usb] {exc}", flush=True)
        time.sleep(20)
