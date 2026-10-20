"""Mini SSHD tests include an in-process end-to-end remote TCP forward."""
import asyncio
import contextlib
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import asyncssh

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import mini_sshd
import server


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PolicyTests(unittest.TestCase):
    def test_only_configured_loopback_forwards_are_allowed(self):
        policy = mini_sshd._ForwardServer((9333, 9444))
        for host in ("", "localhost", "127.0.0.1", "::1", "[::1]"):
            for port in (9333, 9444):
                with self.subTest(host=host, port=port):
                    self.assertTrue(policy.server_requested(host, port))
        for host, port in (("0.0.0.0", 9333), ("example.test", 9444), ("127.0.0.1", 9334), ("", 0)):
            with self.subTest(host=host, port=port):
                self.assertFalse(policy.server_requested(host, port))

    def test_only_configured_loopback_direct_destinations_are_allowed(self):
        policy = mini_sshd._ForwardServer(9333, backward_ports=(8080, 8443))
        for host in ("localhost", "127.0.0.1", "::1", "[::1]"):
            for port in (8080, 8443):
                with self.subTest(host=host, port=port):
                    self.assertTrue(
                        policy.connection_requested(host, port, "127.0.0.1", 50000)
                    )
        for host, port in (
            ("", 8080), ("0.0.0.0", 8080), ("example.test", 8080),
            ("127.0.0.1", 8081),
        ):
            with self.subTest(host=host, port=port):
                self.assertFalse(
                    policy.connection_requested(host, port, "127.0.0.1", 50000)
                )

    def test_ports_are_validated(self):
        for value in (0, 65536, -1, "bad", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mini_sshd.MiniSSHD("127.0.0.1", value, 9333, authorized_keys="keys", host_key="host")
            with self.subTest(forward_port=value), self.assertRaises(ValueError):
                mini_sshd.MiniSSHD(
                    "127.0.0.1", 2222, 9333, authorized_keys="keys",
                    host_key="host", forward_ports=[value],
                )
            with self.subTest(backward_port=value), self.assertRaises(ValueError):
                mini_sshd.MiniSSHD(
                    "127.0.0.1", 2222, 9333, authorized_keys="keys",
                    host_key="host", backward_ports=[value],
                )

    def test_forward_ports_are_ordered_and_deduplicated(self):
        sshd = mini_sshd.MiniSSHD(
            "127.0.0.1", 2222, 9333, authorized_keys="keys",
            host_key="host", forward_ports=[9444, "9333", "9555", 9444],
            backward_ports=[8080, "8443", 8080],
        )
        self.assertEqual(sshd.forward_ports, (9333, 9444, 9555))
        self.assertEqual(sshd.backward_ports, (8080, 8443))

    def test_host_key_is_created_once_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state" / "host_key"
            first = mini_sshd._ensure_host_key(path)
            contents = path.read_bytes()
            self.assertEqual(first, path)
            self.assertIn(b"OPENSSH PRIVATE KEY", contents)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(mini_sshd._ensure_host_key(path), path)
            self.assertEqual(path.read_bytes(), contents)

    def test_missing_authorized_keys_fails_before_thread_start(self):
        with tempfile.TemporaryDirectory() as root:
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), 9333,
                authorized_keys=Path(root) / "missing",
                host_key=Path(root) / "host",
            )
            with self.assertRaisesRegex(ValueError, "authorized keys"):
                sshd.start()
            self.assertIsNone(sshd._thread)

    def test_password_file_validation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            password_file = root / "password"
            password_file.write_text("one\ntwo\n", encoding="utf-8")
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), 9333, authorized_keys=None,
                host_key=root / "host", username="forwarder",
                password_file=password_file,
            )
            with self.assertRaisesRegex(ValueError, "one non-empty"):
                sshd.start()

            password_file.write_text("secret\n", encoding="utf-8")
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), 9333, authorized_keys=None,
                host_key=root / "host", password_file=password_file,
            )
            with self.assertRaisesRegex(ValueError, "requires --sshd-user"):
                sshd.start()


class ForwardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_identification_banner_is_immediate(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            key = asyncssh.generate_private_key("ssh-ed25519")
            authorized = root / "authorized_keys"
            authorized.write_bytes(key.export_public_key("openssh"))
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), free_port(),
                authorized_keys=authorized, host_key=root / "host_key",
            )
            sshd.start()
            self.addCleanup(sshd.stop)

            def read_banner():
                with socket.create_connection(("127.0.0.1", sshd.port), timeout=2) as client:
                    return client.makefile("rb").readline(256)

            banner = await asyncio.to_thread(read_banner)
            self.assertTrue(banner.startswith(b"SSH-2.0-"), banner)

    async def test_authenticated_multiple_remote_forwards_carry_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            client_key = asyncssh.generate_private_key("ssh-ed25519")
            authorized = root / "authorized_keys"
            authorized.write_bytes(client_key.export_public_key("openssh"))
            ssh_port, remote_port, second_remote_port = (
                free_port(), free_port(), free_port()
            )
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", ssh_port, remote_port,
                authorized_keys=authorized,
                host_key=root / "host_key",
                forward_ports=[second_remote_port],
            )
            sshd.start()
            self.addCleanup(sshd.stop)

            async def echo(reader, writer):
                writer.write(await reader.read(100))
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
            self.addAsyncCleanup(echo_server.wait_closed)
            self.addCleanup(echo_server.close)
            echo_port = echo_server.sockets[0].getsockname()[1]

            async with asyncssh.connect(
                "127.0.0.1", ssh_port, username="forwarder",
                client_keys=[client_key], known_hosts=None,
            ) as connection:
                listeners = [
                    await connection.forward_remote_port(
                        "127.0.0.1", port, "127.0.0.1", echo_port
                    )
                    for port in (remote_port, second_remote_port)
                ]
                for index, port in enumerate((remote_port, second_remote_port), 1):
                    with self.subTest(port=port):
                        reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        payload = f"tunnel {index}".encode()
                        writer.write(payload)
                        await writer.drain()
                        self.assertEqual(await reader.readexactly(len(payload)), payload)
                        writer.close()
                        await writer.wait_closed()
                for listener in listeners:
                    listener.close()
                    await listener.wait_closed()

    async def test_authenticated_direct_forward_carries_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            client_key = asyncssh.generate_private_key("ssh-ed25519")
            authorized = root / "authorized_keys"
            authorized.write_bytes(client_key.export_public_key("openssh"))

            async def echo(reader, writer):
                writer.write(await reader.read(100))
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
            self.addAsyncCleanup(echo_server.wait_closed)
            self.addCleanup(echo_server.close)
            echo_port = echo_server.sockets[0].getsockname()[1]
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), free_port(),
                authorized_keys=authorized, host_key=root / "host_key",
                backward_ports=[echo_port],
            )
            sshd.start()
            self.addCleanup(sshd.stop)

            async with asyncssh.connect(
                "127.0.0.1", sshd.port, username="forwarder",
                client_keys=[client_key], known_hosts=None,
            ) as connection:
                listener = await connection.forward_local_port(
                    "127.0.0.1", 0, "127.0.0.1", echo_port
                )
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", listener.get_port()
                )
                writer.write(b"backward tunnel")
                await writer.drain()
                self.assertEqual(await reader.readexactly(15), b"backward tunnel")
                writer.close()
                await writer.wait_closed()
                listener.close()
                await listener.wait_closed()

    async def test_password_file_authentication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            password_file = root / "password"
            password_file.write_text("correct horse battery staple\n", encoding="utf-8")
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), free_port(), authorized_keys=None,
                host_key=root / "host_key", username="forwarder",
                password_file=password_file,
            )
            sshd.start()
            self.addCleanup(sshd.stop)

            async with asyncssh.connect(
                "127.0.0.1", sshd.port, username="forwarder",
                password="correct horse battery staple", client_keys=None,
                known_hosts=None,
            ):
                pass
            for username, password in (("forwarder", "wrong"), ("wrong-user", "correct horse battery staple")):
                with self.subTest(username=username), self.assertRaises(asyncssh.PermissionDenied):
                    await asyncssh.connect(
                        "127.0.0.1", sshd.port, username=username,
                        password=password, client_keys=None, known_hosts=None,
                    )

    async def test_unauthorized_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            allowed = asyncssh.generate_private_key("ssh-ed25519")
            authorized = root / "authorized_keys"
            authorized.write_bytes(allowed.export_public_key("openssh"))
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), free_port(),
                authorized_keys=authorized, host_key=root / "host_key",
            )
            sshd.start()
            self.addCleanup(sshd.stop)
            denied = asyncssh.generate_private_key("ssh-ed25519")
            with self.assertRaises(asyncssh.PermissionDenied):
                await asyncssh.connect(
                    "127.0.0.1", sshd.port, username="any-name",
                    client_keys=[denied], known_hosts=None,
                )

    async def test_remote_commands_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            client_key = asyncssh.generate_private_key("ssh-ed25519")
            authorized = root / "authorized_keys"
            authorized.write_bytes(client_key.export_public_key("openssh"))
            sshd = mini_sshd.MiniSSHD(
                "127.0.0.1", free_port(), free_port(),
                authorized_keys=authorized, host_key=root / "host_key",
            )
            sshd.start()
            self.addCleanup(sshd.stop)
            async with asyncssh.connect(
                "127.0.0.1", sshd.port, username="forwarder",
                client_keys=[client_key], known_hosts=None,
            ) as connection:
                with self.assertRaises(asyncssh.ChannelOpenError):
                    await connection.run("id")


