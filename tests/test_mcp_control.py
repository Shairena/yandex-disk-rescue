import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from agent_control import Channel, ControlError
from engine import Engine, Store, safe_local


APP_DIR = Path(__file__).parents[1]
ARTIFACT_DIR = APP_DIR / 'review_artifacts' / 'tester'


def metadata(path, kind='file', payload=b'payload', **extra):
    result = {
        'path': path,
        'name': path.rsplit('/', 1)[-1],
        'type': kind,
        'resource_id': 'mcp:' + path,
        'size': len(payload) if kind == 'file' else None,
        'sha256': hashlib.sha256(payload).hexdigest() if kind == 'file' else None,
        'md5': hashlib.md5(payload).hexdigest() if kind == 'file' else None,
        'modified': '2026-10-15T00:00:00Z',
    }
    result.update(extra)
    return result


class MCPControlTests(unittest.TestCase):
    def setUp(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='yandex-mcp-', dir=ARTIFACT_DIR)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return self.root / 'state'

    def profile(self, roots=('disk:/allowed',)):
        return {
            'destination': str(self.root / 'backup'),
            'roots': list(roots),
            'interval': 0,
            'retries': 1,
            'backoff': 1,
        }

    def live_channel(self, roots=('disk:/allowed',), credential=True):
        store = Store(self.state())
        channel = Channel(store)
        session = channel.open_session()
        channel.publish(session, 'idle', credential)
        channel.approve(session, self.profile(roots))
        return store, channel, session

    def seed(self, store, path, kind='file', payload=b'payload', status=None):
        with store.db() as db:
            store.observe(db, metadata(path, kind, payload))
            if status is not None:
                db.execute('UPDATE entries SET status=? WHERE path=?', (status, path))

    def mcp_batch(self, state, requests):
        proc = subprocess.Popen(
            [sys.executable, 'main.py', '--mcp', '--state', str(state)],
            cwd=APP_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = b''.join(json.dumps(request, ensure_ascii=False).encode() + b'\n' for request in requests)
        stdout, stderr = proc.communicate(payload, timeout=8)
        lines = [line for line in stdout.decode('utf-8').splitlines() if line]
        return proc.returncode, [json.loads(line) for line in lines], stderr.decode('utf-8')

    @staticmethod
    def tool_value(response):
        result = response['result']
        if result.get('isError'):
            return result, None
        return result, json.loads(result['content'][0]['text'])

    def test_stdio_initialize_tools_status_and_unknown_method(self):
        code, responses, stderr = self.mcp_batch(self.state(), [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'ping', 'params': {}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'unknown/method', 'params': {}},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertEqual([response['id'] for response in responses], [1, 2, 3, 4, 5])
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-06-18')
        tools = {tool['name']: tool for tool in responses[1]['result']['tools']}
        self.assertEqual(set(tools), {
            'get_status', 'list_entries', 'get_command',
            'start', 'scan', 'pause', 'resume', 'stop', 'retry', 'rescan', 'export',
        })
        self.assertTrue(responses[1]['result']['tools'])
        status_result, _ = self.tool_value(responses[2])
        self.assertTrue(status_result['isError'])
        self.assertEqual(status_result['content'][0]['text'], 'app_not_running')
        self.assertEqual(responses[3]['result'], {})
        self.assertEqual(responses[4]['error']['code'], -32601)

    def test_stdio_schema_errors_large_input_and_secret_non_disclosure(self):
        _, preinit, _ = self.mcp_batch(self.state(), [
            {'jsonrpc': '2.0', 'id': 99, 'method': 'tools/list', 'params': {}},
        ])
        self.assertEqual(preinit[0]['error']['code'], -32002)
        _, responses, _ = self.mcp_batch(self.state(), [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'start', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'start', 'arguments': {'request_id': 'not-a-uuid'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'get_status', 'arguments': {'sentinel': 'MCP_SECRET_SENTINEL'}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'unknown_tool', 'arguments': {}}},
        ])
        self.assertTrue(all(response['result']['isError'] for response in responses[1:]))
        text = json.dumps(responses, ensure_ascii=False)
        self.assertNotIn('MCP_SECRET_SENTINEL', text)
        self.assertIn('invalid_arguments', text)
        self.assertIn('request_id_must_be_canonical_uuid', text)

        proc = subprocess.Popen(
            [sys.executable, 'main.py', '--mcp', '--state', str(self.root / 'large-state')],
            cwd=APP_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(b'{' + b'a' * 70000 + b'}\n', timeout=8)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(stdout, b'')

    def test_stdio_live_status_inventory_and_command_ack(self):
        store, channel, session = self.live_channel()
        self.seed(store, 'disk:/allowed/file.txt', payload=b'x')
        self.seed(store, 'disk:/allowed/child.bin', payload=b'y')
        self.seed(store, 'disk:/allowedness/sibling.txt', payload=b'z')
        self.seed(store, 'disk:/outside.txt', payload=b'n')
        request_id = str(uuid.uuid4())
        code, responses, stderr = self.mcp_batch(self.state(), [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'get_status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'list_entries', 'arguments': {'view': 'all', 'offset': 0, 'limit': 2}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'start', 'arguments': {'request_id': request_id}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'get_command', 'arguments': {'request_id': request_id}}},
        ])
        self.assertEqual((code, stderr), (0, ''))
        _, status = self.tool_value(responses[1])
        self.assertTrue(status['enabled'])
        self.assertTrue(status['credential_present'])
        self.assertEqual(status['queue_files'], 2)
        self.assertEqual(status['last_run'], {'status': 'not_run_for_approved_profile'})
        self.assertNotIn('token', json.dumps(status).lower())
        _, page = self.tool_value(responses[2])
        self.assertEqual(page['total'], 2)
        self.assertEqual({row['path'] for row in page['rows']}, {'disk:/allowed/child.bin', 'disk:/allowed/file.txt'})
        _, queued = self.tool_value(responses[3])
        self.assertEqual(queued['status'], 'queued')
        _, command = self.tool_value(responses[4])
        self.assertEqual(command['status'], 'queued')

        calls = []
        channel.dispatch_one(session, lambda command, profile: calls.append((command, profile)) or {'outcome': 'started'})
        self.assertEqual(calls[0][0], 'start')
        code, responses, _ = self.mcp_batch(self.state(), [
            {'jsonrpc': '2.0', 'id': 5, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call', 'params': {'name': 'get_command', 'arguments': {'request_id': request_id}}},
            {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'start', 'arguments': {'request_id': request_id}}},
        ])
        self.assertEqual(code, 0)
        _, completed = self.tool_value(responses[1])
        self.assertEqual(completed['status'], 'completed')
        _, replay = self.tool_value(responses[2])
        self.assertEqual(replay['status'], 'completed')
        self.assertEqual(len(calls), 1)

    def test_status_sanitizes_run_error_and_requires_matching_roots(self):
        store, channel, _ = self.live_channel()
        store.setting('roots', 'disk:/allowed')
        store.setting('last_run_status', 'error')
        store.setting('last_run_error', 'auth (HTTP 401): MCP_SECRET_SENTINEL')
        status = channel.status()
        self.assertEqual(status['last_run'], {'status': 'error', 'error_category': 'auth'})
        self.assertNotIn('MCP_SECRET_SENTINEL', json.dumps(status))
        store.setting('roots', 'disk:/other')
        status = channel.status()
        self.assertEqual(status['last_run'], {'status': 'not_run_for_approved_profile'})

    def test_distinct_start_commands_share_one_synthetic_worker(self):
        store, channel, session = self.live_channel()
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        channel.submit('start', first_id)
        channel.submit('start', second_id)
        active = False
        starts = []

        def handler(command, profile):
            nonlocal active
            if active:
                return {'outcome': 'already_active'}
            active = True
            starts.append(command)
            return {'outcome': 'started'}

        channel.dispatch_one(session, handler)
        channel.dispatch_one(session, handler)
        self.assertEqual(starts, ['start'])
        self.assertEqual(channel.command(first_id)['result'], {'outcome': 'started'})
        self.assertEqual(channel.command(second_id)['result'], {'outcome': 'already_active'})

    def test_scope_filter_sibling_names_and_wildcards(self):
        store, channel, _ = self.live_channel(('disk:/a%',))
        for path in ('disk:/a%/inside.txt', 'disk:/abc/inside.txt', 'disk:/a%more.txt'):
            self.seed(store, path)
        page = channel.inventory('all', 0, 200)
        self.assertEqual({row['path'] for row in page['rows']}, {'disk:/a%/inside.txt'})


    def test_revoke_permission_cancels_queued_and_hides_inventory(self):
        store, channel, session = self.live_channel()
        self.seed(store, 'disk:/allowed/file.txt')
        request_id = str(uuid.uuid4())
        channel.submit('scan', request_id)
        channel.approve(session)
        status = channel.status()
        self.assertFalse(status['enabled'])
        with self.assertRaises(ControlError):
            channel.inventory('all', 0, 100)
        with self.assertRaises(ControlError):
            channel.submit('scan', str(uuid.uuid4()))
        with store.db() as db:
            self.assertEqual(db.execute('SELECT status FROM agent_commands WHERE request_id=?', (request_id,)).fetchone()[0], 'cancelled')

    def test_retry_rescan_and_export_are_scoped_to_approved_roots(self):
        store, channel, session = self.live_channel()
        self.seed(store, 'disk:/allowed/retry.txt', status='retry')
        self.seed(store, 'disk:/outside/retry.txt', status='retry')
        self.seed(store, 'disk:/allowed/folder', kind='dir')
        self.seed(store, 'disk:/outside/folder', kind='dir')
        with store.db() as db:
            db.execute("UPDATE folders SET status='error',error='old' WHERE path IN ('disk:/allowed/folder','disk:/outside/folder')")
        retry_id = str(uuid.uuid4())
        channel.submit('retry', retry_id)
        channel.dispatch_one(session, lambda command, profile: (store.retry(profile['roots']) or {'outcome': 'applied'}))
        rows = {row['path']: row for row in store.rows('all')}
        self.assertEqual(rows['disk:/allowed/retry.txt']['status'], 'pending')
        self.assertEqual(rows['disk:/outside/retry.txt']['status'], 'retry')
        folders = {row['path']: row for row in store.folders()}
        self.assertEqual(folders['disk:/allowed/folder']['status'], 'pending')
        self.assertEqual(folders['disk:/outside/folder']['status'], 'error')

        with store.db() as db:
            db.execute("INSERT INTO folder_items VALUES ('disk:/allowed/folder','disk:/allowed/retry.txt')")
            db.execute("INSERT INTO folder_items VALUES ('disk:/outside/folder','disk:/outside/retry.txt')")
        rescan_id = str(uuid.uuid4())
        channel.submit('rescan', rescan_id)
        channel.dispatch_one(session, lambda command, profile: (store.rescan(profile['roots']) or {'outcome': 'applied'}))
        with store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM folder_items WHERE folder=?', ('disk:/allowed/folder',)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM folder_items WHERE folder=?', ('disk:/outside/folder',)).fetchone()[0], 1)

        export_id = str(uuid.uuid4())
        channel.submit('export', export_id)
        channel.dispatch_one(session, lambda command, profile: {'outcome': 'exported', 'directory': str(store.export(store.directory / 'agent-exports', roots=profile['roots']))})
        command = channel.command(export_id)
        directory = Path(command['result']['directory'])
        self.assertTrue(directory.is_relative_to(store.directory / 'agent-exports'))
        self.assertEqual(sorted(path.name for path in directory.iterdir()), [
            'attempt_history.csv', 'downloaded.csv', 'inventory.csv', 'inventory_errors.csv', 'run_status.csv', 'unresolved.csv',
        ])
        self.assertIn('allowed', (directory / 'inventory.csv').read_text(encoding='utf-8-sig'))
        self.assertNotIn('outside', (directory / 'inventory.csv').read_text(encoding='utf-8-sig'))

    def test_agent_export_rejects_preexisting_reparse_output(self):
        store, channel, session = self.live_channel()
        target = self.root / 'external-export-target'
        target.mkdir()
        output = store.directory / 'agent-exports'
        try:
            output.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Windows reparse privilege unavailable: {exc}')
        request_id = str(uuid.uuid4())
        channel.submit('export', request_id)
        channel.dispatch_one(session, lambda command, profile: {
            'outcome': 'exported',
            'directory': str(store.export(safe_local(store.directory, 'agent-exports'), roots=profile['roots'])),
        })
        result = channel.command(request_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['result'], {'error': 'command_failed_check_gui'})

    def test_idempotency_pending_limit_and_concurrent_producers(self):
        _, channel, _ = self.live_channel()
        first = str(uuid.uuid4())
        self.assertEqual(channel.submit('scan', first)['status'], 'queued')
        self.assertEqual(channel.submit('scan', first)['status'], 'queued')
        with self.assertRaises(ControlError) as conflict:
            channel.submit('start', first)
        self.assertEqual(str(conflict.exception), 'request_id_conflict')
        for _ in range(31):
            channel.submit('scan', str(uuid.uuid4()))
        with self.assertRaises(ControlError) as limit:
            channel.submit('scan', str(uuid.uuid4()))
        self.assertEqual(str(limit.exception), 'too_many_pending_commands')

        store, channel, _ = self.live_channel()
        results, errors = [], []
        def producer():
            try:
                results.append(channel.submit('scan', str(uuid.uuid4()))['status'])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=producer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, ['queued'] * 8)
        with store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM agent_commands WHERE status='queued'").fetchone()[0], 8)

    def test_restart_marks_queued_stale_and_executing_ambiguous(self):
        store, channel, session = self.live_channel()
        queued_id = str(uuid.uuid4())
        executing_id = str(uuid.uuid4())
        channel.submit('scan', queued_id)
        channel.submit('start', executing_id)
        with store.db() as db:
            db.execute("UPDATE agent_commands SET status='executing' WHERE request_id=?", (executing_id,))
        restarted = Channel(store)
        new_session = restarted.open_session()
        with store.db() as db:
            self.assertEqual(db.execute('SELECT status FROM agent_commands WHERE request_id=?', (queued_id,)).fetchone()[0], 'stale_session')
            self.assertEqual(db.execute('SELECT status FROM agent_commands WHERE request_id=?', (executing_id,)).fetchone()[0], 'ambiguous')
        restarted.publish(new_session, 'idle', False)
        restarted.approve(new_session, self.profile())
        self.assertEqual(restarted.command(queued_id)['result'], None)
        self.assertEqual(restarted.command(executing_id)['result'], None)

    def test_startup_recovery_only_marks_selected_root(self):
        store = Store(self.root / 'startup-state')
        self.seed(store, 'disk:/allowed/unfinished.bin', status='downloading')
        self.seed(store, 'disk:/outside/unfinished.bin', status='downloading')
        with store.db() as db:
            db.execute("INSERT INTO attempts(path,status) VALUES ('disk:/allowed/unfinished.bin','downloading')")
            db.execute("INSERT INTO attempts(path,status) VALUES ('disk:/outside/unfinished.bin','downloading')")

        class EmptyAPI:
            def identity(self):
                return 'synthetic-mcp-account'

            def metadata(self, path, offset=0):
                return {'path': path, 'type': 'dir', '_embedded': {'offset': offset, 'total': 0, 'items': []}}

        Engine(store, EmptyAPI(), interval=0, retries=1).run(self.root / 'backup', ['disk:/allowed'], scan_only=True)
        rows = {row['path']: row for row in store.rows('all')}
        self.assertEqual(rows['disk:/allowed/unfinished.bin']['status'], 'pending')
        self.assertEqual(rows['disk:/outside/unfinished.bin']['status'], 'downloading')
        with store.db() as db:
            attempts = {row['path']: row for row in db.execute('SELECT * FROM attempts')}
        self.assertIsNotNone(attempts['disk:/allowed/unfinished.bin']['finished'])
        self.assertIsNone(attempts['disk:/outside/unfinished.bin']['finished'])

    def test_stale_heartbeat_and_gui_lease_fail_closed(self):
        store, channel, session = self.live_channel()
        with store.db() as db:
            db.execute('UPDATE agent_session SET heartbeat=? WHERE session=?', (0, session))
        with self.assertRaises(ControlError) as status_error:
            channel.status()
        self.assertEqual(str(status_error.exception), 'app_not_running')
        with self.assertRaises(ControlError) as submit_error:
            channel.submit('scan', str(uuid.uuid4()))
        self.assertEqual(str(submit_error.exception), 'app_not_running')

        first = Channel(Store(self.root / 'lease-state'))
        second = Channel(Store(self.root / 'lease-state'))
        with first.gui_lease():
            with self.assertRaises(ControlError) as lease_error:
                with second.gui_lease():
                    pass
        self.assertEqual(str(lease_error.exception), 'gui_already_open')

    def test_handler_failure_hides_exception_and_pause_has_checkpoint(self):
        store, channel, session = self.live_channel()
        request_id = str(uuid.uuid4())
        channel.submit('scan', request_id)
        channel.dispatch_one(session, lambda command, profile: (_ for _ in ()).throw(RuntimeError('MCP_SECRET_SENTINEL')))
        result = channel.command(request_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['result'], {'error': 'command_failed_check_gui'})
        self.assertNotIn('MCP_SECRET_SENTINEL', json.dumps(result))

        engine = Engine(Store(self.root / 'pause-state'), api=None, interval=0)
        engine.pause.set()
        thread = threading.Thread(target=engine.check)
        thread.start()
        self.assertTrue(engine.paused.wait(2))
        self.assertTrue(thread.is_alive())
        engine.pause.clear()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(engine.paused.is_set())


if __name__ == '__main__':
    unittest.main()
