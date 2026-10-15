import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from email.message import Message
from unittest.mock import Mock
import urllib.error

from api import DiskAPI, Failure, trusted_url
from engine import Engine, Store, cloud_path, component, safe_local
import vault


def metadata(path, kind='file', payload=b'', **extra):
    result = {
        'path': path,
        'name': path.rsplit('/', 1)[-1],
        'type': kind,
        'resource_id': 'id:' + path,
        'size': len(payload) if kind == 'file' else None,
        'sha256': hashlib.sha256(payload).hexdigest() if kind == 'file' else None,
        'md5': hashlib.md5(payload).hexdigest() if kind == 'file' else None,
        'modified': '2026-10-15T00:00:00Z',
    }
    result.update(extra)
    return result


class PagedAPI:
    def __init__(self, pages, payloads=None):
        self.pages = pages
        self.payloads = payloads or {}
        self.calls = []

    def identity(self):
        return 'synthetic-review-account'

    def metadata(self, path, offset=0):
        self.calls.append(('metadata', path, offset))
        return self.pages[path][offset]

    def download(self, path):
        self.calls.append(('download', path))
        return io.BytesIO(self.payloads[path])


class FailingThenGoodResponse:
    def __init__(self, payload):
        self.payload = payload
        self.first = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if self.first:
            self.first = False
            return self.payload[:2]
        return self.payload[2:]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yandex-review-', dir=Path(__file__).parents[1] / 'review_artifacts' / 'tester')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_recursive_pagination_empty_folder_and_csv_completeness(self):
        good = b'hello'
        pages = {
            'disk:/': [
                {'path': 'disk:/', 'type': 'dir', '_embedded': {'offset': 0, 'total': 2, 'items': [metadata('disk:/empty', 'dir'), metadata('disk:/good.txt', payload=good)]}},
            ],
            'disk:/empty': [{'path': 'disk:/empty', 'type': 'dir', '_embedded': {'offset': 0, 'total': 0, 'items': []}}],
        }
        api = PagedAPI(pages, {'disk:/good.txt': good})
        store = Store(self.root / 'state')
        Engine(store, api, interval=0, retries=1).run(self.root / 'backup', ['disk:/'])
        self.assertEqual((store.summary())[2], 1)
        self.assertTrue((self.root / 'backup' / 'good.txt').read_bytes() == good)
        self.assertTrue(all(folder['status'] == 'complete' for folder in store.folders()))
        exported = store.export(self.root / 'exports')
        self.assertEqual(sorted(p.name for p in exported.iterdir()), sorted([
            'inventory.csv', 'downloaded.csv', 'unresolved.csv',
            'inventory_errors.csv', 'not_current.csv', 'unverified.csv',
            'attempt_history.csv', 'run_status.csv',
        ]))
        self.assertIn('good.txt', (exported / 'downloaded.csv').read_text(encoding='utf-8-sig'))
        self.assertNotIn('good.txt', (exported / 'unresolved.csv').read_text(encoding='utf-8-sig'))

    def test_changed_page_becomes_incomplete_folder_error(self):
        pages = {
            'disk:/': [
                {'path': 'disk:/', 'type': 'dir', '_embedded': {'offset': 0, 'total': 2, 'items': [metadata('disk:/first.txt', payload=b'a')]}},
                {'path': 'disk:/', 'type': 'dir', '_embedded': {'offset': 1, 'total': 2, 'items': [metadata('disk:/second.txt', payload=b'b')]}},
            ],
        }
        class ChangingAPI(PagedAPI):
            def metadata(self, path, offset=0):
                if path == 'disk:/' and offset == 1:
                    return {'path': 'disk:/', 'type': 'dir', '_embedded': {'offset': 1, 'total': 3, 'items': [metadata('disk:/second.txt', payload=b'b')]}}
                return super().metadata(path, offset)
        store = Store(self.root / 'state')
        Engine(store, ChangingAPI(pages), interval=0, retries=1, backoff=1).run(self.root / 'backup', ['disk:/'])
        folder = store.folders()[0]
        self.assertEqual(folder['status'], 'error')
        self.assertEqual(folder['error'], 'inventory_changed_rescan_required')
        self.assertIn('inventory_errors.csv', [p.name for p in store.export(self.root / 'exports').iterdir()])

    def test_missing_hash_and_active_payload_stay_unresolved(self):
        pages = {'disk:/': [{'path': 'disk:/', 'type': 'dir', '_embedded': {
            'offset': 0, 'total': 2,
            'items': [metadata('disk:/unknown.bin', payload=b'x', sha256=None, md5=None), metadata('disk:/shortcut.lnk', payload=b'x')],
        }}]}
        api = PagedAPI(pages, {'disk:/unknown.bin': b'x', 'disk:/shortcut.lnk': b'x'})
        store = Store(self.root / 'state')
        Engine(store, api, interval=0, retries=1).run(self.root / 'backup', ['disk:/'])
        rows = {row['path']: row for row in store.rows('all') if row['kind'] == 'file'}
        self.assertEqual(rows['disk:/unknown.bin']['reason'], 'missing_cloud_hash')
        self.assertEqual(rows['disk:/shortcut.lnk']['status'], 'excluded_security')
        self.assertNotIn(('download', 'disk:/unknown.bin'), api.calls)

    def test_partial_failure_restarts_from_zero_and_keeps_evidence(self):
        payload = b'complete-payload'
        pages = {'disk:/': [{'path': 'disk:/', 'type': 'dir', '_embedded': {'offset': 0, 'total': 1, 'items': [metadata('disk:/file.bin', payload=payload)]}}]}
        store = Store(self.root / 'state')
        first_api = PagedAPI(pages)
        first_api.download = lambda path: FailingThenGoodResponse(payload)
        Engine(store, first_api, interval=0, retries=1).run(self.root / 'backup', ['disk:/'])
        row = next(row for row in store.rows('all') if row['kind'] == 'file')
        self.assertEqual(row['status'], 'retry_exhausted')
        partials = list((self.root / 'backup' / '.YandexBackupParts').glob('*.part'))
        self.assertTrue(partials)
        store.retry()
        Engine(store, PagedAPI(pages, {'disk:/file.bin': payload}), interval=0, retries=1).run(self.root / 'backup', ['disk:/'])
        self.assertEqual(store.rows('downloaded')[0]['status'], 'downloaded')
        self.assertEqual((self.root / 'backup' / 'file.bin').read_bytes(), payload)
        self.assertTrue(partials[0].exists())

    def test_existing_copy_requires_hash_and_conflicts_are_preserved(self):
        payload = b'expected'
        pages = {'disk:/': [{'path': 'disk:/', 'type': 'dir', '_embedded': {'offset': 0, 'total': 1, 'items': [metadata('disk:/same.txt', payload=payload)]}}]}
        store = Store(self.root / 'state')
        destination = self.root / 'backup'
        destination.mkdir()
        (destination / 'same.txt').write_bytes(payload)
        Engine(store, PagedAPI(pages, {'disk:/same.txt': b'network'}), interval=0, retries=1).run(destination, ['disk:/'])
        row = store.rows('downloaded')[0]
        self.assertEqual(row['verification'], 'cloud_hash_existing')
        self.assertEqual((destination / 'same.txt').read_bytes(), payload)
        (destination / 'same.txt').write_bytes(b'wrong')
        store.retry()
        Engine(store, PagedAPI(pages, {'disk:/same.txt': b'network'}), interval=0, retries=1).run(destination, ['disk:/'])
        row = next(row for row in store.rows('all') if row['kind'] == 'file')
        self.assertEqual(row['reason'], 'local_conflict')
        self.assertEqual((destination / 'same.txt').read_bytes(), b'wrong')

    def test_lock_contention_account_binding_and_paths(self):
        store = Store(self.root / 'state')
        first = Engine(store, PagedAPI({}), interval=0)
        second = Engine(store, PagedAPI({}), interval=0)
        with first.lease():
            with self.assertRaises(ValueError):
                with second.lease():
                    pass
        store.configure(self.root / 'dest', 'account-a')
        with self.assertRaises(ValueError):
            store.configure(self.root / 'dest', 'account-b')
        self.assertEqual(cloud_path('/disk/Photos'), 'disk:/Photos')
        with self.assertRaises(ValueError):
            cloud_path('disk:/../secret')
        self.assertTrue(component('CON.txt').startswith('%'))

    def test_reparse_point_is_rejected_when_windows_policy_allows_synthetic_link(self):
        link = self.root / 'link'
        target = self.root / 'target'
        target.mkdir()
        if hasattr(os, 'symlink'):
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'Windows symlink privilege unavailable: {exc}')
            with self.assertRaises(Failure):
                safe_local(self.root, 'link/file.txt')

    def test_api_security_and_error_mapping(self):
        self.assertTrue(trusted_url('https://storage.yandex.net/file'))
        self.assertFalse(trusted_url('http://storage.yandex.net/file'))
        self.assertFalse(trusted_url('https://evil.example/file'))
        with self.assertRaises(ValueError):
            DiskAPI('token\nvalue')
        api = DiskAPI('token')
        headers = Message(); headers['Retry-After'] = '9'
        api.opener = Mock()
        api.opener.open.side_effect = urllib.error.HTTPError('https://cloud-api.yandex.net/v1/disk/resources', 429, 'rate', headers, io.BytesIO())
        with self.assertRaises(Failure) as raised:
            api.metadata('disk:/')
        self.assertEqual(raised.exception.category, 'rate_limit')
        self.assertEqual(raised.exception.retry_after, 9)
        with self.assertRaises(ValueError):
            api.get('/resources/delete', path='disk:/x')

    def test_csv_formula_safety_and_case_collision_evidence(self):
        store = Store(self.root / 'state')
        with store.db() as db:
            store.observe(db, metadata('disk:/=bad.txt', payload=b'a'))
            store.observe(db, metadata('disk:/=BAD.txt', payload=b'b'))
        rows = store.rows('all')
        self.assertEqual(len({row['relative'].casefold() for row in rows}), 2)
        exported = store.export(self.root / 'exports')
        text = (exported / 'inventory.csv').read_text(encoding='utf-8-sig')
        self.assertIn("'=bad.txt", text)

    def test_auth_failure_is_terminal_and_recorded(self):
        class AuthAPI:
            def identity(self):
                return 'synthetic-auth-account'

            def metadata(self, path, offset=0):
                raise Failure('auth', 401)

        store = Store(self.root / 'state')
        Engine(store, AuthAPI(), interval=0, retries=3, backoff=1).run(self.root / 'backup', ['disk:/'])
        self.assertEqual(store.setting('last_run_status'), 'error')
        self.assertIn('auth', store.setting('last_run_error'))
        self.assertEqual(store.folders()[0]['status'], 'error')

    def test_rate_limit_retries_then_records_incomplete_folder(self):
        class RateAPI:
            def __init__(self):
                self.calls = 0

            def identity(self):
                return 'synthetic-rate-account'

            def metadata(self, path, offset=0):
                self.calls += 1
                raise Failure('rate_limit', 429, 0)

        api = RateAPI()
        store = Store(self.root / 'state')
        Engine(store, api, interval=0, retries=2, backoff=1).run(self.root / 'backup', ['disk:/'])
        self.assertEqual(api.calls, 2)
        self.assertEqual(store.folders()[0]['attempts'], 2)
        self.assertEqual(store.folders()[0]['status'], 'error')

    def test_dpapi_roundtrip_is_user_scoped_or_explicitly_unavailable(self):
        token = b'test1-synthetic-oauth-token'
        if os.name == 'nt':
            encrypted = vault.transform(token)
            self.assertNotEqual(encrypted, token)
            self.assertEqual(vault.transform(encrypted, decrypt=True), token)
        else:
            with self.assertRaises(RuntimeError):
                vault.transform(token)

    def test_metadata_change_resets_retry_fields_preserves_history_and_files(self):
        old_payload, new_payload = b'old', b'new'
        old_pages = {'disk:/': [{'path': 'disk:/', 'type': 'dir', '_embedded': {
            'offset': 0, 'total': 1, 'items': [metadata('disk:/versioned.bin', payload=old_payload, resource_id='old')],
        }}]}
        store = Store(self.root / 'state')
        destination = self.root / 'backup'
        Engine(store, PagedAPI(old_pages, {'disk:/versioned.bin': b'wrong'}), interval=0, retries=1, backoff=1).run(destination, ['disk:/'])
        old_row = next(row for row in store.rows('all') if row['kind'] == 'file')
        old_partial = Path(old_row['partial_path'])
        old_partial.write_bytes(b'old-partial-evidence')
        old_local = destination / 'old-local-evidence.bin'
        old_local.write_bytes(b'old-local-evidence')
        with store.db() as db:
            db.execute('UPDATE entries SET local_path=?,local_sha256=?,verification=?,next_attempt=?,http=? WHERE path=?',
                       (str(old_local), 'old-local-sha', 'old-verification', 999, 429, old_row['path']))
        old_attempts = len(store.rows('all'))
        store.rescan()
        new_pages = {'disk:/': [{'path': 'disk:/', 'type': 'dir', '_embedded': {
            'offset': 0, 'total': 1, 'items': [metadata('disk:/versioned.bin', payload=new_payload, resource_id='new')],
        }}]}
        engine = Engine(store, PagedAPI(new_pages, {'disk:/versioned.bin': b'wrong'}), interval=0, retries=1, backoff=1)
        engine.roots = ['disk:/']
        engine.scan()
        changed = next(row for row in store.rows('all') if row['kind'] == 'file')
        self.assertEqual(changed['cycle_attempts'], 0)
        self.assertEqual(changed['next_attempt'], 0)
        self.assertIsNone(changed['reason'])
        self.assertIsNone(changed['http'])
        self.assertIsNone(changed['local_path'])
        self.assertIsNone(changed['local_sha256'])
        self.assertIsNone(changed['verification'])
        self.assertIsNone(changed['partial_path'])
        self.assertTrue(old_partial.exists())
        self.assertTrue(old_local.exists())
        with store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE path=?", (changed['path'],)).fetchone()[0], 1)
            self.assertTrue(db.execute("SELECT 1 FROM history WHERE path=? AND event='cloud_metadata_changed'", (changed['path'],)).fetchone())
        engine.download_one(dict(changed))
        refreshed = next(row for row in store.rows('all') if row['kind'] == 'file')
        self.assertEqual(refreshed['cycle_attempts'], 1)
        self.assertEqual(refreshed['attempts'], old_row['attempts'] + 1)

    def test_unchanged_metadata_does_not_reset_retry_budget_or_evidence(self):
        payload = b'same'
        pages = {'disk:/': [{'path': 'disk:/', 'type': 'dir', '_embedded': {
            'offset': 0, 'total': 1, 'items': [metadata('disk:/same.bin', payload=payload, resource_id='same')],
        }}]}
        store = Store(self.root / 'state')
        destination = self.root / 'backup'
        Engine(store, PagedAPI(pages, {'disk:/same.bin': b'wrong'}), interval=0, retries=1, backoff=1).run(destination, ['disk:/'])
        first = next(row for row in store.rows('all') if row['kind'] == 'file')
        with store.db() as db:
            db.execute('UPDATE entries SET reason=?,next_attempt=?,http=?,local_path=?,verification=? WHERE path=?',
                       ('retry_exhausted', 123, 503, str(destination / 'old.bin'), 'old-verification', first['path']))
        store.rescan()
        engine = Engine(store, PagedAPI(pages, {'disk:/same.bin': b'wrong'}), interval=0, retries=1, backoff=1)
        engine.roots = ['disk:/']
        engine.scan()
        unchanged = next(row for row in store.rows('all') if row['kind'] == 'file')
        self.assertEqual(unchanged['cycle_attempts'], first['cycle_attempts'])
        self.assertEqual(unchanged['next_attempt'], 123)
        self.assertEqual(unchanged['reason'], 'retry_exhausted')
        self.assertEqual(unchanged['http'], 503)
        self.assertEqual(unchanged['local_path'], str(destination / 'old.bin'))
        self.assertEqual(unchanged['verification'], 'old-verification')


if __name__ == '__main__':
    unittest.main(verbosity=2)
