"""Assert EdgeMCP behavior against a local fixture, using disposable tabs only.

Run on the same machine as Edge (CDP_HOST/CDP_PORT, default 127.0.0.1:9222):
  python scripts/smoke_http.py --start-server
Or test an existing MCP server using MCP_URL (default http://127.0.0.1:8080/mcp).
The managed test server binds loopback on a free port and is stopped by PID.
Any failed tool or assertion exits nonzero; SMOKE OK is printed only on success.
"""
import argparse
import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(1.2)
            body = b"finished"
        elif self.path.startswith("/loading"):
            body = b'<!doctype html><title>Loading</title><body>Loading fixture<img src="/slow-image"></body>'
        else:
            name = "two" if self.path.startswith("/two") else "one"
            body = f'''<!doctype html><title>EdgeMCP {name}</title>
<body><h1>EdgeMCP fixture {name}</h1><a href="/two">next</a>
<iframe id="inner" srcdoc="<title>Child</title><p>Child fixture</p><input id='input'>"></iframe>
</body>'''.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def decoded(result):
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured["result"] if set(structured) == {"result"} else structured
    text = [b.text for b in result.content if b.type == "text"]
    if len(text) == 1:
        try:
            return json.loads(text[0])
        except json.JSONDecodeError:
            return text[0]
    return [json.loads(t) for t in text]


