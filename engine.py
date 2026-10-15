"""Durable single-writer recovery queue; all cloud calls are reads."""
import csv
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from api import Failure


def now():
    return datetime.now(timezone.utc).isoformat()


def scope_filter(roots):
    if roots is None:
        return '1', []
    parts, args = [], []
    for root in roots:
        prefix = cloud_path(root).rstrip('/') + '/'
        parts.append('(path=? OR substr(path,1,?)=?)')
        args.extend([cloud_path(root), len(prefix), prefix])
    return '(' + ' OR '.join(parts) + ')' if parts else '0', args


def cloud_path(value):
    value = value.strip()
    if value == '/disk' or value.startswith('/disk/'):
        value = 'disk:/' + value[6:].lstrip('/')
    if value.startswith('/'):
        value = 'disk:' + value
    if not value.startswith('disk:/'):
        raise ValueError('Путь должен начинаться с disk:/ или /disk/.')
    parts = value[6:].split('/') if value[6:] else []
    if any(not p or p in ('.', '..') or '\\' in p or '\x00' in p for p in parts):
        raise ValueError('Недопустимый облачный путь.')
    return 'disk:/' + '/'.join(parts)


def component(name):
    # Reversible escaping; percent itself is escaped. Reserved names/long names get a suffix.
    safe = ''.join(f'%{ord(c):04X}' if ord(c) < 32 or c in '<>:"/\\|?*%' else c for c in name)
    while safe.endswith(('.', ' ')):
        safe = safe[:-1] + f'%{ord(safe[-1]):04X}'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', safe, re.I):
        safe = '%' + safe
    if not safe or safe in ('.', '..'):
        safe = '%empty'
    if len(safe.encode('utf-16-le')) > 220:
        safe = safe[:70] + '~' + hashlib.sha256(name.encode()).hexdigest()[:16]
    return safe


def safe_local(root, relative):
    root = Path(root).absolute()
    dest = root / relative
    if not dest.is_relative_to(root) or any(p in ('.', '..') for p in Path(relative).parts) or Path(relative).is_absolute():
        raise Failure('unsafe_local_path')
    cursor = dest
    while True:
        if cursor.exists() or cursor.is_symlink():
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise Failure('local_reparse_point')
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return dest


def hashes(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    length = 0
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            sha.update(chunk); md5.update(chunk); length += len(chunk)
    return length, sha.hexdigest(), md5.hexdigest()


def matches(row, result):
    size, sha, md5 = result
    if row['size'] is None or size != row['size']:
        return False
    if row['sha256']:
        return sha.lower() == row['sha256'].lower()
    if row['md5']:
        return md5.lower() == row['md5'].lower()
    return False  # Size alone is never enough to adopt an existing file.


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = self.directory / 'queue.sqlite3'
        with self.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(path TEXT PRIMARY KEY, resource_id TEXT, kind TEXT NOT NULL,
 size INTEGER, sha256 TEXT, md5 TEXT, modified TEXT, relative TEXT UNIQUE COLLATE NOCASE,
 status TEXT NOT NULL, reason TEXT, http INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
 cycle_attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT, next_attempt REAL NOT NULL DEFAULT 0,
 local_path TEXT, local_sha256 TEXT, verification TEXT, partial_path TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
 total INTEGER, status TEXT NOT NULL DEFAULT 'pending', error TEXT, http INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0, updated TEXT);
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, recorded TEXT, path TEXT, metadata TEXT);
CREATE TABLE IF NOT EXISTS folder_items(folder TEXT,path TEXT,PRIMARY KEY(folder,path));
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, recorded TEXT, path TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, path TEXT, started TEXT, finished TEXT,
 status TEXT, reason TEXT, http INTEGER, partial_path TEXT, partial_bytes INTEGER);
