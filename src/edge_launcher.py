"""Launch a local, dedicated Edge instance on a specific CDP port.

No shell commands, credentials, or account-specific paths are embedded here.
Browser processes intentionally outlive the MCP server. A lock and retained
launch attempts prevent duplicate launches on retries within this process.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_LOCK = threading.Lock()
_ATTEMPTS: dict[int, subprocess.Popen] = {}
_READY: set[int] = set()


def _validate_endpoint(host: str, port: str | int) -> int:
    if host.lower() not in ("127.0.0.1", "localhost"):
        raise ValueError(
            "Launching Edge requires --cdp 127.0.0.1:PORT or localhost:PORT. "
            "It cannot launch a browser on a remote host."
        )
    if isinstance(port, bool) or not str(port).isascii() or not str(port).isdecimal():
        raise ValueError("CDP port must be an integer from 1 to 65535")
    value = int(port)
    if not 1 <= value <= 65535:
        raise ValueError("CDP port must be an integer from 1 to 65535")
    return value


def _find_edge() -> Path:
    configured = os.environ.get("EDGE_PATH")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError("EDGE_PATH does not point to an existing Edge executable")
        if sys.platform != "win32" and path.suffix.lower() == ".exe":
            raise RuntimeError("Run the MCP server with Windows Python to launch Windows Edge")
        if sys.platform != "win32" and not os.access(path, os.X_OK):
            raise RuntimeError("EDGE_PATH is not executable")
        return path.resolve()

    candidates = []
    if sys.platform == "win32":
        for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    elif sys.platform == "darwin":
        candidates = [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    commands = ("msedge.exe",) if sys.platform == "win32" else ("microsoft-edge", "microsoft-edge-stable")
    for command in commands:
        found = shutil.which(command)
        if found:
            return Path(found).resolve()
    raise RuntimeError(
        "Microsoft Edge was not found. Install Edge or set EDGE_PATH. "
        "For Windows Edge, run the MCP server with Windows Python, not WSL Python."
    )


def _profile_path(port: int) -> Path:
    configured = os.environ.get("EDGE_PROFILE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return (base / "EdgeMCP" / f"debug-profile-{port}").resolve()


def _probe_edge(port: int, timeout: float = 1) -> str | None:
    """None means no listener. Refuse to reuse non-Edge/invalid CDP listeners."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        return None
    except OSError as exc:
        raise RuntimeError(f"Cannot check local CDP port {port}: {exc}") from exc

    # Do not send local CDP discovery through a configured proxy or redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            version = json.loads(response.read(65536)).get("Browser", "")
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError(f"Port {port} is occupied but is not a ready Edge CDP endpoint") from exc
    if not isinstance(version, str) or not version.startswith(("Edg/", "Microsoft Edge/")):
        raise RuntimeError(f"Port {port} is occupied by a non-Edge service; choose another --cdp port")
    return version


def launch(host: str, port: str | int, *, timeout: float = 15) -> dict:
    """Ensure a local Edge CDP listener exists; reuse it if already available."""
    port = _validate_endpoint(host, port)
    if timeout <= 0:
        raise ValueError("Launch timeout must be positive")
    endpoint = f"http://127.0.0.1:{port}"
    with _LOCK:
        try:
            version = _probe_edge(port)
        except RuntimeError:
            if port not in _ATTEMPTS or port in _READY:
                raise  # do not spawn into an unrelated occupied port
            version = None  # our pending launch may still be initializing
        if version:
            if port in _ATTEMPTS:
                _READY.add(port)
            return {"started": False, "already_running": True, "cdp_url": endpoint, "browser": version}
        if port in _READY:
            # A previously ready browser is now gone: allow an intentional restart.
            _READY.discard(port)
            previous = _ATTEMPTS.pop(port, None)
            if previous:
                previous.poll()

        process = _ATTEMPTS.get(port)
        started = process is None
        if process is None:
            executable = _find_edge()
            profile = _profile_path(port)
            profile.mkdir(parents=True, exist_ok=True)
            command = [
                str(executable), f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "about:blank",
            ]
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, shell=False)
            if sys.platform == "win32":
                options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **options)
            except OSError as exc:
                raise RuntimeError(f"Could not start Edge: {exc}") from exc
            _ATTEMPTS[port] = process

        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                version = _probe_edge(port, timeout=min(1, remaining / 2))
                if version:
                    _READY.add(port)
                    return {"started": started, "already_running": False, "cdp_url": endpoint,
                            "browser": version, "launch_pid": process.pid}
            except RuntimeError as exc:
                # Edge can accept TCP before its HTTP discovery endpoint is ready.
                last_error = str(exc)
            code = process.poll()
            if code not in (None, 0):
                _ATTEMPTS.pop(port, None)
                raise RuntimeError(f"Edge exited with code {code} before CDP became ready on port {port}")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        # Do not kill browsers or blindly start duplicates on a retry. Some Edge
        # launchers exit after delegating to a separate browser process.
        detail = f" Last probe: {last_error}." if last_error else ""
        raise RuntimeError(
            f"Edge CDP did not become ready at {endpoint} within {timeout:g}s. "
            f"Launch PID: {process.pid}. Check browser policies/profile conflicts. "
            "The launch may still be running; retries in this server will wait for it, "
            f"not spawn another instance.{detail}"
        )
