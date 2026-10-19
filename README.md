# EdgeMCP

A **Python MCP server** that exposes a **Streamable HTTP endpoint** a remote
agent can reach, and drives the **current page** in Microsoft Edge over the
**Chrome DevTools Protocol (CDP)** via `--remote-debugging-port`.

> This is **Shape C** from [`FEASIBILITY.md`](./FEASIBILITY.md). It answers
> "remote agent controls the page" cleanly, but it is **not** an Edge extension —
> it requires launching Edge with remote debugging. The supplied launchers use
> a dedicated debugging profile rather than the normal browser profile.
> See FEASIBILITY.md for extension-based alternatives.

**Security:** the MCP endpoint has no built-in authentication or TLS. It binds
to localhost by default. The optional forwarding-only SSH server requires either
public-key or configured username/password authentication, but does not
authenticate MCP clients. Read
[SECURITY.md](SECURITY.md) before enabling remote access, and do not use a
sensitive browser profile.

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

## Forward CDP from another machine with mini-SSHD

EdgeMCP can run an in-process, forwarding-only SSH server. This allows **Machine
A** to keep Edge CDP on its own loopback while forwarding it to **thisMachine**,
where EdgeMCP and the remotely reachable MCP endpoint run:

```text
remote agent ──MCP──► thisMachine:8080 ──► 127.0.0.1:9333
                                             │ SSH -R
                                             ▼
                                      Machine A:127.0.0.1:9222 ──► Edge
```

1. Choose an SSH authentication method. You can add Machine A's SSH public key
   to `~/.ssh/authorized_keys` on thisMachine (or select another file with
   `--sshd-authorized-keys`). Alternatively, create a protected password file
   containing exactly one non-empty UTF-8 line:

   ```bash
   mkdir -m 700 -p ~/.edgemcp
   install -m 600 /dev/null ~/.edgemcp/forwarding-password
   printf '%s\n' 'replace-with-a-strong-password' > ~/.edgemcp/forwarding-password
   ```

   Then start EdgeMCP on thisMachine. Here `9333` is the tunnel listener and
   `2222` is mini-SSHD. For password authentication, add the final two flags:

   ```bash
   python src/server.py \
     --sshd 2222 \
     --cdp 127.0.0.1:9333 \
     --listen 0.0.0.0:8080 \
     --sshd-user forwarder \
     --sshd-password-file ~/.edgemcp/forwarding-password
   ```

2. Start Edge on Machine A with CDP on its loopback port, for example `9222`.
   From Machine A, establish the reverse forward using OpenSSH's `-p` syntax:

   ```bash
   ssh -N -p 2222 \
     -o ExitOnForwardFailure=yes \
     -R 127.0.0.1:9333:127.0.0.1:9222 \
     user@thisMachine
   ```

   `-N` is recommended because no remote shell is needed. A command without
   `-N` is also kept connected by an inert forwarding-only session, but there
   is no command interpreter. Remote exec, subsystems, direct forwarding,
   agent forwarding, and X11 forwarding are rejected. Only a loopback remote
   listener on the exact configured CDP port (`9333` here) is accepted.

3. Point agents at `http://thisMachine:8080/mcp`.

At least one authentication source is mandatory: an existing authorized-keys
file or `--sshd-user` plus `--sshd-password-file`. When both are configured,
either method is accepted. The password file is read once at startup; one final
LF or CRLF is removed, while other spaces are preserved. It must not contain
additional lines. Protect it from other users and restart EdgeMCP after rotating
it. Password-authentication usernames are checked exactly but are not mapped to
OS accounts. Public keys continue to accept any supplied SSH username against
the shared authorized-keys file.

The default server host key is `~/.edgemcp/ssh_host_ed25519_key`; it is generated
once with private permissions and reused. Use `--sshd-host-key PATH` to supply a
different unencrypted host private key. Verify and retain the host fingerprint
as you would for any SSH server.

`--sshd PORT` binds SSH on `0.0.0.0` by default so Machine A can reach it. Use
`--sshd HOST:PORT` or `SSHD_HOST` to narrow that listener. `--sshd` requires a
loopback `--cdp` address and cannot be combined with `--launch-edge`. The MCP
listener remains independently unauthenticated: expose `--listen 0.0.0.0:8080`
only on a trusted/restricted network or behind an authenticated gateway.

### Diagnosing identification/banner timeouts

The mini-SSHD logs every accepted TCP connection as `[edgemcp:sshd] TCP
connection from ...` and logs closure/handshake errors. If OpenSSH reports
`kex_exchange_identification` or `banner exchange ... timed out`:

- First try the standard SSH port, `--sshd 22`, especially from hardened or
  privileged-access workstations. Such networks commonly permit SSH on port 22
  to approved destinations while silently dropping application data on ports
  such as 2222. On Unix, binding port 22 may require elevated bind capability;
  do not run all of EdgeMCP as root merely to obtain the port.
- No corresponding EdgeMCP TCP log means the handshake was accepted or
  intercepted before reaching this process (for example by NAT, a VPN relay,
  endpoint security, or a firewall). `Test-NetConnection` can still report true
  when an intermediary completed the TCP handshake.
- A TCP log with no `Remote protocol version` on `ssh -vvv` means EdgeMCP
  accepted the socket and emitted its `SSH-2.0-...` identification, but the
  return traffic was dropped or intercepted. Check both directions of the
  route and any SSH/DPI policy.
