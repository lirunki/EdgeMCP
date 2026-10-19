# EdgeMCP

A **Python MCP server** that exposes a **Streamable HTTP endpoint** a remote
agent can reach, and drives the **current page** in Microsoft Edge over the
**Chrome DevTools Protocol (CDP)** via `--remote-debugging-port`.

> This is **Shape C** from [`FEASIBILITY.md`](./FEASIBILITY.md). It answers
> "remote agent controls the page" cleanly, but it is **not** an Edge extension —
> it requires launching Edge with remote debugging. The supplied launchers use
> a dedicated debugging profile rather than the normal browser profile.
> See FEASIBILITY.md for extension-based alternatives.

**Security:** this proof of concept has no built-in authentication or TLS.
It binds to localhost by default. Read [SECURITY.md](SECURITY.md) before enabling
remote access, and do not use a sensitive browser profile.

## How it works

```
remote agent ──MCP(Streamable HTTP)──► edgemcp server (Python) ──CDP/WS──► Edge (--remote-debugging-port=9222)
                                        http://<host>:8080/mcp
```

The server speaks standard **MCP over Streamable HTTP** (so any MCP client —
Claude Desktop, the `mcp` CLI, a remote agent — can reach it over the network).
It forwards each tool call to Edge via CDP. All page-control capability
(navigate, eval, click, type, screenshot, …) runs against the tab Edge exposes
for debugging.

## The endpoint

The MCP endpoint is the URL a remote agent points its client at:

```
http://<MCP_HOST>:<MCP_PORT><MCP_PATH>     # default http://127.0.0.1:8080/mcp
```

`MCP_HOST=127.0.0.1` is localhost-only. Setting `MCP_HOST=0.0.0.0` binds all
interfaces, but does not add authentication. Prefer an authenticated tunnel
or gateway and restrict access; see [SECURITY.md](SECURITY.md).

## Tools

| Tool | What it does |
|---|---|
| `list_tabs` | List open page tabs. |
| `select_tab` | Choose the active tab by target id. |
| `get_active_tab` | id / url / title of the active tab (top-level frame). |
| `navigate` | Navigate with a short settling delay; use waiting tools for readiness. With `frame`, navigates that iframe. |
| `eval` | Run JS and return JSON result. With `frame`, runs inside that iframe. |
| `get_url` / `get_title` | Current URL / title. With `frame`, the iframe's. |
| `get_dom` / `get_text` | outerHTML / textContent. With `frame`, inside that iframe. |
| `extract_links` | All `<a>` `{href, text}`. With `frame`, inside that iframe. |
| `click` | Real mouse click. With `frame`, clicks inside that iframe (coords composed). |
| `type` | Focus + `Input.insertText`. With `frame`, inside that iframe. |
| `press_key` | Press a key (e.g. `Enter`,`Tab`,`ArrowDown`,`a`,`F5`) with optional modifiers. |
| `scroll` | Scroll to (x,y) or selector into view. With `frame`, inside that iframe. |
| `wait_for` | Wait until a selector exists. With `frame`, inside that iframe. |
| `wait_for_text` | Wait until text appears in the body. With `frame`, inside that iframe. |
| `wait_for_url` | Wait for a regex match against the URL; optional `frame`. |
| `wait_for_load_state` | Wait for `domcontentloaded` / `load` / `networkidle` using readiness checks and tracked CDP network events. |
| `reload` | Reload the active tab (optionally bypassing cache). |
| `go_back` / `go_forward` | Step back/forward in the tab's history. |
| `new_tab` | Open a new tab, optionally at a URL. |
| `close_tab` | Close a tab by target id (defaults to the active tab). |
| `screenshot` | PNG of viewport, or full page (page-level, no frame). |

## Waiting and navigation

```text
wait_for_text(text, timeout=5000, frame=null)
wait_for_url(pattern, timeout=5000, frame=null)
wait_for_load_state(state="load", timeout=30000)
reload(ignore_cache=false)
go_back()
go_forward()
new_tab(url=null)
close_tab(target_id=null)
```

