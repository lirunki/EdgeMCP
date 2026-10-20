"""Opt-in, authenticated SSH session broker for consenting PowerShell workers.

No commands execute on this server. They run in a separately started client
worker's existing PowerShell language mode. A timeout never implies cancellation.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

MARKER = '# EDGEMCP_REVERSEPS_BOOTSTRAP_END_V1'
MAX_FRAME = 65536
MAX_COMMAND = 4096
MAX_OUTPUT = 8192


@dataclass
class Pending:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


@dataclass
class Client:
    id: str
    session: Any
    loop: Any
    label: str
    language_mode: str
    username: str
    pending: Pending | None = None


class Broker:
    def __init__(self, script_path: Path | None = None):
        script_path = script_path or Path(__file__).resolve().parents[1] / 'scripts/reverse-powershell.ps1'
        # Read only our installed script, never a file requested by an SSH peer.
        script = script_path.read_text(encoding='utf-8')
        if MARKER in script.splitlines():
            raise ValueError('Bootstrap script contains the reserved marker line')
        self.bootstrap = script.rstrip() + '\n' + MARKER + '\n'
        self.clients: dict[str, Client] = {}
        self.lock = threading.Lock()

    def attach(self, session, hello: dict) -> Client:
        if (hello.get('version') != 1 or hello.get('accept_commands') is not True
                or hello.get('language_mode') not in ('FullLanguage', 'ConstrainedLanguage')):
            raise ValueError('Worker must explicitly accept commands in a supported language mode')
        label = hello.get('label', 'PowerShell worker')
        if not isinstance(label, str) or not 1 <= len(label) <= 64 or not label.isprintable():
            raise ValueError('Worker label must be 1-64 printable characters')
        connection = session.channel.get_connection()
        client = Client(uuid.uuid4().hex, session, asyncio.get_running_loop(), label,
                        hello['language_mode'], connection.get_extra_info('username') or '')
        with self.lock:
            if len(self.clients) >= 32:
                raise ValueError('Worker connection limit reached')
            self.clients[client.id] = client
        return client

    def detach(self, client: Client | None):
        if client is None:
            return
        with self.lock:
            if self.clients.pop(client.id, None) is not client:
                return
            if client.pending:
                client.pending.result = {
                    'client_id': client.id, 'request_id': client.pending.id,
                    'status': 'disconnected', 'execution_may_continue': True,
                    'message': 'Connection lost; execution state is unknown. Do not retry automatically.',
                }
                client.pending.done.set()
                client.pending = None

    def complete(self, client: Client, frame: dict):
        if (not isinstance(frame.get('output'), str) or len(frame['output']) > MAX_OUTPUT
                or type(frame.get('success')) is not bool
                or type(frame.get('truncated')) is not bool
                or (frame.get('native_exit_code') is not None
                    and type(frame['native_exit_code']) is not int)):
            raise ValueError('Invalid result fields or output limit exceeded')
        # Reject invalid surrogate text before it reaches MCP's UTF-8 serializer.
        frame['output'].encode('utf-8')
        with self.lock:
            pending = client.pending
            if pending is None or frame.get('id') != pending.id:
                raise ValueError('Unexpected result request ID')
            pending.result = {
                'client_id': client.id, 'request_id': pending.id, 'status': 'completed',
                'success': frame['success'], 'output': frame['output'],
                'truncated': frame['truncated'], 'native_exit_code': frame.get('native_exit_code'),
                'language_mode': client.language_mode,
            }
            client.pending = None
            pending.done.set()

    def run(self, command: str | None = None, client_id: str | None = None,
            timeout: int = 30000) -> dict:
        if command is None:
            if client_id is not None:
                raise ValueError('Omit both command and client_id to list workers')
            with self.lock:
                return {'clients': [
                    {'client_id': c.id, 'label': c.label, 'language_mode': c.language_mode,
                     'ssh_username': c.username, 'busy': c.pending is not None,
                     'pending_request_id': c.pending.id if c.pending else None}
                    for c in self.clients.values()
                ]}
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
            raise ValueError(f'command must contain 1-{MAX_COMMAND} characters')
        try:
            command.encode('utf-8')
        except UnicodeError:
            raise ValueError('command must be valid Unicode text') from None
        if not client_id:
            raise ValueError('An explicit client_id is required; call run_powershell() to list workers')
        if type(timeout) is not int or not 1 <= timeout <= 300000:
            raise ValueError('timeout must be 1-300000 milliseconds')
        with self.lock:
            client = self.clients.get(client_id)
            if client is None:
                raise ValueError('Unknown or disconnected client_id')
            if client.pending:
                raise ValueError('Client is busy; its prior command may still be executing')
            pending = Pending()
            client.pending = pending
            # Channel I/O belongs to the private SSH event loop, not the MCP thread.
            try:
                client.loop.call_soon_threadsafe(
                    client.session.send, {'type': 'command', 'id': pending.id, 'command': command}
                )
            except RuntimeError:
                client.pending = None
                raise RuntimeError('SSH loop is closed; command was not queued') from None
        if pending.done.wait(timeout / 1000):
            return pending.result
        with self.lock:
            if pending.done.is_set():
                return pending.result
            # Keep the client busy until the actual result or disconnect arrives.
            return {'client_id': client.id, 'request_id': pending.id, 'status': 'timeout',
                    'execution_may_continue': True,
                    'message': 'Wait timed out, not execution. Do not retry automatically.'}


class Session(asyncssh.SSHServerSession):
    """Bootstrap over stdout, followed by newline-delimited JSON on the same channel."""

    def __init__(self, broker: Broker):
        self.broker = broker
        self.channel = None
        self.client: Client | None = None
        self.buffer = ''
        self.handshake_timer = None
        self.closed = False

    def connection_made(self, channel):
        self.channel = channel

    def shell_requested(self):
        return True

    def exec_requested(self, _command):
        return False

    def subsystem_requested(self, _subsystem):
        return False

    def session_started(self):
        self.channel.write(self.broker.bootstrap)
        self.handshake_timer = asyncio.get_running_loop().call_later(60, self.close)

    def send(self, frame: dict):
        if self.closed:
            return
        try:
            self.channel.write(json.dumps(frame, ensure_ascii=True, separators=(',', ':')) + '\n')
        except (OSError, asyncssh.Error):
            self.close()

    def data_received(self, data, datatype):
        if self.closed:
            return
        if datatype is not None:
            self.close()
            return
        self.buffer += data
        try:
            while '\n' in self.buffer:
                line, self.buffer = self.buffer.split('\n', 1)
                if len(line.encode('utf-8')) > MAX_FRAME:
                    raise ValueError('Frame too long')
                if not line.strip():
                    continue
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError('Expected JSON object')
                kind = frame.get('type')
                if kind == 'ping':
                    continue
                if kind == 'bye':
                    self.close()
                    return
                if self.client is None:
                    if kind != 'hello':
                        raise ValueError('Expected worker hello')
                    self.client = self.broker.attach(self, frame)
                    self.handshake_timer.cancel()
                    self.send({'type': 'welcome', 'client_id': self.client.id, 'version': 1})
                elif kind == 'result':
                    self.broker.complete(self.client, frame)
                else:
                    raise ValueError('Unexpected frame type')
            if len(self.buffer.encode('utf-8')) > MAX_FRAME:
                raise ValueError('Incomplete frame too long')
        except (ValueError, TypeError, UnicodeError):
            # Do not log command/result content, even for a malformed frame.
            self.send({'type': 'error', 'message': 'ReversePS protocol error; closing session'})
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.handshake_timer:
            self.handshake_timer.cancel()
        self.broker.detach(self.client)
        if self.channel:
            try:
                if self.client is not None:
                    self.channel.write('{"type":"close"}\n')
                self.channel.exit(0)
            except (OSError, asyncssh.Error, RuntimeError):
                pass  # Peer may already have closed the channel.

    def connection_lost(self, _exc):
        self.close()

    def eof_received(self):
        self.close()
        return False