- Authentication and KEX algorithm problems occur only after `ssh -vvv` prints
  the remote protocol version, and produce different diagnostics.

The integration suite directly verifies that the server identification banner
is emitted immediately after TCP connection, before client authentication.

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
| `launch_edge` | Start local Edge with CDP on the server's configured `--cdp` port, or reuse an existing Edge endpoint. |
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

## Launching Edge from MCP

There are two ways to ask the server to launch Edge:

```bash
# Launch Edge on CDP port 9333, then serve MCP on a different port.
python src/server.py --launch-edge --cdp 127.0.0.1:9333 --listen 127.0.0.1:8080
```

Or start MCP without the flag and invoke the **`launch_edge()`** tool with no
arguments. It uses the server's current `--cdp` configuration, not its HTTP
`--listen` port. The flag and tool share the same launch implementation.

- Launching is supported only when the CDP host is `127.0.0.1` or `localhost`.
  It starts Edge on the **MCP server's machine**, not on a remote CDP host.
- The helper discovers a native Edge installation on Windows, Linux or macOS.
  Set `EDGE_PATH` to override executable discovery. For Windows Edge, run the
  server with Windows Python; launching Windows Edge from WSL Python is not
  supported by this tool (the separate WSL shell launcher remains available).
- By default it uses a dedicated profile named `debug-profile-PORT` under the
  platform's local application-data/state directory in `EdgeMCP`. Different
  CDP ports therefore use different profiles. `EDGE_PROFILE_DIR` overrides the
  exact profile directory; only point it at a dedicated non-sensitive profile.
- CDP is requested on loopback. An existing **Edge** CDP endpoint is reused;
  an unrelated service or another browser occupying the port is an error.
- The helper waits up to 15 seconds after launch for CDP readiness. It returns
  `started`, `already_running`, `cdp_url`, and `browser`; a new launch also
  returns `launch_pid` (the launcher PID, not necessarily the final browser PID).
- A launch timeout is an error, not success. The process may still be starting;
  retries within the same server wait on that pending attempt instead of
  blindly spawning duplicates. Check browser policy/profile conflicts if it
  never becomes ready.
- **The browser remains open when MCP exits.** The helper does not kill browsers
  or modify firewall rules. Without the CLI flag or a tool invocation, MCP
  never launches Edge automatically.

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

To launch both Edge and the MCP server in one command, use `--launch-edge` as
shown above (then skip step 3). Alternatively, launch Edge separately:

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
- `--launch-edge` — launch local Edge on the configured CDP port before serving MCP (off by default).
- `--sshd [HOST:]PORT` — run the forwarding-only SSH server; a bare port binds using `SSHD_HOST` (default `0.0.0.0`).
- `--sshd-authorized-keys PATH` — allowed client public keys (default `SSHD_AUTHORIZED_KEYS` or `~/.ssh/authorized_keys`).
- `--sshd-host-key PATH` — unencrypted server private key, generated as Ed25519 if missing (default `SSHD_HOST_KEY` or `~/.edgemcp/ssh_host_ed25519_key`).
- `--sshd-user USER` — exact username accepted for password authentication (default `SSHD_USER`).
- `--sshd-password-file PATH` — one-line UTF-8 password file; requires `--sshd-user` (default `SSHD_PASSWORD_FILE`).

Env vars (`CDP_HOST`, `CDP_PORT`, `MCP_HOST`, `MCP_PORT`, `MCP_PATH`,
`SSHD_HOST`, `SSHD_AUTHORIZED_KEYS`, `SSHD_HOST_KEY`, `SSHD_USER`,
`SSHD_PASSWORD_FILE`) work as fallback defaults.

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
| `--launch-edge` | — | off | Launch local Edge on `--cdp` before starting MCP. |
| `--sshd [HOST:]PORT` | `SSHD_HOST` (host only) | off (`0.0.0.0` host) | Start forwarding-only mini-SSHD. |
| `--sshd-authorized-keys PATH` | `SSHD_AUTHORIZED_KEYS` | `~/.ssh/authorized_keys` | Authorized client public keys. |
| `--sshd-host-key PATH` | `SSHD_HOST_KEY` | `~/.edgemcp/ssh_host_ed25519_key` | Persistent server host key. |
| `--sshd-user USER` | `SSHD_USER` | unset | Username for password authentication. |
| `--sshd-password-file PATH` | `SSHD_PASSWORD_FILE` | unset | Protected one-line password file; requires a user. |

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
requirements.txt        # mcp (v2), pychrome, AsyncSSH
src/server.py           # the MCP server (Streamable HTTP) + CDP tool handlers
src/edge_launcher.py    # shared configured-port browser launcher
src/mini_sshd.py         # authenticated, forwarding-only in-process SSH server
scripts/launch-edge.sh  # WSL launcher for Edge
scripts/launch-edge.ps1 # Windows launcher for Edge
scripts/smoke_http.py   # asserted HTTP integration test with local fixture
tests/test_waiting_navigation.py # wait/navigation unit tests
tests/test_edge_launcher.py     # launch helper and CLI/tool unit tests (mocked processes)
tests/test_mini_sshd.py          # policy, CLI, authentication and real forwarding tests
```

## Why not an extension?

Because the original question was about an extension-hosted server;
FEASIBILITY.md explains the one hard constraint (extensions can't bind a port)
and the two extension-based shapes (native-messaging bridge, WebSocket relay).
This repo implements Shape C as the pragmatic PoC; Shape A is the natural next
step if you need to drive a normal, already-running Edge profile.
