"""ReversePS protocol tests plus optional real Windows CLM/OpenSSH integration."""
import asyncio
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import asyncssh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import mini_sshd
import reverse_ps
import server


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def skip_bootstrap(process):
    code = []
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), 5)
        if not line:
            raise AssertionError('EOF before bootstrap marker')
        if line.rstrip('\r\n') == reverse_ps.MARKER:
            break
        code.append(line)
    return ''.join(code)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = asyncssh.generate_private_key('ssh-ed25519')
        self.authorized = self.root / 'authorized'
        self.authorized.write_bytes(self.key.export_public_key())
        self.broker = reverse_ps.Broker()
        self.sshd = mini_sshd.MiniSSHD('127.0.0.1', free_port(), free_port(),
            authorized_keys=self.authorized, host_key=self.root / 'host', reverse_broker=self.broker)
        self.sshd.start()
        self.addCleanup(self.sshd.stop)

    async def connect(self, hello=True):
        connection = await asyncssh.connect('127.0.0.1', self.sshd.port,
            username='worker', client_keys=[self.key], known_hosts=None)
        self.addAsyncCleanup(connection.wait_closed)
        self.addCleanup(connection.close)
        process = await connection.create_process()
        code = await skip_bootstrap(process)
        self.assertIn('AcceptCommands', code)
        if not hello:
            return connection, process, None
        process.stdin.write(json.dumps({'type': 'hello', 'version': 1, 'accept_commands': True,
            'label': 'test worker', 'language_mode': 'ConstrainedLanguage'}) + '\n')
        welcome = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        self.assertEqual(welcome['type'], 'welcome')
        return connection, process, welcome['client_id']

    async def test_bootstrap_eof_download_has_only_script_and_marker(self):
        connection = await asyncssh.connect('127.0.0.1', self.sshd.port,
            username='worker', client_keys=[self.key], known_hosts=None)
        self.addAsyncCleanup(connection.wait_closed)
        self.addCleanup(connection.close)
        process = await connection.create_process()
        process.stdin.write_eof()
        code = await asyncio.wait_for(process.stdout.read(), 5)
        self.assertTrue(code.endswith(reverse_ps.MARKER + '\n'))
        self.assertEqual(code.count('\n' + reverse_ps.MARKER + '\n'), 1)
        self.assertEqual(self.broker.run()['clients'], [])

    async def test_two_workers_cannot_complete_each_others_request(self):
        _, first, cid1 = await self.connect()
        _, second, cid2 = await self.connect()
        self.assertNotEqual(cid1, cid2)
        task = asyncio.create_task(asyncio.to_thread(self.broker.run, 'Get-Date', cid1, 2000))
        command = json.loads(await first.stdout.readline())
        fake = {'type': 'result', 'id': command['id'], 'output': 'wrong client',
                'success': True, 'truncated': False, 'native_exit_code': 0}
        second.stdin.write(json.dumps(fake) + '\n')
        self.assertEqual(json.loads(await second.stdout.readline())['type'], 'error')
        self.assertFalse(task.done())
        fake['output'] = 'right client'
        first.stdin.write(json.dumps(fake) + '\n')
        self.assertEqual((await task)['output'], 'right client')

    async def test_oversized_result_disconnects_without_completing(self):
        _, process, cid = await self.connect()
        task = asyncio.create_task(asyncio.to_thread(self.broker.run, 'Get-Date', cid, 2000))
        command = json.loads(await process.stdout.readline())
        process.stdin.write(json.dumps({'type': 'result', 'id': command['id'], 'output': 'x' * 8193,
            'success': True, 'truncated': False, 'native_exit_code': 0}) + '\n')
        self.assertEqual(json.loads(await process.stdout.readline())['type'], 'error')
        self.assertEqual((await task)['status'], 'disconnected')

    async def test_serial_roundtrips_and_default_listing(self):
        _, process, cid = await self.connect()
        self.assertEqual(self.broker.run()['clients'][0]['client_id'], cid)
        for index in range(2):
            task = asyncio.create_task(asyncio.to_thread(self.broker.run, f'Write-Output {index}', cid, 3000))
            command = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
            self.assertEqual(command['command'], f'Write-Output {index}')
            process.stdin.write(json.dumps({'type': 'result', 'id': command['id'], 'output': f'{index}\n',
                'success': True, 'truncated': False, 'native_exit_code': 0}) + '\n')
            result = await task
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['output'], f'{index}\n')
            self.assertFalse(self.broker.run()['clients'][0]['busy'])

    async def test_timeout_stays_busy_until_late_reply(self):
        _, process, cid = await self.connect()
        result = await asyncio.to_thread(self.broker.run, 'Start-Sleep 1', cid, 50)
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(result['execution_may_continue'])
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.broker.run('Get-Date', cid, 10)
        command = json.loads(await process.stdout.readline())
        process.stdin.write(json.dumps({'type': 'result', 'id': command['id'], 'output': '',
            'success': True, 'truncated': False, 'native_exit_code': 0}) + '\n')
        for _ in range(100):
            if not self.broker.run()['clients'][0]['busy']:
                break
            await asyncio.sleep(.01)
        self.assertFalse(self.broker.run()['clients'][0]['busy'])

    async def test_disconnect_reports_unknown_execution(self):
        connection, process, cid = await self.connect()
        task = asyncio.create_task(asyncio.to_thread(self.broker.run, 'Get-Date', cid, 5000))
        await process.stdout.readline()
        connection.close()
        result = await task
        self.assertEqual(result['status'], 'disconnected')
        self.assertTrue(result['execution_may_continue'])
        self.assertEqual(self.broker.run()['clients'], [])

    async def test_fragmentation_and_protocol_id_enforcement(self):
        _, process, cid = await self.connect()
        process.stdin.write('{"type":"pi')
        process.stdin.write('ng"}\n')
        process.stdin.write(json.dumps({'type': 'result', 'id': 'wrong', 'output': '',
            'success': True, 'truncated': False, 'native_exit_code': 0}) + '\n')
        error = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
        self.assertEqual(error['type'], 'error')
        await process.wait_closed()
        self.assertEqual(self.broker.run()['clients'], [])

    async def test_no_consent_and_oversize_are_rejected(self):
        _, process, _ = await self.connect(hello=False)
        process.stdin.write('{"type":"hello","version":1,"accept_commands":false}\n')
        self.assertEqual(json.loads(await process.stdout.readline())['type'], 'error')
        self.assertEqual(self.broker.run()['clients'], [])
        _, process, _ = await self.connect(hello=False)
        process.stdin.write('x' * (reverse_ps.MAX_FRAME + 1))
        self.assertEqual(json.loads(await process.stdout.readline())['type'], 'error')

    async def test_command_validation_and_explicit_target(self):
        _, _, cid = await self.connect()
        for kwargs in ({'command': 'Get-Date'}, {'command': 'Get-Date', 'client_id': 'missing'},
                       {'command': ' '}, {'command': 'a' * 4097, 'client_id': cid},
                       {'command': 'Get-Date', 'client_id': cid, 'timeout': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.broker.run(**kwargs)

    async def test_unauthorized_key_cannot_get_bootstrap(self):
        with self.assertRaises(asyncssh.PermissionDenied):
            await asyncssh.connect('127.0.0.1', self.sshd.port, username='worker',
                client_keys=[asyncssh.generate_private_key('ssh-ed25519')], known_hosts=None)

    async def test_server_exec_and_subsystems_still_rejected_in_reverse_mode(self):
        connection, _, _ = await self.connect(hello=False)
        with self.assertRaises(asyncssh.ChannelOpenError):
            await connection.run('must-not-execute-on-server')
        with self.assertRaises(asyncssh.ChannelOpenError):
            await connection.create_process(subsystem='sftp')
        self.assertEqual(self.broker.run()['clients'], [])

    async def test_default_server_retains_inert_shell(self):
        policy = mini_sshd._ForwardServer(9333)
        session = policy.session_requested()
        self.assertIsInstance(session, mini_sshd._ForwardSession)
        channel = Mock()
        session.connection_made(channel)
        session.session_started()
        self.assertIn('forwarding-only', channel.write.call_args.args[0])
        self.assertFalse(session.exec_requested('anything'))
        self.assertFalse(session.subsystem_requested('powershell'))


class CLITests(unittest.TestCase):
    def test_reverseps_requires_sshd_and_tool_is_not_registered_by_default(self):
        self.assertNotIn('run_powershell', {tool.name for tool in asyncio.run(server.mcp.list_tools())})
        with patch.object(sys, 'argv', ['server.py', '--reversePS']), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            server.main()

    def test_reverseps_tool_is_registered_only_during_opted_in_server(self):
        stack = contextlib.ExitStack()
        with stack:
            for key in ('CDP_HOST', 'CDP_PORT', 'MCP_HOST', 'MCP_PORT', 'MCP_PATH', '_reverse_broker'):
                stack.enter_context(patch.object(server, key, getattr(server, key)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(server, '_list_pages', return_value=[]))
            sshd = stack.enter_context(patch.object(server.mini_sshd, 'MiniSSHD'))
            def check_tool(**_kwargs):
                tools = asyncio.run(server.mcp.list_tools())
                self.assertIn('run_powershell', {t.name for t in tools})
                self.assertEqual(server.run_powershell(), {'clients': []})
            stack.enter_context(patch.object(server.mcp, 'run', side_effect=check_tool))
            stack.enter_context(patch.object(sys, 'argv', ['server.py', '--sshd', '2222', '--cdp', '127.0.0.1:9333', '--reversePS']))
            server.main()
            self.assertIsInstance(sshd.call_args.kwargs['reverse_broker'], reverse_ps.Broker)
            sshd.return_value.stop.assert_called_once()
            self.assertNotIn('run_powershell', {t.name for t in asyncio.run(server.mcp.list_tools())})


@unittest.skipUnless(sys.platform == 'win32' and shutil.which('ssh.exe') and shutil.which('powershell.exe'),
                     'Real client test requires Windows PowerShell/OpenSSH')
class WindowsClientTests(ProtocolTests):
    async def test_real_ps51_constrained_worker(self):
        import win32api
        import win32security
        import ntsecuritycon
        identity = self.root / 'client_key'
        identity.write_bytes(self.key.export_private_key('openssh'))
        # Test harness ACL only. The PS client does not create/manage SSH keys.
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(str(identity), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        known = self.root / 'known_hosts'
        host_key = asyncssh.read_private_key(self.root / 'host')
        known.write_bytes(f'[127.0.0.1]:{self.sshd.port} '.encode() + host_key.export_public_key())
        script = (ROOT / 'scripts/reverse-powershell.ps1').read_text()
        quote = lambda s: "'" + str(s).replace("'", "''") + "'"
        # Tighten this test process; do not change execution policy or relax mode.
        command = "$ExecutionContext.SessionState.LanguageMode='ConstrainedLanguage'; & {\n" + script + '\n} '
        command += f'-Server 127.0.0.1 -Port {self.sshd.port} -User worker -IdentityFile {quote(identity)} -KnownHostsFile {quote(known)} -AcceptCommands -MaxSessionSeconds 15'
        process = await asyncio.create_subprocess_exec('powershell.exe', '-NoProfile', '-NonInteractive',
            '-Command', command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            for _ in range(300):
                clients = self.broker.run()['clients']
                if clients:
                    break
                if process.returncode is not None:
                    break
                await asyncio.sleep(.05)
            if not clients:
                if process.returncode is None:
                    out, err = await asyncio.wait_for(process.communicate(), 25)
                else:
                    out, err = await process.communicate()
                self.fail('Worker did not register: ' + out.decode(errors='replace') + err.decode(errors='replace'))
            cid = clients[0]['client_id']
            self.assertEqual(len(self.sshd._connections), 1)
            for cmd, expected in (
                ('$ExecutionContext.SessionState.LanguageMode', 'ConstrainedLanguage'),
                ("Write-Output 'first reply'", 'first reply'),
                ("Write-Output ('caf' + [char]233)", 'caf\u00e9'),
                ("Write-Output '\U0001f600'", '\U0001f600'),
            ):
                result = await asyncio.to_thread(self.broker.run, cmd, cid, 5000)
                self.assertEqual(result['status'], 'completed', result)
                self.assertTrue(result['success'], result)
                self.assertIn(expected, result['output'])
            error = await asyncio.to_thread(self.broker.run, "throw 'expected failure'", cid, 5000)
            self.assertFalse(error['success'])
            truncated = await asyncio.to_thread(self.broker.run, "Write-Output ('x' * 10000)", cid, 5000)
            self.assertTrue(truncated['truncated'])
            self.assertEqual(len(truncated['output']), 8192)
            paired = await asyncio.to_thread(self.broker.run, "Write-Output (('x' * 8191) + '\U0001f600')", cid, 5000)
            self.assertTrue(paired['truncated'])
            self.assertEqual(paired['output'], 'x' * 8191)
            # CLM restrictions must survive: no .NET object construction bypass.
            blocked = await asyncio.to_thread(self.broker.run, 'New-Object System.Net.Sockets.TcpClient', cid, 5000)
            self.assertFalse(blocked['success'])
            result = await asyncio.to_thread(self.broker.run, 'Start-Sleep -Milliseconds 400; Write-Output done', cid, 50)
            self.assertEqual(result['status'], 'timeout')
            for _ in range(100):
                if not self.broker.run()['clients'][0]['busy']:
                    break
                await asyncio.sleep(.05)
            self.assertFalse(self.broker.run()['clients'][0]['busy'])
            # Ask the server to close only this worker. Its pipeline should exit.
            session = self.broker.clients[cid].session
            self.broker.clients[cid].loop.call_soon_threadsafe(session.close)
            out, err = await asyncio.wait_for(process.communicate(), 20)
            self.assertNotIn(b'OperationStopped', err)
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()


if __name__ == '__main__':
    unittest.main()
