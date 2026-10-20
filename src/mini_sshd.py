"""Small in-process SSH server for constrained remote TCP forwards.

The server supports public-key and optional file-backed password authentication.
Remote TCP listeners and direct TCP destinations must be loopback-only and use
explicitly allowed ports, preventing use as a general-purpose SSH proxy.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import stat
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import asyncssh

_LOOPBACK_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def _port(value: str | int, label: str) -> int:
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError(f"{label} must be an integer from 1 to 65535")
    result = int(value)
    if not 1 <= result <= 65535:
        raise ValueError(f"{label} must be an integer from 1 to 65535")
    return result


def _ports(values: str | int | Iterable[str | int], label: str) -> tuple[int, ...]:
    """Validate ports, preserving order while removing duplicates."""
    if isinstance(values, (str, int)):
        values = (values,)
    result = tuple(dict.fromkeys(_port(value, label) for value in values))
    if not result:
        raise ValueError(f"{label} must contain at least one port")
    return result


def _ensure_host_key(path: str | os.PathLike[str]) -> Path:
    """Return an existing host key, or atomically create a private Ed25519 key."""
    key_path = Path(path).expanduser()
    if key_path.is_file():
        return key_path
    if key_path.exists():
        raise ValueError(f"SSH host key path is not a regular file: {key_path}")

    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(key_path.parent, stat.S_IRWXU)
    except OSError:
        pass
    encoded = asyncssh.generate_private_key("ssh-ed25519").export_private_key("openssh")
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # Another EdgeMCP process created it concurrently.
        if not key_path.is_file():
            raise ValueError(f"SSH host key path is not a regular file: {key_path}")
        return key_path
    with os.fdopen(fd, "wb") as output:
        output.write(encoded)
    return key_path


class _ForwardSession(asyncssh.SSHServerSession):
    """Inert shell used only to keep `ssh -R` without `-N` connected."""

    def connection_made(self, channel) -> None:
        self._channel = channel

    def shell_requested(self) -> bool:
        return True

    def exec_requested(self, command: str) -> bool:
        return False  # Never execute (or pretend to execute) a remote command.

    def subsystem_requested(self, subsystem: str) -> bool:
        return False

    def session_started(self) -> None:
        self._channel.write(
            "EdgeMCP forwarding-only connection; remote commands are disabled.\n"
        )

    def data_received(self, data, datatype) -> None:
        pass  # No command interpreter exists.

    def eof_received(self) -> bool:
        self._channel.exit(0)
        return False


class _ForwardServer(asyncssh.SSHServer):
    def __init__(
        self,
        allowed_ports: str | int | Iterable[str | int],
        connections: set[Any] | None = None,
        username: str | None = None,
        password: str | None = None,
        backward_ports: Iterable[str | int] = (),
    ):
        self._allowed_ports = frozenset(_ports(allowed_ports, "Allowed forward port"))
        self._backward_ports = frozenset(
            _port(port, "SSH backward port") for port in backward_ports
        )
        self._connections = connections
        self._username = username
        self._password = password
        self._connection = None
        self._peer = None

    def connection_made(self, connection) -> None:
        self._connection = connection
        if self._connections is not None:
            self._connections.add(connection)
        self._peer = connection.get_extra_info("peername")
        print(f"[edgemcp:sshd] TCP connection from {self._peer}.", flush=True)

    def connection_lost(self, exc) -> None:
        detail = f" ({exc})" if exc else ""
        print(f"[edgemcp:sshd] Connection from {self._peer} closed{detail}.", flush=True)
        if self._connections is not None:
            self._connections.discard(self._connection)

    def password_auth_supported(self) -> bool:
        return self._password is not None

    def validate_password(self, username: str, password: str) -> bool:
        return (
            self._username is not None
            and self._password is not None
            and secrets.compare_digest(username, self._username)
            and secrets.compare_digest(password, self._password)
        )

    def session_requested(self) -> _ForwardSession:
        return _ForwardSession()

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        """Authorize only an allowed `-R` listener bound on this server's loopback."""
        host = (listen_host or "").lower().strip("[]")
        return host in _LOOPBACK_HOSTS and listen_port in self._allowed_ports

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        """Authorize `-L` only to an allowed loopback destination port."""
        host = (dest_host or "").lower().strip("[]")
        return bool(host) and host in _LOOPBACK_HOSTS and dest_port in self._backward_ports


