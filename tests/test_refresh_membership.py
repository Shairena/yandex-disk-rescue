"""Offline membership-refresh regressions using isolated SQLite state and fake APIs."""
import csv
from contextlib import closing
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_control import Channel
from api import Failure
from engine import Engine, Interrupted, Store


APP_DIR = Path(__file__).parents[1]
SCRATCH_ROOT = APP_DIR / 'review_artifacts' / 'refresh_membership_tests'


def metadata(path, kind='file', payload=b'payload', **extra):
    value = {
        'path': path,
        'name': path.rsplit('/', 1)[-1],
        'type': kind,
        'resource_id': 'synthetic:' + path,
        'size': len(payload) if kind == 'file' else None,
        'sha256': hashlib.sha256(payload).hexdigest() if kind == 'file' else None,
        'md5': hashlib.md5(payload).hexdigest() if kind == 'file' else None,
        'modified': '2026-10-15T00:00:00Z',
    }
    value.update(extra)
    return value


def page(path, items=(), *, total=None, offset=0):
    items = list(items)
    return {
        'path': path,
        'type': 'dir',
        '_embedded': {
            'offset': offset,
            'total': len(items) if total is None else total,
            'items': items,
        },
    }


class FakeAPI:
    def __init__(self, responses, on_metadata=None):
        self.responses = responses
        self.on_metadata = on_metadata
        self.calls = []
        self.download_calls = []

    def identity(self):
        return 'synthetic-refresh-account'

    def metadata(self, path, offset=0):
        self.calls.append((path, offset))
        if self.on_metadata:
            self.on_metadata(path, offset)
        response = self.responses.get((path, offset))
        if response is None:
            raise AssertionError(f'unexpected metadata request: {path} offset={offset}')
        if isinstance(response, Exception):
            raise response
        return response

    def download(self, path):
        self.download_calls.append(path)
        raise AssertionError('refresh membership tests must not download cloud content')


class NoWaitEngine(Engine):
    def wait(self, seconds):
        self.check()


