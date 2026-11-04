"""Herdr test doubles: a fake socket server, a fake ``herdr`` CLI and recording CLIs."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
from typing import Any

PLUGIN_ID = "fluzzy.herdr-quotaribbon"


def short_tempdir() -> tempfile.TemporaryDirectory:
    """A temp dir short enough for a Unix socket path (macOS limit: 104 bytes)."""
    base = tempfile.gettempdir()
    if len(base) > 60 and os.path.isdir("/tmp"):
        base = "/tmp"
    return tempfile.TemporaryDirectory(prefix="qr", dir=base)


def plugin_entry(plugin_root: str, *, enabled: bool = True, plugin_id: str = PLUGIN_ID) -> dict:
    """A ``plugin.list`` entry shaped like Herdr 0.9.5's answer for a linked plugin."""
    return {
        "plugin_id": plugin_id,
        "name": "QuotaRibbon",
        "version": "0.1.0",
        "enabled": enabled,
        "plugin_root": plugin_root,
        "manifest_path": os.path.join(plugin_root, "herdr-plugin.toml"),
        "source": {"kind": "local"},
        "actions": [],
        "panes": [],
        "platforms": ["macos"],
    }


def integration(target: str, state: str) -> dict:
    return {"target": target, "label": target, "command": target, "available": state != "not_installed",
            "state": state}


def agent(kind: str, pane: str = "w1:p1") -> dict:
    return {"terminal_id": "term_x", "agent": kind, "agent_status": "idle", "pane_id": pane,
            "workspace_id": pane.split(":")[0], "tab_id": pane.split(":")[0] + ":t1", "focused": False,
            "agent_session": {"source": f"herdr:{kind}", "agent": kind, "kind": "id", "value": "x"}}


