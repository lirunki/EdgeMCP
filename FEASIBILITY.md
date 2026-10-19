# Architecture: browser control through MCP

## Can an Edge extension host an MCP server?

An ordinary Manifest V3 browser extension cannot bind a TCP/HTTP listening
port. It can control permitted tabs and initiate outbound connections, so an
extension-based solution needs an external transport endpoint or relay.

MCP uses JSON-RPC with protocol-specific initialization, message handling and
transport rules. Standard clients support transports such as Streamable HTTP;
a raw WebSocket connection is not automatically an interoperable MCP endpoint.

## Architecture options

### A. Extension plus native-messaging bridge

```text
MCP client -> MCP service -> local IPC/native-messaging bridge -> Edge extension
```

The extension initiates a native-messaging connection to a registered local
host. The extension performs browser operations, while the external service
presents a standard MCP endpoint. This can operate on an existing browser
profile without requiring a remote-debugging launch.

Native messaging and MCP stdio both use process streams but have different
framing and process lifecycles. A browser-launched native host and an
agent-launched MCP process need an explicit IPC/broker design; they cannot
simply share stdin/stdout. Windows native-messaging registration is also needed.

Manifest V3 workers have lifecycle limits. Design reconnection and request
recovery explicitly; occasional alarms do not guarantee continuous execution.
Page access also depends on permissions, protected browser pages, frame
boundaries, and the chosen scripting/debugger APIs.

### B. Extension plus outbound relay

```text
MCP client -> authenticated HTTP MCP endpoint <- relay <- outbound extension connection
```

An extension can open an outbound connection to a relay. The relay supplies the
standard MCP transport and routes tool calls to the browser. No native host is
needed on the browser machine, but authentication, pairing, authorization,
worker lifecycle, disconnects, and session routing must be implemented.

### C. External MCP server plus CDP (implemented here)

```text
MCP client -> Python HTTP MCP server -> CDP -> debugging-enabled Edge
```

Edge exposes the Chrome DevTools Protocol when launched with remote debugging.
The Python server connects to that endpoint and translates MCP tools into CDP
operations. Edge does **not** connect to the MCP HTTP endpoint itself.

This avoids extension installation and provides DOM access, navigation, input,
screenshots, and tab management. It requires a debugging-enabled browser and
should use a dedicated profile. Browser version and policy can affect which
CDP features are available.

## Current implementation

This repository implements option C with the Python MCP SDK and `pychrome`.
The default endpoints are local-only:

- Edge CDP: `127.0.0.1:9222`
- MCP Streamable HTTP: `http://127.0.0.1:8080/mcp`

See [README.md](README.md) for the tool interface and limitations, and
[SECURITY.md](SECURITY.md) before enabling remote access. Options A and B are
architectural alternatives, not features implemented in this repository.