class CLITests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.stack.enter_context(patch.object(server, "_list_pages", return_value=[]))
        self.run = self.stack.enter_context(patch.object(server.mcp, "run"))
        self.sshd_class = self.stack.enter_context(patch.object(server.mini_sshd, "MiniSSHD"))
        self.sshd = self.sshd_class.return_value

    def test_cli_starts_and_stops_sshd_around_mcp(self):
        argv = [
            "server.py", "--sshd", "127.0.0.1:2222", "--cdp", "127.0.0.1:9333",
            "--sshd-forward-port", "9444", "--sshd-forward-port", "9555",
            "--ssh-backward-port", "8080", "--ssh-backward-port", "8443",
            "--sshd-authorized-keys", "allowed", "--sshd-host-key", "host",
            "--sshd-user", "forwarder", "--sshd-password-file", "password.txt",
        ]
        with patch.object(sys, "argv", argv):
            server.main()
        self.sshd_class.assert_called_once_with(
            "127.0.0.1", "2222", "9333", authorized_keys="allowed", host_key="host",
            username="forwarder", password_file="password.txt",
            forward_ports=["9444", "9555"], backward_ports=["8080", "8443"],
        )
        self.sshd.start.assert_called_once_with()
        self.run.assert_called_once()
        self.sshd.stop.assert_called_once_with()

    def test_password_file_requires_user(self):
        with patch.object(sys, "argv", [
            "server.py", "--sshd", "2222", "--sshd-password-file", "password.txt"
        ]), self.assertRaises(SystemExit):
            server.main()
        self.sshd_class.assert_not_called()
        self.run.assert_not_called()

    def test_sshd_requires_loopback_cdp(self):
        with patch.object(sys, "argv", ["server.py", "--sshd", "2222", "--cdp", "edge.test:9333"]), self.assertRaises(SystemExit):
            server.main()
        self.sshd_class.assert_not_called()
        self.run.assert_not_called()

    def test_sshd_and_launch_edge_are_mutually_exclusive(self):
        with patch.object(sys, "argv", ["server.py", "--sshd", "2222", "--launch-edge"]), self.assertRaises(SystemExit):
            server.main()
        self.sshd_class.assert_not_called()
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
