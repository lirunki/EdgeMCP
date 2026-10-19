"""Launch tests use mocked processes: no real Edge or services are started."""
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import edge_launcher as launcher
import server

VERSION = 'Edg/123.0.0.0'


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.process = Mock(pid=12345)
        self.process.poll.return_value = None
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(launcher._ATTEMPTS, {}, clear=True))
        self.stack.enter_context(patch.object(launcher, '_READY', set()))
        self.probe = self.stack.enter_context(patch.object(launcher, '_probe_edge'))
        self.find = self.stack.enter_context(patch.object(launcher, '_find_edge', return_value=self.root / 'Edge binary'))
        self.profile = self.stack.enter_context(patch.object(launcher, '_profile_path', return_value=self.root / 'profile with spaces'))
        self.spawn = self.stack.enter_context(patch.object(launcher.subprocess, 'Popen', return_value=self.process))

    def test_custom_cdp_port_and_safe_arguments(self):
        self.probe.side_effect = [None, VERSION]
        result = launcher.launch('localhost', '9444')
        args = self.spawn.call_args.args[0]
        self.assertIn('--remote-debugging-port=9444', args)
        self.assertIn('--remote-debugging-address=127.0.0.1', args)
        self.assertIn('--user-data-dir=' + str(self.root / 'profile with spaces'), args)
        self.assertEqual(args[-1], 'about:blank')
        self.assertIs(self.spawn.call_args.kwargs['shell'], False)
        self.assertEqual(self.spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(result['started'])
        self.assertEqual(result['cdp_url'], 'http://127.0.0.1:9444')
        self.assertEqual(result['launch_pid'], 12345)
        self.profile.assert_called_once_with(9444)

    def test_existing_edge_is_reused_without_launch(self):
        self.probe.return_value = VERSION
        result = launcher.launch('127.0.0.1', 9222)
        self.assertFalse(result['started'])
        self.assertTrue(result['already_running'])
        self.spawn.assert_not_called()
        self.find.assert_not_called()

    def test_remote_or_wildcard_host_rejected(self):
        for host in ('browser.example.test', '0.0.0.0'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                launcher.launch(host, 9222)
        self.probe.assert_not_called()
        self.spawn.assert_not_called()

    def test_invalid_ports_rejected(self):
        for port in (0, 65536, -1, 'abc', '9222;command', 2.5, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                launcher.launch('127.0.0.1', port)
        self.spawn.assert_not_called()

    def test_occupied_port_not_reused_or_killed(self):
        self.probe.side_effect = RuntimeError('Port occupied by non-Edge service')
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            launcher.launch('127.0.0.1', 9222)
        self.spawn.assert_not_called()
        self.process.kill.assert_not_called()

    def test_missing_executable(self):
        self.probe.return_value = None
        self.find.side_effect = RuntimeError('Microsoft Edge was not found')
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            launcher.launch('127.0.0.1', 9222)
        self.spawn.assert_not_called()

    def test_spawn_failure(self):
        self.probe.return_value = None
        self.spawn.side_effect = OSError('launch denied')
        with self.assertRaisesRegex(RuntimeError, 'Could not start Edge'):
            launcher.launch('127.0.0.1', 9222)
        self.assertNotIn(9222, launcher._ATTEMPTS)

    def test_waits_for_readiness_after_spawn(self):
        self.probe.side_effect = [None, None, RuntimeError('initializing'), VERSION]
        self.assertTrue(launcher.launch('127.0.0.1', 9222)['started'])
        self.assertEqual(self.probe.call_count, 4)

    def test_process_exit_is_error_not_success(self):
        self.probe.return_value = None
        self.process.poll.return_value = 3
        with self.assertRaisesRegex(RuntimeError, 'exited with code 3'):
            launcher.launch('127.0.0.1', 9222)
        self.assertNotIn(9222, launcher._ATTEMPTS)

    def test_timeout_retry_does_not_spawn_duplicates(self):
        self.probe.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
            launcher.launch('127.0.0.1', 9222, timeout=0.01)
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        self.probe.side_effect = [None, VERSION]
        result = launcher.launch('127.0.0.1', 9222)
        self.assertFalse(result['started'])
        self.assertEqual(self.spawn.call_count, 1)

    def test_restart_after_previously_ready_browser_closed(self):
        self.probe.side_effect = [None, VERSION, None, VERSION]
        launcher.launch('127.0.0.1', 9222)
        launcher.launch('127.0.0.1', 9222)
        self.assertEqual(self.spawn.call_count, 2)


class DiscoveryTests(unittest.TestCase):
    def test_edge_path_override_and_invalid_override(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / 'edge-test'
            binary.touch()
            binary.chmod(0o700)
            with patch.dict(os.environ, {'EDGE_PATH': str(binary)}, clear=True):
                self.assertEqual(launcher._find_edge(), binary.resolve())
            with patch.dict(os.environ, {'EDGE_PATH': str(binary) + '-missing'}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'EDGE_PATH'):
                    launcher._find_edge()

    def test_windows_install_discovery_uses_environment(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / 'Microsoft/Edge/Application/msedge.exe'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.object(launcher.sys, 'platform', 'win32'), patch.dict(os.environ, {'PROGRAMFILES': root}, clear=True):
                self.assertEqual(launcher._find_edge(), binary.resolve())

    def test_profile_is_port_specific_and_override_is_respected(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(launcher.sys, 'platform', 'linux'), patch.dict(os.environ, {'XDG_STATE_HOME': root}, clear=True):
                self.assertEqual(launcher._profile_path(9333), (Path(root) / 'EdgeMCP/debug-profile-9333').resolve())
                self.assertNotEqual(launcher._profile_path(9333), launcher._profile_path(9334))
            with patch.dict(os.environ, {'EDGE_PROFILE_DIR': root}, clear=True):
                self.assertEqual(launcher._profile_path(9333), Path(root).resolve())


class ProbeTests(unittest.TestCase):
    def test_connection_refused_means_not_running(self):
        with patch.object(launcher.socket, 'create_connection', side_effect=ConnectionRefusedError):
            self.assertIsNone(launcher._probe_edge(9222))

    def test_only_edge_is_accepted(self):
        for product, succeeds in ((VERSION, True), ('Chrome/123', False), ('', False)):
            with self.subTest(product=product), patch.object(launcher.socket, 'create_connection'), patch.object(launcher.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({'Browser': product}).encode()
                if succeeds:
                    self.assertEqual(launcher._probe_edge(9444), VERSION)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'non-Edge'):
                        launcher._probe_edge(9444)
                self.assertEqual(opener.return_value.open.call_args.args[0], 'http://127.0.0.1:9444/json/version')


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for key in ('CDP_HOST', 'CDP_PORT', 'MCP_HOST', 'MCP_PORT', 'MCP_PATH'):
            self.stack.enter_context(patch.object(server, key, getattr(server, key)))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.stack.enter_context(patch.object(server, '_list_pages', return_value=[]))
        self.run = self.stack.enter_context(patch.object(server.mcp, 'run'))
        self.launch = self.stack.enter_context(patch.object(server.edge_launcher, 'launch', return_value={'cdp_url': 'http://127.0.0.1:9444', 'browser': VERSION}))

    def test_cli_launches_on_cdp_not_mcp_port(self):
        with patch.object(sys, 'argv', ['server.py', '--launch-edge', '--cdp', '127.0.0.1:9444', '--listen', '127.0.0.1:8089']):
            server.main()
        self.launch.assert_called_once_with('127.0.0.1', '9444')
        self.assertEqual(self.run.call_args.kwargs['port'], 8089)

    def test_cli_without_flag_does_not_launch(self):
        with patch.object(sys, 'argv', ['server.py']):
            server.main()
        self.launch.assert_not_called()
        self.run.assert_called_once()

    def test_cli_startup_failure_prevents_serving(self):
        self.launch.side_effect = RuntimeError('Could not start Edge')
        with patch.object(sys, 'argv', ['server.py', '--launch-edge']), self.assertRaises(SystemExit) as error:
            server.main()
        self.assertEqual(error.exception.code, 2)
        self.run.assert_not_called()

    def test_cli_rejects_conflicting_listener_port_before_launch(self):
        with patch.object(sys, 'argv', ['server.py', '--launch-edge', '--cdp', '127.0.0.1:8089', '--listen', '127.0.0.1:8089']), self.assertRaises(SystemExit):
            server.main()
        self.launch.assert_not_called()
        self.run.assert_not_called()

    def test_tool_registered_and_callable_through_sdk(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertIn('launch_edge', {tool.name for tool in tools})
        result = asyncio.run(server.mcp.call_tool('launch_edge', {}))
        self.assertFalse(result.is_error)
        self.launch.assert_called_once_with(server.CDP_HOST, server.CDP_PORT)

    def test_tool_uses_configured_endpoint(self):
        server.CDP_HOST, server.CDP_PORT = 'localhost', '9555'
        self.assertEqual(server.launch_edge()['browser'], VERSION)
        self.launch.assert_called_once_with('localhost', '9555')


if __name__ == '__main__':
    unittest.main()