async def exercise(url, base):
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            wanted = {"wait_for_text", "wait_for_url", "wait_for_load_state", "reload",
                      "go_back", "go_forward", "new_tab", "close_tab"}
            assert wanted <= tools.keys(), wanted - tools.keys()

            async def call(name, args=None, error=False):
                result = await session.call_tool(name, args or {})
                failed = getattr(result, "is_error", False)
                assert failed == error, f"{name}: {result}"
                return decoded(result) if not error else result

            created = set()
            try:
                tab = await call("new_tab")
                created.add(tab["id"])
                assert tab["url"] == "about:blank"
                await call("select_tab", {"target_id": tab["id"]})
                assert not (await call("go_back"))["went_back"]
                assert not (await call("go_forward"))["went_forward"]

                await call("navigate", {"url": base + "/one"})
                for state in ("domcontentloaded", "load", "networkidle"):
                    assert (await call("wait_for_load_state", {"state": state, "timeout": 3000}))["reached"]
                assert (await call("wait_for_text", {"text": "EdgeMCP fixture one", "timeout": 0}))["found"]
                assert not (await call("wait_for_text", {"text": "missing", "timeout": 100}))["found"]
                # Existing iframes must work immediately after attaching to a tab too.
                await call("select_tab", {"target_id": tab["id"]})
                assert (await call("wait_for_text", {"text": "Child fixture", "frame": "#inner", "timeout": 2000}))["found"]
                assert await call("get_title", {"frame": 0}) == "Child"
                await call("type", {"selector": "#input", "text": "hello", "frame": "#inner"})
                assert await call("eval", {"expression": "document.querySelector('#input').value", "frame": "#inner"}) == "hello"
                print("PASS: readiness, text immediate/timeout, frame wait and existing contexts", flush=True)

                # Replacing the body must not leave wait_for_text observing a stale node.
                await call("eval", {"expression": "setTimeout(()=>{document.body.innerHTML='<p>delayed marker</p>'},150); null"})
                assert (await call("wait_for_text", {"text": "delayed marker", "timeout": 2000}))["found"]
                await call("eval", {"expression": "setTimeout(()=>{location.hash='ready'},150); null"})
                assert (await call("wait_for_url", {"pattern": r"#ready$", "timeout": 2000}))["matched"]
                assert not (await call("wait_for_url", {"pattern": "never-matches", "timeout": 100}))["matched"]
                await call("wait_for_url", {"pattern": "["}, error=True)
                await call("wait_for_text", {"text": "x", "timeout": -1}, error=True)
                await call("wait_for_load_state", {"state": "invalid"}, error=True)
                print("PASS: delayed text, URL changes, timeout and invalid inputs", flush=True)

                # Start a fetch BEFORE waiting, then prove networkidle counts it.
                await call("eval", {"expression": "fetch('/slow').then(r=>r.text()).then(()=>window.fetchDone=true); null"})
                start = time.monotonic()
                assert not (await call("wait_for_load_state", {"state": "networkidle", "timeout": 100}))["reached"]
                assert (await call("wait_for_load_state", {"state": "networkidle", "timeout": 4000}))["reached"]
                assert time.monotonic() - start >= 1.0
                assert await call("eval", {"expression": "window.fetchDone"}) is True
                print("PASS: networkidle waits for pre-existing request and idle window", flush=True)

                await call("navigate", {"url": base + "/two"})
                assert (await call("go_back"))["went_back"]
                assert (await call("wait_for_url", {"pattern": r"/one#ready$", "timeout": 3000}))["matched"]
                assert (await call("go_forward"))["went_forward"]
                assert (await call("wait_for_url", {"pattern": r"/two$", "timeout": 3000}))["matched"]
                assert not (await call("go_forward"))["went_forward"]
                await call("eval", {"expression": "document.body.innerHTML='<p>modified</p>'"})
                assert (await call("reload", {"ignore_cache": True}))["reloaded"]
                assert (await call("wait_for_text", {"text": "EdgeMCP fixture two", "timeout": 4000}))["found"]
                print("PASS: real back/forward roundtrip, history boundary, reload", flush=True)

                # DCL can be reached while a subresource still delays window.load.
                await call("eval", {"expression": "setTimeout(()=>location.assign('/loading'),0); null"})
                assert (await call("wait_for_url", {"pattern": r"/loading$", "timeout": 3000}))["matched"]
                assert (await call("wait_for_load_state", {"state": "domcontentloaded", "timeout": 1000}))["reached"]
                assert not (await call("wait_for_load_state", {"state": "load", "timeout": 100}))["reached"]
                assert (await call("wait_for_load_state", {"state": "load", "timeout": 3000}))["reached"]
                print("PASS: DOMContentLoaded vs delayed load and load timeout", flush=True)

                other = await call("new_tab", {"url": base + "/one"})
                created.add(other["id"])
                assert (await call("get_active_tab"))["id"] == tab["id"]
                assert (await call("close_tab", {"target_id": other["id"]}))["closed"] == other["id"]
                created.remove(other["id"])
                await call("close_tab", {"target_id": "nonexistent-target"}, error=True)
                shot = await session.call_tool("screenshot", {"full_page": True})
                assert not shot.is_error, shot
                image = next(b for b in shot.content if b.type == "image")
                data = base64.b64decode(image.data)
                assert data.startswith(b"\x89PNG\r\n\x1a\n")
                (ROOT / "smoke.png").write_bytes(data)
                assert (await call("close_tab"))["closed"] == tab["id"]
                created.remove(tab["id"])
                remaining = await call("list_tabs")
                assert all(p["id"] not in (tab["id"], other["id"]) for p in remaining)
                print("PASS: new/close tabs, selection preservation, screenshot", flush=True)
            finally:
                for tid in created:
                    await session.call_tool("close_tab", {"target_id": tid})
    print("SMOKE OK (assertions passed)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-server", action="store_true")
    args = parser.parse_args()
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    process = None
    log = None
    try:
        url = os.environ.get("MCP_URL", "http://127.0.0.1:8080/mcp")
        if args.start_server:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            log = (ROOT / "smoke-server.log").open("w", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "src/server.py"),
                 "--cdp", f"{os.environ.get('CDP_HOST', '127.0.0.1')}:{os.environ.get('CDP_PORT', '9222')}",
                 "--listen", f"127.0.0.1:{port}"], cwd=ROOT, stdout=log, stderr=log,
            )
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Test server exited; see smoke-server.log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Test server did not start; see smoke-server.log")
                    time.sleep(0.1)
            url = f"http://127.0.0.1:{port}/mcp"
        asyncio.run(exercise(url, f"http://127.0.0.1:{fixture.server_port}"))
    finally:
        if process is not None:
            process.terminate()  # only the server started by THIS script
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log:
            log.close()
        fixture.shutdown()
        fixture.server_close()


if __name__ == "__main__":
    main()