- Timeouts are **milliseconds**, from 0 to 300000. Zero checks once. A wait
  timeout returns `found: false`, `matched: false`, or `reached: false`, rather
  than raising. Invalid arguments and browser connection errors remain errors.
- Text matching is a case-sensitive substring search in `body.textContent`
  (including hidden text). Each poll re-reads the body, so replacement works.
- URL patterns use **Python regular expressions** (`re.search`), not glob syntax.
  Example: `"/checkout.*#complete$"`. Invalid regex patterns are errors.
- `domcontentloaded` accepts `document.readyState` of `interactive` or `complete`;
  `load` requires `complete`. These wait for the **current** document, not a
  hypothetical future navigation. Already-reached states return immediately.
- `networkidle` requires `complete` and no tracked HTTP requests for **500 ms**.
  Requests are tracked continuously from tab selection, including requests that
  start before the wait call. Select the tab before initiating activity: CDP
  cannot reconstruct every request already running before attachment. WebSocket
  lifetime is excluded; long-lived HTTP/SSE requests may prevent idleness. This
  is a readiness heuristic, not proof that an application has finished all work.
- `reload`, `go_back`, and `go_forward` return after CDP accepts the command;
  use `wait_for_url` and/or `wait_for_load_state` afterward. History traversal
  preserves forward entries and returns `went_back: false` / `went_forward:
  false` at a boundary.
- `new_tab` defaults to `about:blank` and returns `{id, url}`. It does **not**
  change the MCP-selected tab: call `select_tab(target_id=id)` explicitly.
- `close_tab()` closes the MCP-selected tab, not necessarily Edge's visually
  focused tab. Passing an ID closes only that tab. Closing the selected tab
  clears its connection/context/network state; the next page tool selects a
  remaining page if there is one. An unknown ID returns an error.

This PoC shares one MCP-selected tab across clients. Use one controlling agent
at a time; simultaneous commands from multiple clients are not isolated.

## Operating inside iframes (`frame` parameter)

Most DOM tools accept an optional `frame` argument to operate inside an `<iframe>`:

| `frame` value | Meaning |
|---|---|
| omitted / `null` | the main/top-level document (default) |
| an integer (e.g. `0`) | 0-based index into `document.querySelectorAll('iframe')` |
| a string (e.g. `"#ifr"`, `"iframe[name=inner]"`) | CSS selector matching the `<iframe>` element |

The server resolves the iframe to a CDP frame and runs JavaScript in an execution
context exposed by the attached target. **Out-of-process iframes are not fully
supported**: a cross-origin frame may require a separate target session that
this implementation does not attach to. Selectors/indices address iframes in
the top-level document, not arbitrary nested frame paths. Frame clicks use a
simple coordinate offset; transformed or deeply nested frames may need more
advanced handling. Example:

```jsonc
// get the URL of the first iframe
{"tool": "get_url", "arguments": {"frame": 0}}
// eval inside the iframe selected by #ifr
{"tool": "eval", "arguments": {"expression": "document.title", "frame": "#ifr"}}
```

## Setup

### 1. Install (Python)

Requires Python 3.10+ and a debugging-enabled Edge instance. On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 2. Launch Edge with remote debugging

**From Windows PowerShell:**

```powershell
.\scripts\launch-edge.ps1
```

**From WSL:**

```bash
bash scripts/launch-edge.sh
```

The launcher discovers Edge through Windows environment variables and uses
`%LOCALAPPDATA%\EdgeMCP\debug-profile` by default. No account name or machine
path is hardcoded. Override with `-EdgePath` / `-ProfilePath` (or `EDGE_PATH` /
`EDGE_PROFILE_DIR`). Use `-Port` / `CDP_PORT` to change the debugging port.
`-PrintCommand` previews the command without launching a browser.

Use a **separate profile** to avoid reusing an already-running browser process
that may ignore debugging flags. The launcher requests a loopback-only CDP
listener. Verify it is reachable:

```powershell
(Invoke-WebRequest http://127.0.0.1:9222/json/version).Content
```

### 3. Start the MCP server

