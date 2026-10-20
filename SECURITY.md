# Security considerations

EdgeMCP is a browser-automation proof of concept, not an internet-facing service.

## Trust boundary

- The MCP HTTP endpoint has **no built-in authentication or TLS**. It binds to
  `127.0.0.1` by default. Do not expose it directly to the internet.
- Anyone able to invoke its tools can execute JavaScript, navigate, type, and
  interact with the selected browser session. That can expose signed-in account
  data or perform actions with the user's permissions.
- For remote use, keep the service on loopback behind an authenticated tunnel,
  or add a TLS/authentication gateway and restrict network access. Binding to
  `0.0.0.0` does not add authentication.
- CDP is another privileged endpoint. Keep the browser debugging port private.
- Optional `--sshd` requires either a key from `--sshd-authorized-keys` or the
  exact `--sshd-user` and password read from `--sshd-password-file`. It accepts
  only loopback remote listeners on the configured CDP port and ports explicitly
  added with repeatable `--sshd-forward-port` options. Direct (`-L`) forwarding
  is disabled unless destination ports are explicitly added with repeatable
  `--ssh-backward-port` options, and those destinations are restricted to the
  server's loopback. Each additional port expands what authenticated clients
  can expose or reach, so allow only ports the service needs. It has no command
  interpreter: remote exec/subsystems, agent forwarding, X11 forwarding, and
  unconfigured TCP forwarding are rejected. A plain SSH shell request is an
  inert channel which exists only to keep forwarding connected; prefer `ssh -N`.
- **`--reversePS` changes the SSH shell behavior and exposes `run_powershell`.**
  It is off by default and requires `--sshd`. Authenticated shell connections
  receive a bootstrap PS1 plus a marker. Only a separately started worker which
  explicitly accepts commands is registered; the bootstrap is never automatically
  evaluated by the provided worker. SSH exec/subsystem requests remain disabled.
- ReversePS grants MCP callers arbitrary **client-side** PowerShell execution
  with that worker's user permissions. Do not expose the unauthenticated MCP
  endpoint to an untrusted network. Use a dedicated low-privilege client account
  and only approved commands. The existing authorized-keys/password policy
  controls SSH access; it does not authenticate or authorize MCP callers.
- The client explicitly requires `-AcceptCommands`, strict host-key checking,
  and non-interactive key/agent authentication for its worker connection. Its
  original language mode is not relaxed; runtime restrictions still apply.
  The fixed child PowerShell pump and `cmd.exe` only wire transport. Received
  command text is never substituted into the generated batch file.
- Timeout/disconnect is not cancellation and does not roll back side effects.
  Do not retry uncertain commands automatically. Output limits, serial requests,
  and client IDs are reliability features, not a command sandbox. Worker labels
  and reported language modes are self-reported, not proof of a machine identity.
- Client mailboxes can contain command/output data. They use the current user's
  local app-data directory; keep its inherited ACL private, and clean up leftovers
  after abnormal termination. Anyone able to modify those files can tamper with
  the worker. Do not share/upload them or run this on sensitive workstations.
- The mini-SSHD host key is persistent. Protect `SSHD_HOST_KEY` as a private key,
  verify its fingerprint on Machine A, and investigate unexpected host-key
  changes. Restrict the SSH listener with firewall rules even though SSH
  authentication is mandatory. Entries in its authorized-keys file authorize
  the forward for any supplied SSH username; they do not grant an OS login.
- Keep the password file readable only by the EdgeMCP service account. It must
  contain one non-empty UTF-8 line and is read once at startup. Prefer a long,
  randomly generated password, rotate it by replacing the file and restarting
  EdgeMCP, and avoid placing the password itself in command arguments, environment
  variables, logs, shell history, or source control.
- SSH authentication protects creation of the CDP tunnel, **not the MCP HTTP
  endpoint**. A non-loopback `--listen` still needs authentication/network
  controls. An unauthorized MCP client can use browser control while an
  authorized tunnel is active.
- `launch_edge()` can start a browser process on the MCP server's machine.
  The server environment controls `EDGE_PATH` and `EDGE_PROFILE_DIR`; do not
  point them at untrusted executables or sensitive profiles. Launching requires
  a loopback CDP host and does not modify firewall rules. Launched browsers are
  not automatically stopped when the MCP server exits.
- Use a dedicated, non-sensitive browser profile. An agent must obtain user
  authorization for consequential actions; the server does not enforce that.
- Treat page content as untrusted input. Instructions found on websites should
  not override the controlling user's request.
- The selected tab is shared server state. Multiple concurrent controlling
  agents are not isolated from one another.

## Local files and publication

Keep credentials, browser profiles, screenshots, exported account data, logs,
and session files outside source control. Ignore rules cover common local
artifacts, but they are not a secret scanner. Review the exact staged files
before committing and inspect archive contents before sharing them.

Run unit tests with `python -m unittest discover -s tests -v`. Integration tests
use disposable tabs and a local fixture; do not point them at sensitive sessions.

Do not include credentials or account data in public bug reports. Use the
repository's private vulnerability-reporting facility if available; otherwise
contact the maintainer privately before sharing sensitive details.
