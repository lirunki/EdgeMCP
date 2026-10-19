#!/usr/bin/env python3
"""EdgeMCP — MCP server (Streamable HTTP) that drives Microsoft Edge over CDP.

Exposes an HTTP endpoint (default http://127.0.0.1:8080/mcp) that an MCP
client/agent can reach. The server talks to an Edge instance launched with
--remote-debugging-port=PORT (default 127.0.0.1:9222) via the Chrome DevTools
Protocol, and exposes browser-control tools (navigate, eval, click, screenshot…).

Usage:
  python src/server.py --cdp 127.0.0.1:9222 --listen 127.0.0.1:8080 [--launch-edge] [--path /mcp]

Flags (env fallbacks shown):
  --cdp HOST:PORT     Edge remote-debugging address    (CDP_HOST/CDP_PORT, 127.0.0.1:9222)
  --listen HOST:PORT  MCP HTTP bind address             (MCP_HOST/MCP_PORT, 127.0.0.1:8080)
  --path PATH         MCP Streamable-HTTP endpoint path (MCP_PATH, /mcp)
  --launch-edge       launch local Edge on the configured CDP port before serving

Many tools accept an optional `frame` argument to operate inside an <iframe>:
  frame=None        the main/top-level document (default)
  frame=<int>       0-based index into document.querySelectorAll('iframe')
  frame="<selector>" CSS selector matching the <iframe> element
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Literal

import pychrome
import edge_launcher
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

# Defaults from env; overridden by CLI flags in main().
CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
CDP_PORT = os.environ.get("CDP_PORT", "9222")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

mcp = MCPServer("edgemcp")

# contexts: frameId -> default execution contextId (for Runtime.evaluate in a frame)
# ctx_to_frame: contextId -> frameId (for cleanup on destroy)
_state: dict[str, Any] = {
    "browser": None,
    "tab": None,
    "contexts": {},
    "ctx_to_frame": {},
    "network": None,
}


class _NetworkTracker:
    """Track requests from tab attachment, not just from the start of a wait."""

    def __init__(self):
        self.pending: set[str] = set()
        self.changed_at = time.monotonic()
        self.lock = threading.Lock()

    def request(self, requestId: str, **payload):
        # WebSocket lifetime is not an HTTP request. Long-poll/SSE requests count.
        if payload.get("type") == "WebSocket":
            return
        with self.lock:
            self.pending.add(requestId)  # redirects reuse the same request ID
            self.changed_at = time.monotonic()

    def finished(self, requestId: str, **_payload):
        with self.lock:
            self.pending.discard(requestId)
            self.changed_at = time.monotonic()

    def idle(self) -> bool:
        with self.lock:
            return not self.pending and time.monotonic() - self.changed_at >= 0.5



def _browser() -> pychrome.Browser:
    if _state["browser"] is None:
        _state["browser"] = pychrome.Browser(url=f"http://{CDP_HOST}:{CDP_PORT}")
    return _state["browser"]


def _http_json(path: str) -> Any:
    with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}{path}", timeout=5) as r:
        return json.load(r)


def _list_pages() -> list[dict]:
    data = _http_json("/json")
    return [
        {"id": d["id"], "url": d.get("url", ""), "title": d.get("title", "")}
        for d in data
        if d.get("type") == "page"
    ]


def _find_tab_by_id(target_id: str) -> pychrome.Tab:
    for t in _browser().list_tab():
        if t.id == target_id:
            return t
    raise RuntimeError(f"No page target with id {target_id}")


def _ensure_tab() -> pychrome.Tab:
    if _state["tab"] is not None:
        try:
            _state["tab"].Runtime.evaluate(expression="1", _timeout=3)  # sanity ping
            return _state["tab"]
        except Exception:
            try:
                _state["tab"].stop()
            except Exception:
                pass
            _state["tab"] = None
    pages = _list_pages()
    if not pages:
        raise RuntimeError(
            f"No page target at {CDP_HOST}:{CDP_PORT}. Launch Edge with "
            f"--remote-debugging-port={CDP_PORT}."
        )
    return _select(pages[0]["id"])


def _select(target_id: str) -> pychrome.Tab:
    if _state["tab"] is not None:
        try:
            _state["tab"].stop()
        except Exception:
            pass
        _state["tab"] = None
    _state["contexts"] = {}
    _state["ctx_to_frame"] = {}
    tab = _find_tab_by_id(target_id)
    tab.start()

    # Capture the dictionaries locally so events from an old tab cannot change
    # a newly selected tab's context map.
    contexts = _state["contexts"]
    reverse = _state["ctx_to_frame"]

    # Register listeners BEFORE Runtime.enable, which emits existing contexts.
    def _on_ctx_created(**payload):
        ctx = payload.get("context") or {}
        aux = ctx.get("auxData") or {}
        fid = aux.get("frameId")
        cid = ctx.get("id")
        if fid and cid and aux.get("isDefault"):
            contexts[fid] = cid
            reverse[cid] = fid

    def _on_ctx_destroyed(**payload):
        cid = payload.get("executionContextId")
        fid = reverse.pop(cid, None)
        if fid is not None and contexts.get(fid) == cid:
            contexts.pop(fid, None)

    def _on_ctxs_cleared(**_payload):
        contexts.clear()
        reverse.clear()

    tab.Runtime.executionContextCreated = _on_ctx_created
    tab.Runtime.executionContextDestroyed = _on_ctx_destroyed
    tab.Runtime.executionContextsCleared = _on_ctxs_cleared
    network = _NetworkTracker()
    tab.Network.requestWillBeSent = network.request
    tab.Network.loadingFinished = network.finished
    tab.Network.loadingFailed = network.finished
    try:
        tab.Page.enable(_timeout=5)
        tab.Runtime.enable(_timeout=5)
        tab.DOM.enable(_timeout=5)
        tab.Network.enable(_timeout=5)
    except Exception:
        tab.stop()
        raise

    _state["network"] = network
    _state["tab"] = tab
    return tab


# --- Frame resolution --------------------------------------------------------


def _resolve_frame_id(frame: Any) -> str | None:
    """Resolve a `frame` argument to a CDP frameId.

    frame may be:
      - None  -> None (use the main/default execution context)
      - an int, or a digit string -> 0-based index into document.querySelectorAll('iframe')
      - any other string -> a CSS selector matching the <iframe> element
    """
    if frame is None:
        return None
    tab = _ensure_tab()
    root = tab.DOM.getDocument(depth=0)
    root_id = root["root"]["nodeId"]
    if isinstance(frame, int) or (isinstance(frame, str) and frame.isdigit()):
        idx = int(frame)
        res = tab.DOM.querySelectorAll(nodeId=root_id, selector="iframe")
        node_ids = res.get("nodeIds") if isinstance(res, dict) else res
        if isinstance(node_ids, int):
            node_ids = [node_ids]
        node_ids = node_ids or []
        if idx < 0 or idx >= len(node_ids):
            raise RuntimeError(f"No <iframe> at index {idx} (found {len(node_ids)})")
        nid = node_ids[idx]
    else:
        res = tab.DOM.querySelector(nodeId=root_id, selector=str(frame))
        nid = res.get("nodeId") if isinstance(res, dict) else res
        if not nid:
            raise RuntimeError(f"No <iframe> matches selector {frame!r}")
    desc = tab.DOM.describeNode(nodeId=nid)
    node = desc.get("node", desc) if isinstance(desc, dict) else {}
    fid = node.get("frameId")
    if not fid:
        raise RuntimeError(f"Could not resolve frameId for iframe ({frame!r})")
    return fid


def _context_id_for(frame: Any) -> int | None:
    """Return the default execution contextId for `frame`, or None for the main frame."""
    if frame is None:
        return None
    fid = _resolve_frame_id(frame)
    # The execution-context event may lag slightly behind DOM readiness; poll briefly.
    for _ in range(20):
        cid = _state["contexts"].get(fid)
        if cid is not None:
            return cid
        time.sleep(0.05)
    raise RuntimeError(
        f"No execution context for frame {frame!r} (frameId {fid}). "
        "The iframe may be detached or cross-origin-restricted."
    )


def _iframe_viewport_offset(frame: Any) -> dict | None:
    """Return {left, top} of the iframe element in the parent viewport (main context)."""
    if isinstance(frame, int) or (isinstance(frame, str) and frame.isdigit()):
        idx = int(frame)
        js = (
            f"(function(){{const f=document.querySelectorAll('iframe')[{idx}];"
            "if(!f)return null;const r=f.getBoundingClientRect();"
            "return {left:r.left,top:r.top};})()"
        )
    else:
        sel = _js_str(str(frame))
        js = (
            f"(function(){{const f=document.querySelector({sel});"
            "if(!f)return null;const r=f.getBoundingClientRect();"
            "return {left:r.left,top:r.top};})()"
        )
    return _eval(js)  # evaluated in the MAIN context


# --- Core eval ---------------------------------------------------------------


def _eval(expression: str, await_promise: bool = True, frame: Any = None,
          cdp_timeout: float = 10) -> Any:
    tab = _ensure_tab()
    context_id = _context_id_for(frame)
    kwargs: dict[str, Any] = {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
        "allowUnsafeEvalBlocking": True,
    }
    if context_id is not None:
        kwargs["contextId"] = context_id
    res = tab.Runtime.evaluate(**kwargs, _timeout=cdp_timeout)
    if not res:
        return None
    if res.get("exceptionDetails"):
        det = res["exceptionDetails"]
        text = (det.get("exception") or {}).get("description") or det.get("text")
        raise RuntimeError(f"Eval failed: {text}")
    return (res.get("result") or {}).get("value")


def _js_str(s: str) -> str:
    return json.dumps(s)


# --- Tools -------------------------------------------------------------------


@mcp.tool(description="Launch Microsoft Edge on this server's configured --cdp port, with a dedicated "
    "profile and loopback CDP listener. Reuses an existing Edge CDP endpoint. Only localhost/127.0.0.1 "
    "CDP hosts are supported for launching; waits up to 15 seconds for readiness. "
    "EDGE_PATH and EDGE_PROFILE_DIR can be configured in the server environment. "
    "The browser remains open when the MCP server exits.")
def launch_edge() -> dict:
    return edge_launcher.launch(CDP_HOST, CDP_PORT)


@mcp.tool(description="List all open page tabs in the Edge instance.")
def list_tabs() -> list[dict]:
    return _list_pages()


@mcp.tool(description="Select the active tab to operate on by target id (from list_tabs).")
def select_tab(target_id: str) -> dict:
    t = _select(target_id)
    return {"selected": {"id": t.id}}


@mcp.tool(description="Return id, url, title of the currently active tab (top-level frame).")
def get_active_tab() -> dict:
    _ensure_tab()
    return {
        "id": _state["tab"].id,
        "url": _eval("location.href"),
        "title": _eval("document.title"),
    }


@mcp.tool(description="Navigate a URL and wait for load. With frame, navigates that iframe "
    "(frame is an int index or a CSS selector for the <iframe> element).")
def navigate(url: str, frame: Any = None) -> dict:
    tab = _ensure_tab()
    fid = _resolve_frame_id(frame)
    kwargs: dict[str, Any] = {"url": url}
    if fid is not None:
        kwargs["frameId"] = fid
    tab.Page.navigate(**kwargs)
    try:
        tab.wait(timeout=5)
    except Exception:
        pass
    return {"navigated": url, "frame": frame}


@mcp.tool(description="Evaluate a JavaScript expression and return the JSON result. "
    "Optional frame (int index or CSS selector for the <iframe>) evals inside that iframe.")
def eval(expression: str, frame: Any = None) -> Any:
    return _eval(expression, frame=frame)


@mcp.tool(description="Return the current URL. Optional frame returns the iframe's URL.")
def get_url(frame: Any = None) -> str:
    return _eval("location.href", frame=frame)


@mcp.tool(description="Return the current title. Optional frame returns the iframe's title.")
def get_title(frame: Any = None) -> str:
    return _eval("document.title", frame=frame)


@mcp.tool(description="Return outerHTML of the document or the first element matching a CSS selector. "
    "Optional frame operates inside that iframe.")
def get_dom(selector: str | None = None, frame: Any = None) -> str | None:
    if selector:
        expr = f"(document.querySelector({_js_str(selector)})||{{outerHTML:null}}).outerHTML"
    else:
        expr = "document.documentElement.outerHTML"
    return _eval(expr, frame=frame)


@mcp.tool(description="Return textContent of the first element matching a selector, or the whole body. "
    "Optional frame operates inside that iframe.")
def get_text(selector: str | None = None, frame: Any = None) -> str:
    sel = _js_str(selector) if selector else '"body"'
    expr = f"(document.querySelector({sel})||{{textContent:''}}).textContent.trim().slice(0,50000)"
    return _eval(expr, frame=frame)


@mcp.tool(description="Return an array of {href, text} for all <a> elements. "
    "Optional frame operates inside that iframe.")
def extract_links(frame: Any = None) -> list[dict]:
    expr = (
        "Array.from(document.querySelectorAll('a'))"
        ".map(a=>({href:a.href,text:(a.textContent||'').trim()}))"
        ".filter(l=>l.href)"
    )
    return _eval(expr, frame=frame)


@mcp.tool(description="Click the first element matching a CSS selector using real mouse input. "
    "Optional frame (int index or CSS selector for the <iframe>) clicks inside that iframe.")
def click(selector: str, frame: Any = None) -> dict:
    tab = _ensure_tab()
    sel = _js_str(selector)
    box = _eval(
        "(function(){const el=document.querySelector(" + sel + ");if(!el)return null;"
        "el.scrollIntoView({block:'center',inline:'center'});"
        "const r=el.getBoundingClientRect();"
        "return {x:r.left+r.width/2,y:r.top+r.height/2};})()",
        frame=frame,
    )
    if not box:
        raise RuntimeError(f"No element matches selector {selector}")
    # If inside an iframe, translate the iframe-local coords to page viewport coords.
    if frame is not None:
        off = _iframe_viewport_offset(frame)
        if off:
            box = {"x": box["x"] + off["left"], "y": box["y"] + off["top"]}
    common = {"x": box["x"], "y": box["y"], "button": "left", "clickCount": 1}
    tab.Input.dispatchMouseEvent(type="mousePressed", **common)
    tab.Input.dispatchMouseEvent(type="mouseReleased", **common)
    return {"clicked": selector, "at": box, "frame": frame}


@mcp.tool(description="Focus the first element matching a selector and type text into it. "
    "Optional frame (int index or CSS selector for the <iframe>) types inside that iframe.")
def type(selector: str, text: str, frame: Any = None) -> dict:
    tab = _ensure_tab()
    sel = _js_str(selector)
    focused = _eval(
        "(function(){const el=document.querySelector(" + sel + ");if(!el)return false;"
        "el.focus();return true;})()",
        frame=frame,
    )
    if not focused:
        raise RuntimeError(f"No element matches selector {selector}")
    tab.Input.insertText(text=text)
    return {"typed": len(text), "into": selector, "frame": frame}


_CDP_KEYS = {
    "Enter": {"code": "Enter", "key": "Enter", "keyCode": 13, "text": "\r", "location": 0},
    "Tab": {"code": "Tab", "key": "Tab", "keyCode": 9, "text": "\t", "location": 0},
    "Escape": {"code": "Escape", "key": "Escape", "keyCode": 27, "text": "", "location": 0},
    "Backspace": {"code": "Backspace", "key": "Backspace", "keyCode": 8, "text": "\u0008", "location": 0},
    "Delete": {"code": "Delete", "key": "Delete", "keyCode": 46, "text": "\u007f", "location": 0},
    "Space": {"code": "Space", "key": " ", "keyCode": 32, "text": " ", "location": 0},
    "ArrowUp": {"code": "ArrowUp", "key": "ArrowUp", "keyCode": 38, "text": "", "location": 0},
    "ArrowDown": {"code": "ArrowDown", "key": "ArrowDown", "keyCode": 40, "text": "", "location": 0},
    "ArrowLeft": {"code": "ArrowLeft", "key": "ArrowLeft", "keyCode": 37, "text": "", "location": 0},
    "ArrowRight": {"code": "ArrowRight", "key": "ArrowRight", "keyCode": 39, "text": "", "location": 0},
    "Home": {"code": "Home", "key": "Home", "keyCode": 36, "text": "", "location": 0},
    "End": {"code": "End", "key": "End", "keyCode": 35, "text": "", "location": 0},
    "PageUp": {"code": "PageUp", "key": "PageUp", "keyCode": 33, "text": "", "location": 0},
    "PageDown": {"code": "PageDown", "key": "PageDown", "keyCode": 34, "text": "", "location": 0},
    "F1": {"code": "F1", "key": "F1", "keyCode": 112, "text": "", "location": 0},
    "F2": {"code": "F2", "key": "F2", "keyCode": 113, "text": "", "location": 0},
    "F3": {"code": "F3", "key": "F3", "keyCode": 114, "text": "", "location": 0},
    "F4": {"code": "F4", "key": "F4", "keyCode": 115, "text": "", "location": 0},
    "F5": {"code": "F5", "key": "F5", "keyCode": 116, "text": "", "location": 0},
    "F6": {"code": "F6", "key": "F6", "keyCode": 117, "text": "", "location": 0},
    "F7": {"code": "F7", "key": "F7", "keyCode": 118, "text": "", "location": 0},
    "F8": {"code": "F8", "key": "F8", "keyCode": 119, "text": "", "location": 0},
    "F9": {"code": "F9", "key": "F9", "keyCode": 120, "text": "", "location": 0},
    "F10": {"code": "F10", "key": "F10", "keyCode": 121, "text": "", "location": 0},
    "F11": {"code": "F11", "key": "F11", "keyCode": 122, "text": "", "location": 0},
    "F12": {"code": "F12", "key": "F12", "keyCode": 123, "text": "", "location": 0},
}

_MOD_BITS = {"ctrl": 2, "control": 2, "alt": 1, "shift": 16, "meta": 8, "cmd": 8, "command": 8}


@mcp.tool(description="Press a key (optionally with modifiers) on the active page. "
    "key is a normalized key name (e.g. 'Enter','Tab','Escape','ArrowDown','a','F5'); "
    "modifiers is an optional list of 'ctrl','alt','shift','meta'.")
def press_key(key: str, modifiers: list[str] | None = None) -> dict:
    tab = _ensure_tab()
    mods = [m.lower() for m in (modifiers or [])]
    mod_bit = 0
    for m in mods:
        mod_bit |= _MOD_BITS.get(m, 0)

    entry = _CDP_KEYS.get(key)
    if entry is None:
        entry = {
            "code": key if len(key) == 1 and key.isalpha() else "Unidentified",
            "key": key,
            "keyCode": ord(key.upper()) if len(key) == 1 and key.isalpha() else 0,
            "text": key if len(key) == 1 else "",
            "location": 0,
        }

    common = {
        "key": entry["key"],
        "code": entry["code"],
        "windowsVirtualKeyCode": entry["keyCode"],
        "nativeVirtualKeyCode": entry["keyCode"],
        "modifiers": mod_bit,
        "text": entry["text"],
        "location": entry["location"],
        "unmodifiedText": entry["text"],
        "isKeypad": False,
        "isSystemKey": False,
    }

    tab.Input.dispatchKeyEvent(type="keyDown", **common)
    if entry["text"]:
        tab.Input.dispatchKeyEvent(type="char", **common)
    tab.Input.dispatchKeyEvent(type="keyUp", **common)

    return {"pressed": key, "modifiers": mods}


@mcp.tool(description="Scroll the page to (x,y), or scroll an element matching selector into view. "
    "Optional frame operates inside that iframe (scrolling the iframe's content).")
def scroll(selector: str | None = None, x: float = 0, y: float = 0, frame: Any = None) -> dict:
    if selector:
        sel = _js_str(selector)
        _eval(
            "(document.querySelector(" + sel + ")||{}).scrollIntoView"
            "&&document.querySelector(" + sel + ").scrollIntoView({block:'center'})",
            frame=frame,
        )
        return {"scrolled_to": selector, "frame": frame}
    _eval(f"window.scrollTo({x}, {y})", frame=frame)
    return {"scrolled_to": {"x": x, "y": y}, "frame": frame}


@mcp.tool(description="Wait until a CSS selector matches an element, or until timeout (ms). "
    "Optional frame waits inside that iframe.")
def wait_for(selector: str, timeout: int = 5000, frame: Any = None) -> dict:
    sel = _js_str(selector)
    expr = (
        "(async()=>{if(document.querySelector(" + sel + "))return true;"
        "return await new Promise(resolve=>{const obs=new MutationObserver(()=>{"
        "if(document.querySelector(" + sel + ")){obs.disconnect();resolve(true);}});"
        "obs.observe(document.documentElement,{childList:true,subtree:true});"
        f"setTimeout(()=>{{obs.disconnect();resolve(false);}}, {timeout});" + "});})()"
    )
    found = _eval(expr, await_promise=True, frame=frame)
    return {"found": bool(found), "selector": selector, "frame": frame}


@mcp.tool(description="Capture a PNG screenshot of the active tab. full_page=True captures the entire page.")
def screenshot(full_page: bool = False) -> Image:
    tab = _ensure_tab()
    res = tab.Page.captureScreenshot(
        format="png",
        captureBeyondViewport=bool(full_page),
        fromSurface=True,
    )
    data = base64.b64decode(res["data"])
    return Image(data=data, format="png")


# --- Waiting tools -----------------------------------------------------------


def _deadline(timeout: int) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 300000:
        raise ValueError("timeout must be an integer from 0 to 300000 milliseconds")
    return time.monotonic() + timeout / 1000.0


def _poll(check, deadline: float) -> bool:
    """Check immediately (including timeout=0), then poll using a monotonic clock."""
    while True:
        if check():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _read_while_waiting(expression: str, deadline: float, frame=None):
    # Navigation can destroy an execution context between polls. Retry only that
    # transient condition, not malformed expressions, missing frames, or CDP loss.
    try:
        return _eval(expression, frame=frame,
                     cdp_timeout=max(0.1, min(2, deadline - time.monotonic())))
    except Exception as exc:
        message = str(exc).lower()
        if any(token in message for token in (
            "execution context was destroyed", "cannot find context", "cannot find default execution context",
        )):
            return None
        raise


@mcp.tool(description="Wait for a case-sensitive substring in body.textContent (includes hidden text). "
    "timeout is milliseconds, default 5000; 0 checks once. Optional frame selects an iframe. "
    "Returns found=false on timeout.")
def wait_for_text(text: str, timeout: int = 5000, frame: str | int | None = None) -> dict:
    deadline = _deadline(timeout)
    # Re-read document.body on EVERY poll: pages may replace the entire body.
    # No page timers/MutationObservers are left behind after navigation/timeout.
    expression = f"(document.body?.textContent ?? '').includes({_js_str(text)})"
    found = _poll(lambda: _read_while_waiting(expression, deadline, frame) is True, deadline)
    return {"found": found, "text": text, "frame": frame}


@mcp.tool(description="Wait for a Python regex search against the current URL (including hash changes). "
    "timeout is milliseconds, default 5000; 0 checks once. Optional frame selects an iframe. "
    "Returns matched=false on timeout; invalid regex is an error.")
def wait_for_url(pattern: str, timeout: int = 5000, frame: str | int | None = None) -> dict:
    deadline = _deadline(timeout)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid URL regex: {exc}") from exc
    last = None

    def check():
        nonlocal last
        value = _read_while_waiting("location.href", deadline, frame)
        if value is not None:
            last = str(value)
        return value is not None and regex.search(str(value)) is not None

    matched = _poll(check, deadline)
    return {"matched": matched, "url": last}


@mcp.tool(description="Wait for the current top-level document: domcontentloaded (interactive/complete), "
    "load (complete), or networkidle (complete plus no tracked HTTP requests for 500ms). "
    "timeout is milliseconds, default 30000; returns reached=false on timeout. "
    "Network tracking starts when the tab is selected; select before initiating requests.")
def wait_for_load_state(
    state: Literal["domcontentloaded", "load", "networkidle"] = "load",
    timeout: int = 30000,
) -> dict:
    deadline = _deadline(timeout)
    if state not in ("domcontentloaded", "load", "networkidle"):
        raise ValueError("state must be domcontentloaded, load, or networkidle")
    _ensure_tab()
    tracker = _state["network"]

    def check():
        ready = _read_while_waiting("document.readyState", deadline)
        if state == "domcontentloaded":
            return ready in ("interactive", "complete")
        if ready != "complete":
            return False
        return state == "load" or tracker.idle()

    # Readiness polling avoids the race between checking readyState and installing
    # a lifecycle listener. Network listeners remain active across all tool calls.
    return {"state": state, "reached": _poll(check, deadline)}


# --- Browser navigation & tab lifecycle -------------------------------------


@mcp.tool(description="Request reload of the selected tab; ignore_cache bypasses HTTP cache. "
    "Returns after CDP accepts the command. Use wait_for_load_state to await readiness.")
def reload(ignore_cache: bool = False) -> dict:
    tab = _ensure_tab()
    tab.Page.reload(ignoreCache=ignore_cache, _timeout=10)
    return {"reloaded": True, "ignore_cache": ignore_cache}


def _traverse_history(offset: int) -> dict:
    tab = _ensure_tab()
    hist = tab.Page.getNavigationHistory(_timeout=5)
    entries = hist.get("entries", [])
    destination = hist.get("currentIndex", 0) + offset
    key = "went_back" if offset < 0 else "went_forward"
    if not 0 <= destination < len(entries):
        return {key: False}
    entry = entries[destination]
    # Traverse the existing entry. Page.navigate(url=...) would append an entry
    # and discard forward history, breaking the back -> forward roundtrip.
    tab.Page.navigateToHistoryEntry(entryId=entry["id"], _timeout=10)
    return {key: True, "url": entry["url"]}


@mcp.tool(description="Request one step back in selected tab history, preserving forward history. "
    "Returns went_back=false at the boundary. Use wait_for_url/load_state to wait.")
def go_back() -> dict:
    return _traverse_history(-1)


@mcp.tool(description="Request one step forward in selected tab history. "
    "Returns went_forward=false at the boundary. Use wait_for_url/load_state to wait.")
def go_forward() -> dict:
    return _traverse_history(1)


@mcp.tool(description="Open a new tab at url (default about:blank). Returns its id and requested URL. "
    "Does not change the MCP-selected tab; call select_tab(id) to control it.")
def new_tab(url: str | None = None) -> dict:
    url = url or "about:blank"
    tab = _browser().new_tab(url=url, timeout=10)
    return {"id": tab.id, "url": url}


@mcp.tool(description="Close target_id, or the MCP-selected tab when omitted. "
    "Unknown targets are errors. Closing the selected tab clears its connection/cache.")
def close_tab(target_id: str | None = None) -> dict:
    tid = target_id if target_id is not None else _ensure_tab().id
    if not any(page["id"] == tid for page in _list_pages()):
        raise ValueError(f"No page target with id {tid}")
    _browser().close_tab(tid, timeout=10)
    if _state["tab"] is not None and _state["tab"].id == tid:
        try:
            _state["tab"].stop()
        finally:
            _state.update(tab=None, contexts={}, ctx_to_frame={}, network=None)
    return {"closed": tid}


# --- main --------------------------------------------------------------------


def _parse_addr(value: str, default_host: str, default_port: str) -> tuple[str, str]:
    """Parse 'host:port' or 'host' or 'port' into (host, port)."""
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host or default_host, port or default_port
    if value.isdigit():
        return default_host, value
    return value, default_port


def main() -> None:
    global CDP_HOST, CDP_PORT, MCP_HOST, MCP_PORT, MCP_PATH
    parser = argparse.ArgumentParser(
        prog="edgemcp",
        description="MCP server (Streamable HTTP) that drives Edge over CDP.",
    )
    parser.add_argument(
        "--cdp",
        default=f"{CDP_HOST}:{CDP_PORT}",
        help="Edge remote-debugging address HOST:PORT (default: %(default)s)",
    )
    parser.add_argument(
        "--listen",
        default=f"{MCP_HOST}:{MCP_PORT}",
        help="MCP HTTP bind address HOST:PORT (default: %(default)s)",
    )
    parser.add_argument(
        "--path",
        default=MCP_PATH,
        help="MCP Streamable-HTTP endpoint path (default: %(default)s)",
    )
    parser.add_argument(
        "--launch-edge",
        action="store_true",
        help="Launch local Edge with CDP on the configured --cdp port before starting MCP",
    )
    args = parser.parse_args()

    CDP_HOST, CDP_PORT = _parse_addr(args.cdp, CDP_HOST, CDP_PORT)
    MCP_HOST, MCP_PORT_S = _parse_addr(args.listen, MCP_HOST, str(MCP_PORT))
    MCP_PORT = int(MCP_PORT_S)
    MCP_PATH = args.path

    if args.launch_edge:
        if str(CDP_PORT).isdecimal() and int(CDP_PORT) == MCP_PORT:
            parser.error("CDP and MCP HTTP must use different ports when launching Edge")
        try:
            result = launch_edge()
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"[edgemcp] Edge ready: {result['cdp_url']} ({result['browser']}).", flush=True)

    if MCP_HOST not in ("127.0.0.1", "localhost", "::1"):
        print(
            "[edgemcp] WARNING: MCP has no authentication. A non-loopback listener "
            "exposes browser control; restrict access with authentication and firewall rules.",
            flush=True,
        )

    try:
        pages = _list_pages()
        print(
            f"[edgemcp] Connected to Edge at {CDP_HOST}:{CDP_PORT} — "
            f"{len(pages)} page target(s).",
            flush=True,
        )
    except Exception as e:
        print(
            f"[edgemcp] WARNING: could not list targets at {CDP_HOST}:{CDP_PORT} "
            f"({e}). Server will still start; tools will fail until Edge is "
            f"reachable. Launch Edge with --remote-debugging-port={CDP_PORT}.",
            flush=True,
        )
    print(
        f"[edgemcp] MCP Streamable HTTP endpoint: http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}",
        flush=True,
    )
    mcp.run(
        transport="streamable-http",
        host=MCP_HOST,
        port=MCP_PORT,
        streamable_http_path=MCP_PATH,
    )


if __name__ == "__main__":
    main()