class MiniSSHD:
    """Run AsyncSSH on a private background asyncio loop."""

    def __init__(
        self,
        host: str,
        port: str | int,
        cdp_port: str | int,
        *,
        authorized_keys: str | os.PathLike[str] | None,
        host_key: str | os.PathLike[str],
        username: str | None = None,
        password_file: str | os.PathLike[str] | None = None,
        forward_ports: Iterable[str | int] = (),
        backward_ports: Iterable[str | int] = (),
    ):
        self.host = host
        self.port = _port(port, "SSHD port")
        self.cdp_port = _port(cdp_port, "CDP forward port")
        self.forward_ports = _ports(
            (self.cdp_port, *forward_ports), "SSHD forward port"
        )
        self.backward_ports = tuple(dict.fromkeys(
            _port(value, "SSH backward port") for value in backward_ports
        ))
        self.authorized_keys = Path(authorized_keys).expanduser() if authorized_keys else None
        self.host_key = Path(host_key).expanduser()
        self.username = username
        self.password_file = Path(password_file).expanduser() if password_file else None
        self._password: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener: Any = None
        self._connections: set[Any] = set()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @staticmethod
    def _handshake_error(connection, exc) -> None:
        if exc is not None:
            peer = connection.get_extra_info("peername")
            print(f"[edgemcp:sshd] SSH handshake from {peer} failed: {exc}.", flush=True)

    async def _open(self) -> None:
        self._listener = await asyncssh.listen(
            self.host,
            self.port,
            server_factory=lambda: _ForwardServer(
                self.forward_ports,
                self._connections,
                self.username,
                self._password,
                self.backward_ports,
            ),
            server_host_keys=[str(self.host_key)],
            authorized_client_keys=(
                str(self.authorized_keys)
                if self.authorized_keys is not None and self.authorized_keys.is_file()
                else None
            ),
            agent_forwarding=False,
            x11_forwarding=False,
            line_editor=False,
            error_handler=self._handshake_error,
        )

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._open())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        else:
            self._ready.set()
            loop.run_forever()
        finally:
            if self._listener is not None:
                self._listener.close()
                loop.run_until_complete(self._listener.wait_closed())
            connections = list(self._connections)
            for connection in connections:
                connection.close()
            if connections:
                loop.run_until_complete(asyncio.gather(
                    *(connection.wait_closed() for connection in connections),
                    return_exceptions=True,
                ))
            loop.close()

    def start(self, timeout: float = 10) -> None:
        if self._thread is not None:
            raise RuntimeError("Mini SSHD has already been started")

        if self.password_file is not None:
            if not self.username:
                raise ValueError("--sshd-password-file requires --sshd-user")
            if not self.password_file.is_file():
                raise ValueError(f"SSH password file does not exist: {self.password_file}")
            try:
                password = self.password_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"Could not read SSH password file: {exc}") from exc
            if password.endswith("\r\n"):
                password = password[:-2]
            elif password.endswith("\n"):
                password = password[:-1]
            if not password or "\n" in password or "\r" in password:
                raise ValueError("SSH password file must contain one non-empty UTF-8 line")
            self._password = password

        has_keys = self.authorized_keys is not None and self.authorized_keys.is_file()
        if not has_keys and self._password is None:
            location = self.authorized_keys or "(not configured)"
            raise ValueError(
                f"SSH authorized keys file does not exist: {location}. Create it, "
                "pass --sshd-authorized-keys PATH, or configure --sshd-user and "
                "--sshd-password-file."
            )
        self.host_key = _ensure_host_key(self.host_key)
        self._thread = threading.Thread(target=self._run, name="edgemcp-sshd", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise RuntimeError("Timed out starting the in-process SSH server")
        if self._error is not None:
            self._thread.join(timeout=1)
            raise RuntimeError(f"Could not start SSHD on {self.host}:{self.port}: {self._error}") from self._error

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
