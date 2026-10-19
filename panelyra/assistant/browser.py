"""Optional read-only Chrome DevTools inspection for assistant diagnostics."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import websocket


DEFAULT_DEBUG_PORT = 9222


def list_debug_tabs(timeout: float = 2.0, *, debug_port: int = DEFAULT_DEBUG_PORT) -> list[dict[str, Any]]:
    try:
        with urlopen(f"http://127.0.0.1:{int(debug_port)}/json/list", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        dict(item)
        for item in payload
        if isinstance(item, dict) and item.get("type") == "page"
    ]


def choose_tab(tabs: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        tab
        for tab in tabs
        if not str(tab.get("url", "")).startswith(("chrome://", "devtools://"))
        and "localhost:8517" not in str(tab.get("url", ""))
    ]
    return (candidates or tabs or [{}])[0]


def devtools_command(socket: websocket.WebSocket, command_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    socket.send(json.dumps({"id": command_id, "method": method, "params": params}))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        payload = json.loads(socket.recv())
        if payload.get("id") == command_id:
            return payload
    raise TimeoutError(f"Chrome DevTools command timed out: {method}")


def inspect_chrome_page(
    *,
    include_screenshot: bool = False,
    screenshot_dir: Path | None = None,
    debug_port: int = DEFAULT_DEBUG_PORT,
) -> dict[str, Any]:
    tab = choose_tab(list_debug_tabs(debug_port=debug_port))
    websocket_url = str(tab.get("webSocketDebuggerUrl", ""))
    if not websocket_url:
        return {"available": False, "error": f"No inspectable Chrome tab is available on port {int(debug_port)}."}

    socket = websocket.create_connection(
        websocket_url,
        timeout=6,
        suppress_origin=True,
    )
    try:
        expression = """
        (() => {
          const visible = (node) => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const summarize = (selector, limit = 30) => Array.from(document.querySelectorAll(selector))
            .filter(visible)
            .slice(0, limit)
            .map((node) => ({
              tag: node.tagName.toLowerCase(),
              text: (node.innerText || node.textContent || node.value || "").trim().slice(0, 180),
              id: node.id || "",
              name: node.getAttribute("name") || "",
              aria: node.getAttribute("aria-label") || "",
              placeholder: node.getAttribute("placeholder") || "",
              type: node.getAttribute("type") || ""
            }));
          return {
            title: document.title,
            url: location.href,
            text_excerpt: (document.body?.innerText || "").trim().slice(0, 5000),
            buttons: summarize("button, [role='button'], input[type='submit']"),
            inputs: summarize("input, textarea, [contenteditable='true'], [role='textbox']"),
            links: summarize("a[href]", 20)
          };
        })()
        """
        evaluated = devtools_command(
            socket,
            1,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        value = (
            evaluated.get("result", {})
            .get("result", {})
            .get("value", {})
        )
        snapshot = dict(value) if isinstance(value, dict) else {}
        snapshot["available"] = bool(snapshot)
        snapshot["debug_port"] = int(debug_port)
        if include_screenshot:
            screenshot = devtools_command(
                socket,
                2,
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": False},
            )
            encoded = screenshot.get("result", {}).get("data")
            if encoded:
                target_dir = screenshot_dir or Path.cwd() / "outputs" / "assistant"
                target_dir.mkdir(parents=True, exist_ok=True)
                path = target_dir / f"browser-{time.strftime('%Y%m%d-%H%M%S')}.png"
                path.write_bytes(base64.b64decode(encoded))
                snapshot["screenshot_path"] = str(path)
        return snapshot
    except Exception as exc:
        return {
            "available": False,
            "title": str(tab.get("title", "")),
            "url": str(tab.get("url", "")),
            "error": str(exc),
        }
    finally:
        socket.close()