''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.file, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def configure(self, destination, uid):
        destination = str(Path(destination).absolute())
        identity = hashlib.sha256(uid.encode()).hexdigest()
        with self.db() as db:
            for key, value in (('destination', destination), ('account', identity)):
                old = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                if old and old[0] != value:
                    raise ValueError('Очередь принадлежит другому аккаунту или каталогу. Выберите новую папку состояния.')
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, value))

    def observe(self, db, metadata):
        path = cloud_path(metadata['path'])
        kind = metadata.get('type')
        if kind not in ('file', 'dir'):
            raise Failure('invalid_inventory_type')
        fields = {k: metadata.get(k) for k in ('path', 'name', 'type', 'resource_id', 'size', 'sha256', 'md5', 'modified')}
        db.execute('INSERT INTO observations(recorded,path,metadata) VALUES (?,?,?)', (now(), path, json.dumps(fields, ensure_ascii=False)))
        old = db.execute('SELECT * FROM entries WHERE path=?', (path,)).fetchone()
        parent = path.rsplit('/', 1)[0]
        parent_row = db.execute('SELECT relative FROM entries WHERE path=?', (parent,)).fetchone()
        # A selected nested root still preserves its full path from disk:/.
        if old:
            relative = old['relative']
        else:
            if parent_row:
                relative = str(Path(parent_row[0]) / component(path.rsplit('/', 1)[-1]))
            else:
                relative = str(Path(*[component(p) for p in path[6:].split('/') if p])) if path != 'disk:/' else ''
            collision = db.execute('SELECT path FROM entries WHERE relative=? COLLATE NOCASE', (relative,)).fetchone()
            if collision and collision[0] != path or relative.split(os.sep)[0].casefold() == '.yandexbackupparts':
                relative += '~' + hashlib.sha256(path.encode()).hexdigest()[:16]
        changed = old and any(old[k] != metadata.get(k) for k in ('size', 'sha256', 'md5', 'modified', 'resource_id'))
        status = ('directory' if kind == 'dir' else 'pending') if not old or changed else old['status']
        if changed:
            db.execute('INSERT INTO history(recorded,path,event,detail) VALUES (?,?,?,?)', (now(), path, 'cloud_metadata_changed', json.dumps(dict(old), ensure_ascii=False)))
        db.execute('''INSERT INTO entries(path,resource_id,kind,size,sha256,md5,modified,relative,status,first_seen,last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET resource_id=excluded.resource_id,
 kind=excluded.kind,size=excluded.size,sha256=excluded.sha256,md5=excluded.md5,modified=excluded.modified,
 status=excluded.status,last_seen=excluded.last_seen''',
                   (path, fields['resource_id'], kind, fields['size'], fields['sha256'], fields['md5'], fields['modified'], relative, status, now(), now()))
        if changed:
            # A new cloud version gets a fresh retry cycle, not a new history.
            # Existing payloads/partials remain on disk; their prior paths are in history.
            db.execute('''UPDATE entries SET cycle_attempts=0,next_attempt=0,reason=NULL,http=NULL,
 local_path=NULL,local_sha256=NULL,verification=NULL,partial_path=NULL WHERE path=?''', (path,))
        if kind == 'dir':
            db.execute('INSERT OR IGNORE INTO folders(path,updated) VALUES (?,?)', (path, now()))
        return path

    def seed(self, roots):
        for value in roots:
            path = cloud_path(value)
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO folders(path,updated) VALUES (?,?)', (path, now()))

    def rows(self, view='all', limit=None):
        where = {'all': '1', 'pending': "kind='file' AND status!='downloaded'", 'downloaded': "status='downloaded'", 'failed': "kind='file' AND status NOT IN ('downloaded','pending','retry')"}[view]
        with self.db() as db:
            sql = 'SELECT * FROM entries WHERE ' + where + ' ORDER BY path'
            return [dict(r) for r in db.execute(sql + (' LIMIT ?' if limit is not None else ''), (limit,) if limit is not None else ())]

    def summary(self):
        with self.db() as db:
            row = db.execute("SELECT count(*),coalesce(sum(size),0),coalesce(sum(status='downloaded'),0),coalesce(sum(CASE WHEN status='downloaded' THEN size ELSE 0 END),0) FROM entries WHERE kind='file'").fetchone()
            return tuple(row)

    def folders(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM folders ORDER BY path')]

    def retry(self, roots=None):
        scope, args = scope_filter(roots)
        with self.db() as db:
            db.execute("UPDATE entries SET status='pending',cycle_attempts=0,next_attempt=0 WHERE kind='file' AND status NOT IN ('downloaded','excluded_security') AND " + scope, args)
            db.execute("UPDATE folders SET status='pending',attempts=0,error=NULL WHERE status='error' AND " + scope, args)
            db.execute("INSERT INTO history(recorded,event) VALUES (?, 'explicit_retry_cycle')", (now(),))

    def rescan(self, roots=None):
        scope, args = scope_filter(roots)
        with self.db() as db:
            db.execute("UPDATE folders SET offset=0,total=NULL,status='pending',attempts=0,error=NULL WHERE " + scope, args)
            db.execute('DELETE FROM folder_items WHERE folder IN (SELECT path FROM folders WHERE ' + scope + ')', args)
            db.execute("INSERT INTO history(recorded,event) VALUES (?, 'explicit_rescan_no_deletions')", (now(),))

    def export(self, directory, roots=None):
        scope, args = scope_filter(roots)
        # A fresh subdirectory preserves pre-existing recovery evidence/exports.
        directory = Path(directory) / ('YandexRescue-export-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=False)
        with self.db() as db:
            tables = {
                'inventory.csv': ('SELECT * FROM entries WHERE ' + scope + ' ORDER BY path', args),
                'downloaded.csv': ("SELECT * FROM entries WHERE status='downloaded' AND " + scope + ' ORDER BY path', args),
                # All unresolved, including never attempted: no silent disappearance.
                'unresolved.csv': ("SELECT * FROM entries WHERE kind='file' AND status!='downloaded' AND " + scope + ' ORDER BY path', args),
                'inventory_errors.csv': ("SELECT * FROM folders WHERE status!='complete' AND " + scope + ' ORDER BY path', args),
                'attempt_history.csv': ('SELECT * FROM attempts WHERE ' + scope + ' ORDER BY id', args),
                'run_status.csv': ("SELECT key,value FROM settings WHERE key IN ('roots','last_run_status','last_run_error')", ()),
            }
            for name, (sql, parameters) in tables.items():
                cursor = db.execute(sql, parameters)
                temp = directory / (name + '.tmp')
                with temp.open('w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow([d[0] for d in cursor.description])
                    for row in cursor:
                        if name == 'run_status.csv' and roots is not None and row[0] == 'roots':
                            row = ('roots', '\n'.join(roots))
                        # Prevent spreadsheet formula execution from untrusted names.
                        writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
                os.replace(temp, directory / name)
        return directory


class Interrupted(Exception):
    pass


class Engine:
    def __init__(self, store, api, interval=3, retries=3, backoff=5, notify=None):
        self.store, self.api = store, api
        self.interval = max(0, min(float(interval), 3600))
        self.retries = max(1, min(int(retries), 20))
        self.backoff = max(1, min(float(backoff), 3600))
        self.notify = notify or (lambda _: None)
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.paused = threading.Event()

    def check(self):
        if self.stop.is_set():
            raise Interrupted()
        while self.pause.is_set():
            self.paused.set()
            if self.stop.wait(.2):
                raise Interrupted()
        self.paused.clear()

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.stop.wait(min(.2, max(0, until - time.monotonic())))

    @contextmanager
    def lease(self):
        # Process lifetime OS lock; a crash releases the lease automatically.
        handle = open(self.store.directory / 'writer.lock', 'a+b')
        if handle.seek(0, 2) == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('Эта очередь уже открыта другим загрузчиком.') from None
        try:
            yield
        finally:
            handle.close()

    def run(self, destination, roots, scan_only=False):
        # Do not mutate another worker's queue/status if the lease is unavailable.
        try:
            with self.lease():
                self._run_owned(destination, roots, scan_only)
        except ValueError:
            self.notify('Очередь занята другим исполнителем; запуск не выполнен.')

    def _run_owned(self, destination, roots, scan_only=False):
        try:
            self.roots = [cloud_path(r) for r in roots]
            self.store.setting('roots', '\n'.join(self.roots))
            self.store.setting('last_run_status', 'in_progress')
            self.store.setting('last_run_error', '')
            self.store.configure(destination, self.api.identity())
            self.store.seed(roots)
            scope, scope_args = scope_filter(self.roots)
            with self.store.db() as db:
                # Abandoned work remains unresolved; partials are retained.
                db.execute("UPDATE entries SET status='pending',reason='interrupted_restart' WHERE status='downloading' AND " + scope, scope_args)
                db.execute("UPDATE attempts SET finished=?,status='interrupted',reason='process_restart' WHERE finished IS NULL AND " + scope, (now(), *scope_args))
            self.scan()
            if not scan_only:
                self.prepare_local()
                self.download_all()
            self.store.setting('last_run_status', 'finished_with_unresolved_items' if self.store.rows('pending') or any(f['status'] != 'complete' for f in self.store.folders()) else 'finished')
            self.notify('Операция завершена. Проверьте незавершённые папки и файлы.')
        except Interrupted:
            self.store.setting('last_run_status', 'interrupted')
            self.notify('Остановлено. Очередь сохранена; частичные файлы не считаются готовыми.')
        except (Failure, ValueError) as exc:
            self.store.setting('last_run_status', 'error')
            self.store.setting('last_run_error', str(exc))
            self.notify(str(exc))
        except Exception:
            self.store.setting('last_run_status', 'error')
            self.store.setting('last_run_error', 'local_unexpected_error')
            self.notify('Локальная ошибка. Очередь сохранена; проверьте доступ к диску и пути.')

    def selected(self, path):
        return any(path == r or path.startswith(r.rstrip('/') + '/') for r in self.roots)

    def scan(self):
        while True:
            self.check()
            with self.store.db() as db:
                folder = next((r for r in db.execute("SELECT * FROM folders WHERE status='pending' ORDER BY path") if self.selected(r['path'])), None)
            if not folder:
                return
            path, offset = folder['path'], folder['offset']
            self.notify('Обход: ' + path + f' (смещение {offset})')
            try:
                result = self.api.metadata(path, offset)
                if cloud_path(result['path']) != path:
                    raise Failure('inventory_path_mismatch')
                if result.get('type') == 'file':
                    with self.store.db() as db:
                        self.store.observe(db, result)
                        db.execute("UPDATE folders SET status='complete',updated=? WHERE path=?", (now(), path))
                    continue
                embedded = result.get('_embedded')
                if not isinstance(embedded, dict):
                    raise Failure('missing_inventory_page')
                items, total = embedded.get('items'), embedded.get('total')
                if not isinstance(items, list) or type(total) is not int or total < 0 or embedded.get('offset', offset) != offset:
                    raise Failure('invalid_inventory_page')
                if folder['total'] is not None and total != folder['total']:
                    raise Failure('inventory_changed_rescan_required')
                if not items and offset < total or offset + len(items) > total:
                    raise Failure('incomplete_inventory_page')
                paths = [cloud_path(i['path']) for i in items]
                if len(set(paths)) != len(paths) or any(p.rsplit('/', 1)[0] != path.rstrip('/') for p in paths):
                    # Root's direct children have parent 'disk:'.
                    if not (path == 'disk:/' and len(set(paths)) == len(paths) and all(p.rsplit('/', 1)[0] == 'disk:' for p in paths)):
                        raise Failure('invalid_inventory_children')
                with self.store.db() as db:
                    if offset == 0:
                        db.execute('DELETE FROM folder_items WHERE folder=?', (path,))
                    for child in paths:
                        if db.execute('SELECT 1 FROM folder_items WHERE folder=? AND path=?', (path, child)).fetchone():
                            raise Failure('duplicate_inventory_page_rescan_required')
                        db.execute('INSERT INTO folder_items VALUES (?,?)', (path, child))
                    self.store.observe(db, result)
                    for item in items:
                        self.store.observe(db, item)
                    end = offset + len(items)
                    db.execute('UPDATE folders SET offset=?,total=?,status=?,attempts=0,updated=? WHERE path=?',
                               (end, total, 'complete' if end == total else 'pending', now(), path))
                self.wait(self.interval)
            except (Failure, ValueError, KeyError, TypeError) as exc:
                error = exc if isinstance(exc, Failure) else Failure('invalid_inventory_response')
                attempt = folder['attempts'] + 1
                with self.store.db() as db:
                    db.execute('UPDATE folders SET status=?,error=?,http=?,attempts=?,updated=? WHERE path=?',
                               ('error' if attempt >= self.retries or error.category == 'auth' else 'pending', error.category, error.code, attempt, now(), path))
                    db.execute('INSERT INTO history(recorded,path,event,detail) VALUES (?,?,?,?)', (now(), path, 'inventory_error', str(error)))
                if error.category == 'auth':
                    raise error
                self.wait(min(3600, max(error.retry_after, self.backoff * 2 ** (attempt - 1))))

    def prepare_local(self):
        root = Path(self.store.setting('destination'))
        self.local_index = {}
        if root.exists():
            safe_local(root, '')
            for parent, dirs, names in os.walk(root, followlinks=False):
                self.check()
                dirs[:] = [d for d in dirs if d != '.YandexBackupParts' and not (Path(parent) / d).is_symlink()
                           and not getattr((Path(parent) / d).lstat(), 'st_file_attributes', 0) & 0x400]
                for name in names:
                    candidate = Path(parent) / name
                    if candidate.suffix.lower() in ('.part', '.crdownload', '.tmp') or candidate.is_symlink():
                        continue
                    info = candidate.lstat()
                    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        continue
                    self.local_index.setdefault((name.casefold(), info.st_size), []).append(candidate)
        for row in self.store.rows():
            self.check()
            if not self.selected(row['path']):
                continue
            if row['kind'] == 'dir':
                try:
                    safe_local(root, row['relative']).mkdir(parents=True, exist_ok=True)
                except (Failure, OSError):
                    with self.store.db() as db:
                        db.execute("UPDATE folders SET status='error',error='local_directory_conflict',updated=? WHERE path=?", (now(), row['path']))
            elif row['status'] == 'downloaded':
                try:
                    existing = Path(row['local_path'])
                    safe_local(root, existing.relative_to(root))
                    valid = existing.is_file() and matches(row, hashes(existing))
                except (Failure, OSError, ValueError, TypeError):
                    valid = False
                if not valid:
                    with self.store.db() as db:
                        db.execute("UPDATE entries SET status='pending',reason='local_copy_changed_or_missing',cycle_attempts=0 WHERE path=?", (row['path'],))
                        db.execute("INSERT INTO history(recorded,path,event) VALUES (?,?,'local_revalidation_failed')", (now(), row['path']))

    def download_all(self):
        while True:
            self.check()
            with self.store.db() as db:
                row = next((r for r in db.execute("SELECT * FROM entries WHERE kind='file' AND status IN ('pending','retry') ORDER BY next_attempt,path") if self.selected(r['path'])), None)
            if not row:
                return
            self.wait(max(0, row['next_attempt'] - time.time()))
            self.download_one(dict(row))
            self.wait(self.interval)

    def download_one(self, row):
        root = Path(self.store.setting('destination'))
        path = row['path']
        self.notify('Файл: ' + path)
        # Never recover quarantined shortcut/active payloads automatically.
        excluded = {'.lnk', '.url', '.exe', '.com', '.scr', '.bat', '.cmd', '.ps1', '.vbs', '.js', '.msi', '.hta', '.reg'}
        if Path(path).suffix.casefold() in excluded:
            with self.store.db() as db:
                db.execute("UPDATE entries SET status='excluded_security',reason='active_payload_not_downloaded' WHERE path=?", (path,))
            return
        partial = None
        aid = None
        try:
            final = safe_local(root, row['relative'])
            if final.exists():
                result = hashes(final) if final.is_file() else None
                if result and matches(row, result):
                    self.complete(row, final, result[1], 'cloud_hash_existing')
                    return
                raise Failure('local_conflict')
            # Reconcile earlier browser backup layout; never trust filename/size alone.
            alias = {'Фотокамера': 'Camera', 'Загрузки': 'Downloads'}
            pieces = path[6:].split('/')
            candidates = []
            if pieces[0] in alias:
                candidates.append(str(Path(alias[pieces[0]], *pieces[1:])))
            candidates += [str(p.relative_to(root)) for p in getattr(self, 'local_index', {}).get((pieces[-1].casefold(), row['size']), [])]
            for relative in candidates:
                candidate = safe_local(root, relative)
                if candidate.is_file():
                    result = hashes(candidate)
                    if matches(row, result):
                        self.complete(row, candidate, result[1], 'cloud_hash_existing_alias')
                        return
            if not row['sha256'] and not row['md5']:
                raise Failure('missing_cloud_hash')
            final.parent.mkdir(parents=True, exist_ok=True)
            parts = safe_local(root, '.YandexBackupParts')
            parts.mkdir(exist_ok=True)
            partial = parts / (uuid.uuid4().hex + '.part')
            with self.store.db() as db:
                cursor = db.execute("INSERT INTO attempts(path,started,status,partial_path) VALUES (?,?,'active',?)", (path, now(), str(partial)))
                aid = cursor.lastrowid
                db.execute("UPDATE entries SET status='downloading',attempts=attempts+1,cycle_attempts=cycle_attempts+1,last_attempt=?,partial_path=? WHERE path=?", (now(), str(partial), path))
            with partial.open('xb') as output, self.api.download(path) as response:
                length = 0
                while True:
                    self.check()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk); length += len(chunk)
                    if row['size'] is not None and length > row['size']:
                        raise Failure('integrity_size')
                    self.notify(f'Скачивание: {path} — {length:,} / {row["size"] or 0:,} байт')
                output.flush(); os.fsync(output.fileno())
            digest = hashes(partial)
            if not matches(row, digest):
                raise Failure('integrity_hash_or_size')
            self.check()
            safe_local(root, row['relative'])
            # Hard-link publish is atomic and refuses an existing final pathname.
            try:
                os.link(partial, final)
            except FileExistsError:
                raise Failure('local_conflict') from None
            except OSError:
                raise Failure('atomic_publication_failed') from None
            partial.unlink()
            self.complete(row, final, digest[1], 'cloud_hash_download', aid)
        except Interrupted:
            self.failed(row, Failure('interrupted'), partial, aid, interrupted=True)
            raise
        except Failure as exc:
            self.failed(row, exc, partial, aid)
            if exc.category == 'auth':
                raise
        except (OSError, ValueError, http.client.HTTPException):
            self.failed(row, Failure('local_io_or_transport'), partial, aid)

    def complete(self, row, final, sha, verification, aid=None):
        with self.store.db() as db:
            db.execute("UPDATE entries SET status='downloaded',reason=NULL,http=NULL,local_path=?,local_sha256=?,verification=?,partial_path=NULL WHERE path=?", (str(final), sha, verification, row['path']))
            db.execute('INSERT INTO history(recorded,path,event,detail) VALUES (?,?,?,?)', (now(), row['path'], 'verified_downloaded', verification))
            if aid:
                db.execute("UPDATE attempts SET finished=?,status='downloaded',partial_bytes=0 WHERE id=?", (now(), aid))

    def failed(self, row, failure, partial, aid, interrupted=False):
        attempt = row['cycle_attempts'] + 1
        terminal = failure.category in ('local_conflict', 'auth', 'missing_cloud_hash', 'local_reparse_point', 'unsafe_local_path')
        status = 'pending' if interrupted else ('unresolved' if terminal else 'retry_exhausted' if attempt >= self.retries else 'retry')
        partial_bytes = partial.stat().st_size if partial and partial.exists() else 0
        with self.store.db() as db:
            db.execute('UPDATE entries SET status=?,reason=?,http=?,next_attempt=? WHERE path=?',
                       (status, failure.category, failure.code, time.time() + min(3600, max(failure.retry_after, self.backoff * 2 ** min(attempt - 1, 12))), row['path']))
            # Preflight conflicts are also evidence, even though no network request occurred.
            if not aid:
                aid = db.execute('INSERT INTO attempts(path,started,partial_path) VALUES (?,?,?)', (row['path'], now(), str(partial) if partial else None)).lastrowid
                db.execute('UPDATE entries SET attempts=attempts+1,cycle_attempts=cycle_attempts+1,last_attempt=? WHERE path=?', (now(), row['path']))
            db.execute('UPDATE attempts SET finished=?,status=?,reason=?,http=?,partial_bytes=? WHERE id=?', (now(), status, failure.category, failure.code, partial_bytes, aid))
            db.execute('INSERT INTO history(recorded,path,event,detail) VALUES (?,?,?,?)', (now(), row['path'], status, str(failure)))