```powershell
.\.venv\Scripts\python.exe src\server.py --cdp 127.0.0.1:9222 --listen 127.0.0.1:8080
```

You should see:

```
[edgemcp] Connected to Edge at 127.0.0.1:9222 — N page target(s).
[edgemcp] MCP Streamable HTTP endpoint: http://127.0.0.1:8080/mcp
INFO:     Uvicorn running on http://127.0.0.1:8080
```

Flags:
- `--cdp HOST:PORT` — Edge remote-debugging address (default `127.0.0.1:9222`).
- `--listen HOST:PORT` — MCP HTTP bind address (default `127.0.0.1:8080`). Non-loopback binding requires external access controls.
- `--path PATH` — endpoint path (default `/mcp`).

Env vars (`CDP_HOST`, `CDP_PORT`, `MCP_HOST`, `MCP_PORT`, `MCP_PATH`) still work as fallback defaults if a flag is omitted.

### 4. Run the smoke test

Unit tests (no browser required):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

With Edge debugging enabled, run on the **same machine as Edge**:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_http.py --start-server
```

This starts its own loopback-only MCP test server on a free port and a local
HTTP fixture. It asserts text/URL waits, timeouts, delayed load, networkidle
with an existing fetch, frame operations, back/forward history, reload and tab
lifecycle over MCP HTTP. It uses and closes only tabs it creates, and stops
only its own server process. A valid screenshot is written to `smoke.png`.
Failures exit nonzero; `SMOKE OK` is printed only after assertions pass.

To test an already-running MCP server instead, omit `--start-server` and set
`MCP_URL` if it differs from `http://127.0.0.1:8080/mcp`.

### 5. Point an MCP client at the endpoint

For any MCP client that supports Streamable HTTP, configure the server URL:

```
http://<host>:8080/mcp
```

Use your client's documented HTTP/remote-server configuration; configuration
formats vary by client. A plain browser GET is not an MCP session: the client
must perform the MCP initialization handshake.

## Configuration

| Flag | Env fallback | Default | Meaning |
|---|---|---|---|
| `--cdp` | `CDP_HOST`/`CDP_PORT` | `127.0.0.1:9222` | Edge remote-debugging address. |
| `--listen` | `MCP_HOST`/`MCP_PORT` | `127.0.0.1:8080` | MCP HTTP bind address (localhost-only by default). |
| `--path` | `MCP_PATH` | `/mcp` | Streamable-HTTP endpoint path. |

## WSL note

With WSL2 NAT networking, Linux `127.0.0.1` and Windows `127.0.0.1` refer to
different network stacks. The WSL launcher starts **Windows Edge** and does not
configure port forwarding or firewall rules. Run the MCP server with Windows
Python when Edge CDP is on Windows loopback. Other WSL networking modes may
behave differently; verify connectivity rather than assuming a debugging
address flag makes the port reachable. Do not expose CDP publicly as a workaround.

## Security note

Anyone able to invoke MCP tools can control the browser with the permissions
of its signed-in user. Keep the default loopback binding and a dedicated test
profile. Remote access needs authentication and network restrictions supplied
outside this PoC. Browser/session data, credentials, captures, and local exports
must not be committed or included in shared archives. See [SECURITY.md](SECURITY.md).

## Layout

```
FEASIBILITY.md          # feasibility analysis & the 3 architecture shapes
SECURITY.md             # trust boundary and safe deployment guidance
requirements.txt        # mcp (v2), pychrome
src/server.py           # the MCP server (Streamable HTTP) + CDP tool handlers
scripts/launch-edge.sh  # WSL launcher for Edge
scripts/launch-edge.ps1 # Windows launcher for Edge
scripts/smoke_http.py   # asserted HTTP integration test with local fixture
tests/test_waiting_navigation.py # unit tests (no browser required)
```

## Why not an extension?

Because the original question was about an extension-hosted server;
FEASIBILITY.md explains the one hard constraint (extensions can't bind a port)
and the two extension-based shapes (native-messaging bridge, WebSocket relay).
This repo implements Shape C as the pragmatic PoC; Shape A is the natural next
step if you need to drive a normal, already-running Edge profile.
