"""Local session-bound command channel. Contains no credential handling."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid
from engine import cloud_path, scope_filter

COMMANDS = {'start', 'scan', 'pause', 'resume', 'stop', 'retry', 'rescan', 'export'}
STALE_SECONDS = 15


class ControlError(ValueError):
    pass


class Channel:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS agent_session(id INTEGER PRIMARY KEY CHECK(id=1),
 session TEXT, heartbeat REAL, enabled INTEGER, profile TEXT, phase TEXT, credential_present INTEGER);
CREATE TABLE IF NOT EXISTS agent_commands(request_id TEXT PRIMARY KEY, session TEXT,
 command TEXT, status TEXT, created REAL, updated REAL, result TEXT);
''')

    @contextmanager
    def gui_lease(self):
        handle = open(self.store.directory / 'gui.lock', 'a+b')
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
            raise ControlError('gui_already_open') from None
        try:
            yield
        finally:
            handle.close()

    def open_session(self):
        session = str(uuid.uuid4())
        with self.store.db() as db:
            db.execute("UPDATE agent_commands SET status=CASE WHEN status='executing' THEN 'ambiguous' ELSE 'stale_session' END,updated=? WHERE status IN ('queued','executing')", (time.time(),))
            db.execute("INSERT OR REPLACE INTO agent_session VALUES (1,?,?,0,NULL,'idle',0)", (session, time.time()))
        return session

    def publish(self, session, phase, credential_present):
        if phase not in {'idle', 'running', 'pause_requested', 'paused', 'stopping', 'closed'}:
            raise ControlError('invalid_phase')
        with self.store.db() as db:
            db.execute('UPDATE agent_session SET heartbeat=?,phase=?,credential_present=? WHERE session=?',
                       (time.time(), phase, int(credential_present), session))

    def approve(self, session, profile=None):
        if profile is not None:
            profile = validate_profile(profile)
        with self.store.db() as db:
            db.execute('UPDATE agent_session SET enabled=?,profile=? WHERE session=?',
                       (int(profile is not None), json.dumps(profile) if profile else None, session))
            # Revocation/reapproval invalidates every command not yet claimed.
            db.execute("UPDATE agent_commands SET status='cancelled',updated=? WHERE session=? AND status='queued'", (time.time(), session))

    def _session(self, db, require_enabled=True):
        row = db.execute('SELECT * FROM agent_session WHERE id=1').fetchone()
        if not row or row['phase'] == 'closed' or time.time() - row['heartbeat'] > STALE_SECONDS:
            raise ControlError('app_not_running')
        if require_enabled and not row['enabled']:
            raise ControlError('agent_control_disabled')
        return row

    def status(self):
        with self.store.db() as db:
            row = self._session(db, False)
            if not row['enabled']:
                return {'online': True, 'enabled': False, 'phase': row['phase'], 'credential_present': bool(row['credential_present'])}
            profile = json.loads(row['profile'])
        scope, args = scope_filter(profile['roots'])
        with self.store.db() as db:
            count, size, done, done_size = db.execute("SELECT count(*),coalesce(sum(size),0),coalesce(sum(status='downloaded'),0),coalesce(sum(CASE WHEN status='downloaded' THEN size ELSE 0 END),0) FROM entries WHERE kind='file' AND " + scope, args).fetchone()
            incomplete = db.execute("SELECT count(*) FROM folders WHERE status!='complete' AND " + scope, args).fetchone()[0]
        last_run = {'status': 'not_run_for_approved_profile'}
        if (self.store.setting('roots') or '').splitlines() == profile['roots']:
            status = self.store.setting('last_run_status')
            allowed = {'in_progress', 'finished', 'finished_with_unresolved_items', 'interrupted', 'error'}
            error = self.store.setting('last_run_error') or ''
            category = error.split(' (HTTP', 1)[0]
            last_run = {'status': status if status in allowed else 'not_started',
                        'error_category': category if category in {'auth','rate_limit','transport','service_unavailable','http_error','invalid_api_response','missing_account_identity','untrusted_download_host','invalid_api_host','local_unexpected_error'} else ('local_error_check_gui' if error else None)}
        return {'online': True, 'enabled': True, 'phase': row['phase'],
                'credential_present': bool(row['credential_present']), 'approved_profile': profile,
                'queue_files': count, 'downloaded_files': done, 'queue_bytes': size,
                'downloaded_bytes': done_size, 'incomplete_folders': incomplete,
                'last_run': last_run,
                'coverage': 'known_inventory_only_not_atomic_cloud_snapshot'}

    def submit(self, command, request_id):
        if command not in COMMANDS:
            raise ControlError('unknown_command')
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ControlError('request_id_must_be_canonical_uuid') from None
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db)
            old = db.execute('SELECT * FROM agent_commands WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['session'] != session['session'] or old['command'] != command:
                    raise ControlError('request_id_conflict')
                return self._command(old)
            pending = db.execute("SELECT count(*) FROM agent_commands WHERE session=? AND status IN ('queued','executing')", (session['session'],)).fetchone()[0]
            if pending >= 32:
                raise ControlError('too_many_pending_commands')
            db.execute("INSERT INTO agent_commands VALUES (?,?,?,'queued',?,?,NULL)", (request_id, session['session'], command, time.time(), time.time()))
            return {'request_id': request_id, 'command': command, 'status': 'queued'}

    @staticmethod
    def _command(row):
        return {'request_id': row['request_id'], 'command': row['command'], 'status': row['status'],
                'result': json.loads(row['result']) if row['result'] else None}

    def command(self, request_id):
        with self.store.db() as db:
            session = self._session(db)
            row = db.execute('SELECT * FROM agent_commands WHERE request_id=?', (request_id,)).fetchone()
            if not row:
                raise ControlError('unknown_request')
            result = self._command(row)
            if row['session'] != session['session']:
                result['result'] = None
            return result

    def dispatch_one(self, session, handler):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._session(db)
            if state['session'] != session:
                raise ControlError('stale_session')
            row = db.execute("SELECT * FROM agent_commands WHERE session=? AND status='queued' ORDER BY created,request_id LIMIT 1", (session,)).fetchone()
            if not row:
                return
            db.execute("UPDATE agent_commands SET status='executing',updated=? WHERE request_id=?", (time.time(), row['request_id']))
            profile = json.loads(state['profile'])
        try:
            result = handler(row['command'], profile)
            status = 'completed'
        except Exception:
            # Never forward raw exceptions, settings or credential values to agents.
            result, status = {'error': 'command_failed_check_gui'}, 'failed'
        with self.store.db() as db:
            db.execute('UPDATE agent_commands SET status=?,result=?,updated=? WHERE request_id=?',
                       (status, json.dumps(result), time.time(), row['request_id']))

    def inventory(self, view, offset, limit):
        if view not in {'all', 'pending', 'downloaded', 'failed', 'folders'} or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ControlError('invalid_page')
        filters = {'all': '1', 'pending': "kind='file' AND status!='downloaded'", 'downloaded': "status='downloaded'",
                   'failed': "kind='file' AND status NOT IN ('pending','retry','downloaded')"}
        with self.store.db() as db:
            session = self._session(db)
            scope, params = scope_filter(json.loads(session['profile'])['roots'])
            if view == 'folders':
                table, fields, where = 'folders', 'path,offset,total,status,error,http,attempts,updated', '1'
            else:
                table, fields, where = 'entries', 'path,resource_id,kind,size,status,reason,http,attempts,last_attempt,verification,local_path', filters[view]
            where = '(' + where + ') AND ' + scope
            total = db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + where, params).fetchone()[0]
            rows = db.execute('SELECT ' + fields + ' FROM ' + table + ' WHERE ' + where + ' ORDER BY path LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
            return {'rows': [dict(r) for r in rows], 'total': total, 'offset': offset,
                    'next_offset': offset + len(rows) if offset + len(rows) < total else None}


def validate_profile(value):
    if not isinstance(value, dict) or set(value) != {'destination', 'roots', 'interval', 'retries', 'backoff'}:
        raise ControlError('invalid_profile')
    destination = value['destination']
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise ControlError('invalid_destination')
    roots = value['roots']
    if not isinstance(roots, list) or not 1 <= len(roots) <= 100 or any(not isinstance(r, str) for r in roots):
        raise ControlError('invalid_roots')
    try:
        roots = [cloud_path(r) for r in roots]
    except ValueError:
        raise ControlError('invalid_roots') from None
    for key, low, high in (('interval', 0, 3600), ('retries', 1, 20), ('backoff', 1, 3600)):
        v = value[key]
        if type(v) not in (int, float) or not low <= v <= high or key == 'retries' and type(v) is not int:
            raise ControlError('invalid_options')
    return {**value, 'destination': str(Path(destination).absolute()), 'roots': roots}