class RefreshMembershipTests(unittest.TestCase):
    def setUp(self):
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=SCRATCH_ROOT)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, api, roots=('disk:/',)):
        normalized = list(roots)
        self.store.seed(normalized)
        engine = NoWaitEngine(self.store, api, interval=0, retries=1, backoff=1)
        engine.roots = normalized
        engine.scan()
        return engine

    def initial_tree(self):
        old = b'old-child-payload'
        responses = {
            ('disk:/', 0): page('disk:/', [metadata('disk:/gone', 'dir')], total=2),
            ('disk:/', 1): page('disk:/', [metadata('disk:/keep.txt')], total=2, offset=1),
            ('disk:/gone', 0): page('disk:/gone', [metadata('disk:/gone/sub', 'dir')]),
            ('disk:/gone/sub', 0): page('disk:/gone/sub', [
                metadata('disk:/gone/sub/old.bin', payload=old),
                metadata('disk:/gone/sub/failed.bin', payload=b'failed-child'),
            ]),
        }
        api = FakeAPI(responses)
        self.scan(api)
        return api

    def test_complete_multipage_listing_marks_absent_descendants_inactive_and_retains_history(self):
        self.initial_tree()
        previous = {row['path']: row for row in self.store.rows('all')}
        self.store.rescan(['disk:/'])
        refreshed = FakeAPI({
            ('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')], total=2),
            ('disk:/', 1): page('disk:/', [metadata('disk:/new.txt')], total=2, offset=1),
        })
        self.scan(refreshed)

        rows = {row['path']: row for row in self.store.rows('all')}
        self.assertEqual(rows['disk:/gone']['presence'], 'not_current')
        self.assertEqual(rows['disk:/gone/sub']['presence'], 'not_current')
        self.assertEqual(rows['disk:/gone/sub/old.bin']['presence'], 'not_current')
        self.assertEqual(rows['disk:/keep.txt']['presence'], 'observed')
        self.assertEqual(rows['disk:/new.txt']['presence'], 'observed')
        self.assertEqual(set(self.store.rows('pending')[i]['path'] for i in range(len(self.store.rows('pending')))),
                         {'disk:/keep.txt', 'disk:/new.txt'})
        self.assertNotIn(('disk:/gone', 0), refreshed.calls)
        self.assertNotIn(('disk:/gone/sub', 0), refreshed.calls)
        with self.store.db() as db:
            self.assertGreater(db.execute("SELECT count(*) FROM history WHERE event='membership_not_current'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM entries WHERE path=?', ('disk:/gone/sub/old.bin',)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM folder_items WHERE folder=?', ('disk:/',)).fetchone()[0], 2)
        self.assertIn('disk:/gone/sub/old.bin', previous)

    def test_failed_changed_total_and_duplicate_pages_do_not_establish_absence(self):
        for failure_kind in ('request_failure', 'incomplete_page', 'changed_total', 'duplicate'):
            with self.subTest(failure_kind=failure_kind):
                self.store = Store(self.root / failure_kind)
                self.initial_tree()
                self.store.rescan(['disk:/'])
                if failure_kind == 'request_failure':
                    responses = {
                        ('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')], total=2),
                        ('disk:/', 1): Failure('service_unavailable', 503),
                    }
                elif failure_kind == 'incomplete_page':
                    responses = {
                        ('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')], total=2),
                        ('disk:/', 1): page('disk:/', [], total=2, offset=1),
                    }
                elif failure_kind == 'changed_total':
                    responses = {
                        ('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')], total=2),
                        ('disk:/', 1): page('disk:/', [metadata('disk:/new.txt')], total=3, offset=1),
                    }
                else:
                    responses = {
                        ('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')], total=2),
                        ('disk:/', 1): page('disk:/', [metadata('disk:/keep.txt')], total=2, offset=1),
                    }
                self.scan(FakeAPI(responses))
                rows = {row['path']: row for row in self.store.rows('all')}
                self.assertEqual(rows['disk:/gone']['presence'], 'unverified')
                self.assertEqual(rows['disk:/gone/sub/old.bin']['presence'], 'unverified')
                root_folder = next(folder for folder in self.store.folders() if folder['path'] == 'disk:/')
                self.assertEqual(root_folder['presence'], 'observed')
                self.assertEqual(root_folder['status'], 'error')

    def test_interrupted_refresh_resumes_at_saved_offset_before_reconciling(self):
        self.initial_tree()
        self.store.rescan(['disk:/'])
        engine_holder = {}

        def stop_after_first_page(path, offset):
            if path == 'disk:/' and offset == 0:
                engine_holder['engine'].stop.set()

        first_api = FakeAPI({
            ('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')], total=2),
        }, on_metadata=stop_after_first_page)
        self.store.seed(['disk:/'])
        first = NoWaitEngine(self.store, first_api, interval=0, retries=1, backoff=1)
        first.roots = ['disk:/']
        engine_holder['engine'] = first
        with self.assertRaises(Interrupted):
            first.scan()
        folder = next(folder for folder in self.store.folders() if folder['path'] == 'disk:/')
        self.assertEqual((folder['offset'], folder['total'], folder['status']), (1, 2, 'pending'))
        self.assertEqual(next(row for row in self.store.rows('all') if row['path'] == 'disk:/gone')['presence'], 'unverified')

        resumed = FakeAPI({
            ('disk:/', 1): page('disk:/', [metadata('disk:/new.txt')], total=2, offset=1),
        })
        self.scan(resumed)
        self.assertEqual(resumed.calls, [('disk:/', 1)])
        rows = {row['path']: row for row in self.store.rows('all')}
        self.assertEqual(rows['disk:/gone']['presence'], 'not_current')
        self.assertEqual(rows['disk:/gone/sub/old.bin']['presence'], 'not_current')

    def test_legacy_schema_migrates_additively_and_seed_does_not_promote_children(self):
        legacy_dir = self.root / 'legacy'
        legacy_dir.mkdir()
        db_path = legacy_dir / 'queue.sqlite3'
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript('''
CREATE TABLE entries(path TEXT PRIMARY KEY, resource_id TEXT, kind TEXT NOT NULL,
 size INTEGER, sha256 TEXT, md5 TEXT, modified TEXT, relative TEXT UNIQUE COLLATE NOCASE,
 status TEXT NOT NULL, reason TEXT, http INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
 cycle_attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT, next_attempt REAL NOT NULL DEFAULT 0,
 local_path TEXT, local_sha256 TEXT, verification TEXT, partial_path TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE folders(path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
 total INTEGER, status TEXT NOT NULL DEFAULT 'pending', error TEXT, http INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0, updated TEXT);
CREATE TABLE observations(id INTEGER PRIMARY KEY, recorded TEXT, path TEXT, metadata TEXT);
CREATE TABLE folder_items(folder TEXT,path TEXT,PRIMARY KEY(folder,path));
CREATE TABLE history(id INTEGER PRIMARY KEY, recorded TEXT, path TEXT, event TEXT, detail TEXT);
CREATE TABLE attempts(id INTEGER PRIMARY KEY, path TEXT, started TEXT, finished TEXT,
 status TEXT, reason TEXT, http INTEGER, partial_path TEXT, partial_bytes INTEGER);
''')
            db.execute("INSERT INTO entries(path,kind,relative,status,local_path,local_sha256,verification) VALUES ('disk:/','dir','','directory','kept-local','kept-sha','kept-verification')")
            db.execute("INSERT INTO entries(path,kind,relative,status) VALUES ('disk:/legacy','dir','legacy','directory')")
            db.execute("INSERT INTO folders(path,status) VALUES ('disk:/','complete')")
            db.execute("INSERT INTO folders(path,status) VALUES ('disk:/legacy','complete')")

        migrated = Store(legacy_dir)
        rows = {row['path']: row for row in migrated.rows('all')}
        folders = {row['path']: row for row in migrated.folders()}
        self.assertEqual(rows['disk:/']['presence'], 'unverified')
        self.assertEqual(rows['disk:/legacy']['presence'], 'unverified')
        self.assertEqual(rows['disk:/legacy']['parent'], 'disk:/')
        self.assertEqual(rows['disk:/']['local_path'], 'kept-local')
        self.assertEqual(rows['disk:/']['local_sha256'], 'kept-sha')
        self.assertEqual(rows['disk:/']['verification'], 'kept-verification')
        self.assertEqual(folders['disk:/legacy']['presence'], 'unverified')
        self.assertEqual(folders['disk:/legacy']['status'], 'complete')
        migrated.seed(['disk:/'])
        self.assertEqual(next(row for row in migrated.rows('all') if row['path'] == 'disk:/legacy')['presence'], 'unverified')

    def test_reopen_backfills_null_parent_after_interrupted_additive_migration(self):
        partial_dir = self.root / 'partial-migration'
        partial_dir.mkdir()
        db_path = partial_dir / 'queue.sqlite3'
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript('''
CREATE TABLE entries(path TEXT PRIMARY KEY, kind TEXT NOT NULL, parent TEXT,
 presence TEXT NOT NULL DEFAULT 'unverified');
CREATE TABLE folders(path TEXT PRIMARY KEY, presence TEXT NOT NULL DEFAULT 'unverified');
''')
            db.execute("INSERT INTO entries(path,kind,parent) VALUES ('disk:/parent/child','dir',NULL)")
            db.execute("INSERT INTO entries(path,kind,parent) VALUES ('disk:/','dir',NULL)")
        reopened = Store(partial_dir)
        rows = {row['path']: row for row in reopened.rows('all')}
        self.assertEqual(rows['disk:/parent/child']['parent'], 'disk:/parent')
        self.assertIsNone(rows['disk:/']['parent'])

    def test_two_concurrent_openers_serialize_legacy_schema_migration(self):
        failures = []
        for attempt in range(2):
            legacy_dir = self.root / f'concurrent-legacy-{attempt}'
            legacy_dir.mkdir()
            db_path = legacy_dir / 'queue.sqlite3'
            with closing(sqlite3.connect(db_path)) as db, db:
                db.executescript('''
CREATE TABLE entries(path TEXT PRIMARY KEY, kind TEXT NOT NULL);
CREATE TABLE folders(path TEXT PRIMARY KEY);
CREATE TABLE observations(id INTEGER PRIMARY KEY, recorded TEXT, path TEXT, metadata TEXT);
CREATE TABLE folder_items(folder TEXT,path TEXT,PRIMARY KEY(folder,path));
CREATE TABLE history(id INTEGER PRIMARY KEY, recorded TEXT, path TEXT, event TEXT, detail TEXT);
CREATE TABLE attempts(id INTEGER PRIMARY KEY, path TEXT, started TEXT, finished TEXT,
 status TEXT, reason TEXT, http INTEGER, partial_path TEXT, partial_bytes INTEGER);
''')
                db.execute("INSERT INTO entries(path,kind) VALUES ('disk:/legacy/child','file')")
                db.execute("INSERT INTO folders(path) VALUES ('disk:/legacy')")

            import threading
            barrier = threading.Barrier(2)
            opened = []
            errors = []

            def open_store():
                try:
                    barrier.wait(timeout=5)
                    opened.append(Store(legacy_dir))
                except Exception as exc:
                    # Keep a string only; retaining the traceback would keep a
                    # failed SQLite connection alive on Windows during cleanup.
                    errors.append(f'{type(exc).__name__}: {exc}')

            threads = [threading.Thread(target=open_store) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertTrue(all(not thread.is_alive() for thread in threads), 'migration opener hung')
            if errors:
                failures.append((attempt, errors))
            else:
                self.assertEqual(len(opened), 2)
                rows = {row['path']: row for row in opened[0].rows('all')}
                self.assertEqual(rows['disk:/legacy/child']['presence'], 'unverified')
                self.assertEqual(rows['disk:/legacy/child']['parent'], 'disk:/legacy')
        self.assertEqual(failures, [], f'concurrent Store open failures: {failures}')

    def test_reappearing_branch_revives_retry_state_and_preserves_verified_local_copy(self):
        self.initial_tree()
        destination = self.root / 'backup'
        local = destination / 'gone' / 'sub' / 'old.bin'
        local.parent.mkdir(parents=True)
        payload = b'old-child-payload'
        local.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with self.store.db() as db:
            db.execute("UPDATE entries SET status='downloaded',attempts=4,cycle_attempts=2,local_path=?,local_sha256=?,verification='cloud_hash_download' WHERE path=?",
                       (str(local), digest, 'disk:/gone/sub/old.bin'))
            db.execute("UPDATE entries SET status='retry_exhausted',cycle_attempts=3,next_attempt=9999999999,reason='old_failure' WHERE path=?",
                       ('disk:/gone/sub/failed.bin',))

        self.store.rescan(['disk:/'])
        absent = FakeAPI({('disk:/', 0): page('disk:/', [metadata('disk:/keep.txt')])})
        self.scan(absent)
        self.assertEqual(next(row for row in self.store.rows('all') if row['path'] == 'disk:/gone/sub/old.bin')['presence'], 'not_current')

        self.store.rescan(['disk:/'])
        reappearing = FakeAPI({
            ('disk:/', 0): page('disk:/', [metadata('disk:/gone', 'dir'), metadata('disk:/keep.txt')]),
            ('disk:/gone', 0): page('disk:/gone', [metadata('disk:/gone/sub', 'dir')]),
            ('disk:/gone/sub', 0): page('disk:/gone/sub', [
                metadata('disk:/gone/sub/old.bin', payload=payload),
                metadata('disk:/gone/sub/failed.bin', payload=b'failed-child'),
            ]),
        })
        run = NoWaitEngine(self.store, reappearing, interval=0, retries=1, backoff=1)
        run.run(destination, ['disk:/'], scan_only=True)
        verifier = NoWaitEngine(self.store, reappearing, interval=0, retries=1, backoff=1)
        verifier.roots = ['disk:/']
        self.store.configure(destination, reappearing.identity())
        verifier.prepare_local()
        rows = {row['path']: row for row in self.store.rows('all')}
        self.assertEqual(rows['disk:/gone/sub/old.bin']['presence'], 'observed')
        self.assertEqual(rows['disk:/gone/sub/old.bin']['status'], 'downloaded')
        self.assertEqual(rows['disk:/gone/sub/old.bin']['attempts'], 4)
        self.assertEqual(rows['disk:/gone/sub/old.bin']['cycle_attempts'], 2)
        self.assertEqual(Path(rows['disk:/gone/sub/old.bin']['local_path']).read_bytes(), payload)
        self.assertEqual(rows['disk:/gone/sub/failed.bin']['status'], 'pending')
        self.assertEqual(rows['disk:/gone/sub/failed.bin']['cycle_attempts'], 0)
        self.assertEqual(rows['disk:/gone/sub/failed.bin']['next_attempt'], 0)
        self.assertIsNone(rows['disk:/gone/sub/failed.bin']['reason'])
        self.assertEqual(reappearing.download_calls, [])

    def test_nested_root_404_does_not_mark_descendants_absent_or_touch_sibling(self):
        nested = 'disk:/parent/chosen'
        sibling = 'disk:/parent/neighbor'
        with self.store.db() as db:
            self.store.observe(db, metadata('disk:/parent', 'dir'))
            self.store.observe(db, metadata(nested, 'dir'))
            self.store.observe(db, metadata(nested + '/old.bin'))
            self.store.observe(db, metadata(sibling, 'dir'))
            self.store.observe(db, metadata(sibling + '/keep.bin'))
        self.store.rescan([nested])
        missing = FakeAPI({(nested, 0): Failure('http_error', 404)})
        self.scan(missing, roots=(nested,))
        rows = {row['path']: row for row in self.store.rows('all')}
        self.assertEqual(rows[nested + '/old.bin']['presence'], 'unverified')
        self.assertEqual(rows[sibling]['presence'], 'observed')
        self.assertEqual(rows[sibling + '/keep.bin']['presence'], 'observed')
        self.assertEqual(missing.calls, [(nested, 0)])
        folder = next(folder for folder in self.store.folders() if folder['path'] == nested)
        self.assertEqual(folder['status'], 'error')
        self.assertEqual(folder['presence'], 'observed')

    def test_status_mcp_inventory_and_csv_expose_current_unknown_and_inactive_membership(self):
        with self.store.db() as db:
            for path in ('disk:/scope/current.bin', 'disk:/scope/unknown.bin', 'disk:/scope/old.bin'):
                self.store.observe(db, metadata(path))
            db.execute("UPDATE entries SET presence='unverified' WHERE path=?", ('disk:/scope/unknown.bin',))
            db.execute("UPDATE entries SET presence='not_current' WHERE path=?", ('disk:/scope/old.bin',))
            self.store.observe(db, metadata('disk:/scope/current-dir', 'dir'))
            self.store.observe(db, metadata('disk:/scope/unknown-dir', 'dir'))
            db.execute("UPDATE entries SET presence='not_current' WHERE path=?", ('disk:/scope/current-dir',))
            db.execute("UPDATE entries SET presence='unverified' WHERE path=?", ('disk:/scope/unknown-dir',))
            db.execute("UPDATE folders SET presence='unverified',status='complete' WHERE path=?", ('disk:/scope/unknown-dir',))
            db.execute("UPDATE folders SET presence='not_current' WHERE path=?", ('disk:/scope/current-dir',))

        channel = Channel(self.store)
        session = channel.open_session()
        channel.approve(session, {
            'destination': str(self.root / 'backup'), 'roots': ['disk:/scope'],
            'interval': 0, 'retries': 1, 'backoff': 1,
        })
        channel.publish(session, 'idle', False)
        status = channel.status()
        self.assertEqual(status['membership_counts'], {'observed': 1, 'not_current': 2, 'unverified': 2})
        self.assertEqual(status['queue_files'], 1)
        self.assertEqual(status['incomplete_folders'], 1)
        self.assertEqual({row['path'] for row in channel.inventory('pending', 0, 20)['rows']}, {'disk:/scope/current.bin'})
        folders = {row['path']: row for row in channel.inventory('folders', 0, 20)['rows']}
        self.assertEqual(folders['disk:/scope/unknown-dir']['presence'], 'unverified')
        self.assertEqual(channel.inventory('all', 0, 20)['total'], 5)

        export_dir = self.store.export(self.root / 'exports', roots=['disk:/scope'])
        names = {path.name for path in export_dir.iterdir()}
        self.assertEqual(names, {
            'inventory.csv', 'downloaded.csv', 'unresolved.csv', 'inventory_errors.csv',
            'not_current.csv', 'unverified.csv', 'attempt_history.csv', 'run_status.csv',
        })
        with (export_dir / 'not_current.csv').open(encoding='utf-8-sig', newline='') as stream:
            inactive = list(csv.DictReader(stream))
        with (export_dir / 'unverified.csv').open(encoding='utf-8-sig', newline='') as stream:
            unverified = list(csv.DictReader(stream))
        self.assertEqual([row['path'] for row in inactive], ['disk:/scope/current-dir', 'disk:/scope/old.bin'])
        self.assertEqual([row['path'] for row in unverified], ['disk:/scope/unknown-dir', 'disk:/scope/unknown.bin'])
        with (export_dir / 'inventory_errors.csv').open(encoding='utf-8-sig', newline='') as stream:
            errors = list(csv.DictReader(stream))
        self.assertEqual([row['path'] for row in errors], ['disk:/scope/unknown-dir'])

    def test_dir_to_file_type_only_change_resets_file_queue_state(self):
        # Same metadata except type ensures file queue status follows the new kind.
        folder = metadata('disk:/switch', 'dir', resource_id='same-id', size=1,
                          sha256='same-hash', md5='same-md5', modified='same-time')
        self.scan(FakeAPI({
            ('disk:/', 0): page('disk:/', [folder]),
            ('disk:/switch', 0): page('disk:/switch', []),
        }))
        self.store.rescan(['disk:/'])
        file = metadata('disk:/switch', 'file', resource_id='same-id', size=1,
                        sha256='same-hash', md5='same-md5', modified='same-time')
        self.scan(FakeAPI({('disk:/', 0): page('disk:/', [file])}))
        row = next(row for row in self.store.rows('all') if row['path'] == 'disk:/switch')
        self.assertEqual(row['kind'], 'file')
        self.assertEqual(row['status'], 'pending')


if __name__ == '__main__':
    unittest.main(verbosity=2)