class FakeHerdrServer:
    """Threaded Unix-socket server answering Herdr API requests from its attributes.

    ``hang=True`` never answers; ``errors`` maps a method to ``(code, message)``;
    ``requests`` records every parsed request.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.plugins: list[dict] = []
        self.integrations: list[dict] = [integration("claude", "not_installed"), integration("codex", "not_installed")]
        self.agents: list[dict] = []
        self.invoke_result: dict | None = None
        self.reload_status = "applied"
        self.reload_diagnostics: list[str] = []
        self.errors: dict[str, tuple[str, str]] = {}
        self.hang = False
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "FakeHerdrServer":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            # Wake the blocking accept() so teardown does not wait for its timeout.
            with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                wake.settimeout(0.5)
                wake.connect(self.socket_path)
            self._thread.join(2)
        if self._sock:
            self._sock.close()
        with contextlib.suppress(OSError):
            os.unlink(self.socket_path)

    def __enter__(self) -> "FakeHerdrServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- helpers -------------------------------------------------------------

    def methods(self) -> list[str]:
        with self._lock:
            return [r.get("method") for r in self.requests]

    def requests_for(self, method: str) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r.get("method") == method]

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0])
            with self._lock:
                self.requests.append(req)
            if self.hang:
                self._stop.wait(10)
                return
            answer = self._answer(req)
            conn.sendall(json.dumps(answer).encode("utf-8") + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def _answer(self, req: dict) -> dict:
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if method in self.errors:
            code, msg = self.errors[method]
            return {"id": rid, "error": {"code": code, "message": msg}}
        if method == "plugin.list":
            pid = params.get("plugin_id")
            plugins = [p for p in self.plugins if pid is None or p.get("plugin_id") == pid]
            return {"id": rid, "result": {"type": "plugin_list", "plugins": plugins}}
        if method == "integration.list":
            return {"id": rid, "result": {"type": "integration_list", "integrations": self.integrations}}
        if method == "agent.list":
            return {"id": rid, "result": {"type": "agent_list", "agents": self.agents}}
        if method == "plugin.action.invoke":
            result = self.invoke_result or {
                "type": "plugin_action_invoked",
                "action": {"plugin_id": params.get("plugin_id"), "action_id": params.get("action_id")},
                "context": params.get("context") or {},
                "log": {"log_id": "plugin-log-1", "status": "running", "command": ["/bin/sh", "bin/quotaribbon"]},
            }
            return {"id": rid, "result": result}
        if method == "server.reload_config":
            return {"id": rid, "result": {"type": "config_reload", "status": self.reload_status,
                                          "diagnostics": self.reload_diagnostics}}
        return {"id": rid, "error": {"code": "invalid_request", "message": "unknown method"}}


_FAKE_HERDR_CLI = r'''#!{python}
import json, os, sys
spec = json.load(open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "herdr-spec.json")))
log = spec.get("log")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "claude": os.environ.get("CLAUDE_CONFIG_DIR"),
                              "codex": os.environ.get("CODEX_HOME")}}) + "\n")
args = sys.argv[1:]
if args == ["--version"]:
    print("herdr " + spec.get("version", "0.9.5")); sys.exit(0)
if args == ["--help"]:
    print("herdr - fake\n\nConfig: " + spec.get("config_path", "/nonexistent/config.toml")); sys.exit(0)
if args[:2] == ["integration", "status"]:
    sys.stdout.write(spec.get("integration_status", "")); sys.exit(spec.get("integration_exit", 0))
if args[:3] == ["plugin", "pane", "open"]:
    err = spec.get("pane_error")
    if err:
        print(json.dumps({{"error": {{"code": err, "message": "x"}}, "id": "cli:plugin"}})); sys.exit(1)
    print(json.dumps({{"id": "cli:plugin", "result": {{"type": "ok"}}}})); sys.exit(0)
if args[:2] == ["server", "reload-config"]:
    print(json.dumps({{"id": "cli", "result": {{"type": "config_reload", "status": spec.get("reload", "applied"),
                                                "diagnostics": []}}}})); sys.exit(0)
sys.exit(2)
'''


def cached_executable(name: str, content: str) -> str:
    """An executable with ``content``, written once and reused across tests.

    macOS scans each new executable on its first run (~0.4 s), so tests share one file.
    """
    import hashlib

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    cache = os.path.join(tempfile.gettempdir(), f"quotaribbon-test-exec-{os.getuid()}")
    os.makedirs(cache, mode=0o700, exist_ok=True)
    path = os.path.join(cache, f"{name}-{digest}")
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            fh.write(content)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    return path


def link_executable(directory: str, name: str, content: str) -> str:
    """``directory/name`` → a cached executable with ``content``; returns the link path."""
    os.makedirs(directory, exist_ok=True)
    link = os.path.join(directory, name)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(link)
    os.symlink(cached_executable(name, content), link)
    return link


def write_fake_herdr_cli(directory: str, **spec: Any) -> str:
    """Put a fake ``herdr`` into ``directory``; it reads ``spec`` from ``herdr-spec.json``.

    ``spec`` keys: ``version``, ``config_path``, ``integration_status``, ``integration_exit``,
    ``pane_error``, ``reload``, ``log``.
    """
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "herdr-spec.json"), "w") as fh:
        json.dump(spec, fh)
    return link_executable(directory, "herdr", _FAKE_HERDR_CLI.format(python=os.path.realpath(sys.executable)))


_RECORDING = """#!/bin/sh
d=$(dirname "$0")
echo "$0 $*" >> "$(cat "$d/.record-$(basename "$0")")"
exit 0
"""


def write_recording_executable(directory: str, name: str, record: str) -> str:
    """A fake CLI that appends its argv to ``record`` and exits 0."""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f".record-{name}"), "w") as fh:
        fh.write(record)
    return link_executable(directory, name, _RECORDING)


__all__ = [
    "PLUGIN_ID",
    "FakeHerdrServer",
    "agent",
    "integration",
    "plugin_entry",
    "short_tempdir",
    "cached_executable",
    "link_executable",
    "write_fake_herdr_cli",
    "write_recording_executable",
]
