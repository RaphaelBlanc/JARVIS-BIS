"""Transactional namespace over immutable blobs on a local persistent volume."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
import uuid

logger = logging.getLogger(__name__)


class CloudError(Exception):
    def __init__(self, code, status=400, **context):
        super().__init__(code)
        self.code, self.status, self.context = code, status, context


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    quota: int = 20 * 1024**3
    max_file: int = 5 * 1024**3
    chunk_size: int = 4 * 1024**2
    ttl: int = 86400
    max_uploads: int = 8
    max_versions: int = 10
    confirmed_history_limit: int | None = None

    @classmethod
    def environment(cls):
        return cls(Path(os.environ.get('CLOUD_DATA_DIR', '/data')),
                   int(os.environ.get('CLOUD_QUOTA_BYTES', 20 * 1024**3)),
                   int(os.environ.get('CLOUD_MAX_FILE_BYTES', 5 * 1024**3)),
                   int(os.environ.get('CLOUD_CHUNK_BYTES', 4 * 1024**2)),
                   int(os.environ.get('CLOUD_UPLOAD_TTL', 86400)),
                   int(os.environ.get('CLOUD_MAX_UPLOADS', 8)),
                   int(os.environ.get('CLOUD_MAX_VERSIONS', 10)),
                   int(os.environ['CLOUD_CONFIRMED_HISTORY_LIMIT']) if 'CLOUD_CONFIRMED_HISTORY_LIMIT' in os.environ else None)


def new_id():
    return uuid.uuid4().hex


def name_checked(name):
    if not isinstance(name, str):
        raise CloudError('invalid_name')
    name = unicodedata.normalize('NFC', name)
    if (not name or name in ('.', '..')
            or any(c in '/\\' or unicodedata.category(c).startswith('C') for c in name)
            or len(name.encode()) > 240):
        raise CloudError('invalid_name')
    return name


def digest_checked(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise CloudError('invalid_checksum')
    return value


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, settings):
        self.settings = settings
        if min(settings.quota, settings.max_file, settings.chunk_size, settings.ttl, settings.max_uploads) < 1:
            raise ValueError('Cloud limits must be positive')
        if settings.chunk_size > 16 * 1024**2:
            raise ValueError('CLOUD_CHUNK_BYTES must be at most 16 MiB')
        if not 0 <= settings.max_versions <= 10:
            raise ValueError('CLOUD_MAX_VERSIONS must be between 0 and 10')
        self.root = settings.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs, self.parts = self.root / 'objects', self.root / 'uploads'
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        self.parts.mkdir(mode=0o700, exist_ok=True)
        self.locks = self.root / 'locks'
        self.locks.mkdir(mode=0o700, exist_ok=True)
        self.database = self.root / 'cloud.sqlite3'
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise RuntimeError('Unsupported cloud database version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,parent TEXT,name TEXT NOT NULL,
                    kind TEXT NOT NULL,revision TEXT NOT NULL,blob TEXT,size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT,modified REAL NOT NULL,UNIQUE(parent,name));
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,file_id TEXT NOT NULL,
                    blob TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,device TEXT NOT NULL,
                    idem TEXT NOT NULL,spec TEXT NOT NULL,state TEXT NOT NULL,expires REAL NOT NULL,
                    result TEXT,cleanup_pending INTEGER NOT NULL DEFAULT 1,UNIQUE(device,idem));
                CREATE TABLE IF NOT EXISTS chunks(upload TEXT NOT NULL,idx INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,size INTEGER NOT NULL,PRIMARY KEY(upload,idx));
            ''')
        os.chmod(self.database, 0o600)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS garbage(blob TEXT PRIMARY KEY,size INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY,parent TEXT NOT NULL,name TEXT NOT NULL,revision TEXT NOT NULL,deleted REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS deletions(device TEXT NOT NULL,idem TEXT NOT NULL,file_id TEXT NOT NULL,revision TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(device,idem))')
            if 'cleanup_pending' not in {row[1] for row in db.execute('PRAGMA table_info(uploads)')}:
                db.execute('ALTER TABLE uploads ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 1')
            if version < 3:
                # Historical committed receipts usually have no parts left.
                # Do not charge their complete source size merely for migrating.
                for row in db.execute("SELECT id FROM uploads WHERE state!='pending'").fetchall():
                    if not any(self.parts.glob(row['id'] + '-*')):
                        db.execute('UPDATE uploads SET cleanup_pending=0 WHERE id=?', (row['id'],))
                        db.execute('DELETE FROM chunks WHERE upload=?', (row['id'],))
            db.execute('PRAGMA user_version=3')
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('server_id', new_id()))
            db.execute('INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?,?)',
                       ('root', None, '', 'directory', new_id(), None, 0, None, time.time()))
            self.server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
            excess = db.execute('SELECT 1 FROM versions GROUP BY file_id HAVING COUNT(*)>? LIMIT 1',
                                (settings.max_versions,)).fetchone()
            if excess and settings.confirmed_history_limit != settings.max_versions:
                raise RuntimeError('Existing history exceeds limit: back up the volume, then run cloud configure --confirm-history-prune')
            self._trim_versions(db)
        self._gc_cursor = ''
        self.collect_garbage()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def envelope(self, **data):
        return dict(api_version=1, server_id=self.server_id, space_id='personal', **data)

    @contextmanager
    def _upload_lock(self, ident, blocking=True):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            raise CloudError('not_found', 404)
        # A bounded set of locks prevents one persistent lock file per receipt.
        path = self.locks / str(int(ident[:2], 16) % 64)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _cleanup_terminal(self, ident):
        """Caller owns the upload lock; pending/assembling transfers keep their parts."""
        try:
            with self.transaction() as db:
                row = db.execute('SELECT state,expires,cleanup_pending FROM uploads WHERE id=?', (ident,)).fetchone()
                if row is None or not row['cleanup_pending']:
                    return
                if row['state'] == 'pending':
                    if row['expires'] >= time.time():
                        return
                    db.execute("UPDATE uploads SET state='expired' WHERE id=?", (ident,))
                # Includes a part written before a crash/rollback of its DB registration.
                for path in self.parts.glob(ident + '-*'):
                    path.unlink(missing_ok=True)
                sync_dir(self.parts)
                db.execute('DELETE FROM chunks WHERE upload=?', (ident,))
                db.execute('UPDATE uploads SET cleanup_pending=0 WHERE id=?', (ident,))
        except (OSError, sqlite3.Error):
            # Retain the cleanup marker and its reservation until disk cleanup succeeds.
            logger.warning('Cloud part cleanup deferred for upload %s', ident)

    def collect_expired(self, ident=None):
        """Online reaper. Never hold a DB transaction while acquiring an upload lock."""
        try:
            with self.connection() as db:
                if ident is None:
                    ids = [row[0] for row in db.execute(
                        "SELECT id FROM uploads WHERE cleanup_pending=1 AND (state!='pending' OR expires<?) LIMIT 256",
                        (time.time(),))]
                else:
                    ids = [ident]
            for upload in ids:
                with self._upload_lock(upload, blocking=False) as acquired:
                    if acquired:
                        self._cleanup_terminal(upload)
        except (OSError, sqlite3.Error):
            logger.warning('Cloud part cleanup will be retried')

    def features(self):
        return {'file_delete': True, 'delete_if_match': True, 'delete_idempotency': True,
                'entry_by_id': True, 'early_create_conflict': True,
                'max_old_versions': self.settings.max_versions}

    @staticmethod
    def _touch(db, parent):
        db.execute('UPDATE entries SET revision=?,modified=? WHERE id=?', (new_id(), time.time(), parent))

    def _trim_versions(self, db, file_id=None):
        files = [file_id] if file_id else [r[0] for r in db.execute('SELECT DISTINCT file_id FROM versions')]
        for ident in files:
            old = db.execute('SELECT id,blob,size FROM versions WHERE file_id=? ORDER BY created DESC,rowid DESC LIMIT -1 OFFSET ?',
                             (ident, self.settings.max_versions)).fetchall()
            for row in old:
                db.execute('INSERT OR IGNORE INTO garbage VALUES (?,?)', (row['blob'], row['size']))
                db.execute('DELETE FROM versions WHERE id=?', (row['id'],))

    @contextmanager
    def _object_lock(self, exclusive=False):
        # Shared readers never take a SQLite write lock. GC opens exclusively;
        # all paths acquire the object lock before any database transaction.
        fd = os.open(self.locks / 'objects', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def collect_garbage(self):
        try:
            with self.connection() as db:
                rows = db.execute('SELECT * FROM garbage ORDER BY (blob<=?),blob LIMIT 256',
                                  (self._gc_cursor,)).fetchall()
            for row in rows:
                self._gc_cursor = row['blob']
                try:
                    with self._object_lock(exclusive=True), self.transaction() as db:
                        referenced = db.execute('SELECT 1 FROM entries WHERE blob=? UNION SELECT 1 FROM versions WHERE blob=?',
                                                (row['blob'], row['blob'])).fetchone()
                        if referenced:
                            continue
                        (self.blobs / row['blob']).unlink(missing_ok=True)
                        sync_dir(self.blobs)
                        db.execute('DELETE FROM garbage WHERE blob=?', (row['blob'],))
                except (OSError, sqlite3.Error):
                    logger.warning('Cloud object cleanup deferred for %s', row['blob'])
        except (OSError, sqlite3.Error):
            logger.warning('Cloud object cleanup deferred')

    def open_content(self, ident):
        with self._object_lock(), self.connection() as db:
            entry = self._entry(db, ident)
            if entry['kind'] != 'file':
                raise CloudError('not_found', 404)
            source = (self.blobs / entry['blob']).open('rb')
        return entry, source

    def open_version(self, entry):
        with self._object_lock(), self.connection() as db:
            referenced = db.execute('SELECT 1 FROM entries WHERE blob=? UNION SELECT 1 FROM versions WHERE blob=?',
                                    (entry['blob'], entry['blob'])).fetchone()
            if not referenced:
                raise CloudError('not_found', 404)
            return (self.blobs / entry['blob']).open('rb')

    def delete_file(self, device, ident, revision, idem):
        if not revision:
            raise CloudError('precondition_required', 428)
        if not isinstance(idem, str) or not re.fullmatch('[A-Za-z0-9_-]{16,128}', idem):
            raise CloudError('invalid_idempotency_key')
        with self.transaction() as db:
            self._active(db, device)
            previous = db.execute('SELECT * FROM deletions WHERE device=? AND idem=?', (device, idem)).fetchone()
            if previous:
                if (previous['file_id'], previous['revision']) != (ident, revision):
                    raise CloudError('idempotency_conflict', 409)
                return json.loads(previous['result'])
            entry = self._entry(db, ident)
            if entry['kind'] != 'file':
                raise CloudError('not_a_file', 409)
            if revision != '"' + entry['revision'] + '"':
                raise CloudError('version_conflict', 412, current=self.public(entry))
            db.execute('INSERT INTO tombstones VALUES (?,?,?,?,?)',
                       (ident, entry['parent'], entry['name'], entry['revision'], time.time()))
            for row in db.execute("SELECT id,spec FROM uploads WHERE state='pending'").fetchall():
                spec = json.loads(row['spec'])
                if spec['parent_id'] == entry['parent'] and spec['name'] == entry['name']:
                    db.execute("UPDATE uploads SET state='invalidated' WHERE id=?", (row['id'],))
            db.execute('INSERT OR IGNORE INTO garbage VALUES (?,?)', (entry['blob'], entry['size']))
            db.execute('INSERT OR IGNORE INTO garbage SELECT blob,size FROM versions WHERE file_id=?', (ident,))
            db.execute('DELETE FROM versions WHERE file_id=?', (ident,))
            db.execute('DELETE FROM entries WHERE id=?', (ident,))
            self._touch(db, entry['parent'])
            result = self.envelope(deleted=True, file_id=ident, revision=entry['revision'])
            db.execute('INSERT INTO deletions VALUES (?,?,?,?,?)', (device, idem, ident, revision, json.dumps(result)))
        self.collect_expired()
        self.collect_garbage()
        return result

    @staticmethod
    def _active(db, device):
        row = db.execute('SELECT * FROM devices WHERE id=?', (device,)).fetchone()
        if row is None or row['revoked']:
            raise CloudError('device_revoked', 403)

    def associate(self, name):
        name = name_checked(name)
        token, device = secrets.token_urlsafe(32), new_id()
        with self.transaction() as db:
            db.execute('INSERT INTO devices(id,name,token_hash) VALUES (?,?,?)',
                       (device, name, hashlib.sha256(token.encode()).hexdigest()))
        return self.envelope(device_id=device, token=token)

    def authenticate(self, token):
        if not token or len(token) > 256:
            raise CloudError('authentication_required', 401)
        with self.connection() as db:
            row = db.execute('SELECT * FROM devices WHERE token_hash=?',
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise CloudError('authentication_required', 401)
        if row['revoked']:
            raise CloudError('device_revoked', 403)
        return row['id']

    def revoke(self, device):
        with self.transaction() as db:
            db.execute('UPDATE devices SET revoked=1 WHERE id=?', (device,))
            db.execute("UPDATE uploads SET state='cancelled' WHERE device=? AND state='pending'", (device,))
        self.collect_expired()
        return self.envelope(revoked=True)

    @staticmethod
    def _entry(db, ident):
        row = db.execute('SELECT * FROM entries WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise CloudError('not_found', 404)
        return dict(row)

    @staticmethod
    def public(entry):
        return {k: v for k, v in entry.items() if k != 'blob'}

    def entry(self, ident):
        with self.connection() as db:
            return self._entry(db, ident)

    def resolve(self, path):
        entry = self.entry('root')
        with self.connection() as db:
            for part in path.strip('/').split('/') if path.strip('/') else []:
                part = name_checked(part)
                if entry['kind'] != 'directory':
                    raise CloudError('not_found', 404)
                row = db.execute('SELECT * FROM entries WHERE parent=? AND name=?', (entry['id'], part)).fetchone()
                if row is None:
                    raise CloudError('not_found', 404)
                entry = dict(row)
        return entry

    def entries(self, parent='root', cursor='', limit=100):
        with self.connection() as db:
            directory = self._entry(db, parent)
            if directory['kind'] != 'directory':
                raise CloudError('not_a_directory')
            rows = db.execute('SELECT * FROM entries WHERE parent=? AND id>? ORDER BY id LIMIT ?',
                              (parent, cursor, limit + 1)).fetchall()
        return self.envelope(parent=self.public(directory), entries=[self.public(dict(r)) for r in rows[:limit]],
                             next_cursor=rows[limit - 1]['id'] if len(rows) > limit else None)

    def mkdir(self, device, parent, name, parent_revision):
        name = name_checked(name)
        with self.transaction() as db:
            self._active(db, device)
            self._parent(db, parent, parent_revision)
            try:
                ident = new_id()
                db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)',
                           (ident, parent, name, 'directory', new_id(), None, 0, None, time.time()))
            except sqlite3.IntegrityError:
                raise CloudError('name_conflict', 409) from None
            self._touch(db, parent)
            return self.envelope(entry=self.public(self._entry(db, ident)))

    def _parent(self, db, ident, revision):
        parent = self._entry(db, ident)
        if parent['kind'] != 'directory' or parent['revision'] != revision:
            raise CloudError('destination_changed', 409)

    @staticmethod
    def _usage(db):
        return (db.execute('SELECT COALESCE(SUM(size),0) FROM entries').fetchone()[0]
                + db.execute('SELECT COALESCE(SUM(size),0) FROM versions').fetchone()[0])

    @staticmethod
    def _reserved(db):
        # Failed cleanup remains charged, including expired/cancelled/committed parts.
        rows = db.execute("SELECT spec FROM uploads WHERE state='pending' OR cleanup_pending=1").fetchall()
        return sum(json.loads(r['spec'])['size'] for r in rows) + db.execute('SELECT COALESCE(SUM(size),0) FROM garbage').fetchone()[0]

    def status(self, device):
        with self.connection() as db:
            self._active(db, device)
            return self.envelope(state='ready', device_id=device, quota_bytes=self.settings.quota,
                                 used_bytes=self._usage(db), reserved_bytes=self._reserved(db),
                                 max_file_bytes=self.settings.max_file, chunk_bytes=self.settings.chunk_size,
                                 root=self.public(self._entry(db, 'root')), webdav='read_only',
                                 features=self.features(), max_old_versions=self.settings.max_versions,
                                 garbage_bytes=db.execute('SELECT COALESCE(SUM(size),0) FROM garbage').fetchone()[0])

    def create_upload(self, device, spec, idem):
        # The caller supplies a complete immutable intent, not a filename used as a disk path.
        self.collect_expired()
        self.collect_garbage()
        spec = dict(spec)
        spec['name'] = name_checked(spec.get('name'))
        digest_checked(spec.get('sha256'))
        size = spec.get('size')
        if type(size) is not int or size < 0 or size > self.settings.max_file:
            raise CloudError('file_too_large', 413)
        if not isinstance(idem, str) or not re.fullmatch('[A-Za-z0-9_-]{16,128}', idem):
            raise CloudError('invalid_idempotency_key')
        if spec.get('policy') not in ('create', 'replace', 'keep_both'):
            raise CloudError('invalid_policy')
        if spec['policy'] == 'replace' and not spec.get('expected_revision'):
            raise CloudError('precondition_required', 428)
        encoded = json.dumps(spec, sort_keys=True, separators=(',', ':'))
        with self.transaction() as db:
            self._active(db, device)
            old = db.execute('SELECT * FROM uploads WHERE device=? AND idem=?', (device, idem)).fetchone()
            if old:
                if old['spec'] != encoded:
                    raise CloudError('idempotency_conflict', 409)
                return self._upload_response(db, old)
            self._parent(db, spec.get('parent_id'), spec.get('parent_revision'))
            if spec['policy'] == 'create':
                current = db.execute('SELECT * FROM entries WHERE parent=? AND name=?',
                                     (spec['parent_id'], spec['name'])).fetchone()
                if current:
                    raise CloudError('version_conflict', 412, current=self.public(dict(current)))
            if db.execute("SELECT COUNT(*) FROM uploads WHERE device=? AND state='pending' AND expires>?", (device, time.time())).fetchone()[0] >= self.settings.max_uploads:
                raise CloudError('too_many_transfers', 429)
            reserved = self._reserved(db)
            if self._usage(db) + reserved + size > self.settings.quota:
                raise CloudError('quota_exceeded', 507)
            # Parts plus assembly coexist until cleanup. Keep room for both and other reservations.
            if shutil.disk_usage(self.root).free < 2 * (size + reserved) + 1024**2:
                raise CloudError('storage_full', 507)
            ident = new_id()
            db.execute('INSERT INTO uploads(id,device,idem,spec,state,expires,result) VALUES (?,?,?,?,?,?,?)',
                       (ident, device, idem, encoded, 'pending', time.time() + self.settings.ttl, None))
            return self._upload_response(db, self._get_upload(db, device, ident))

    def _get_upload(self, db, device, ident):
        self._active(db, device)
        row = db.execute('SELECT * FROM uploads WHERE id=? AND device=?', (ident, device)).fetchone()
        if row is None:
            raise CloudError('not_found', 404)
        return row

    def _pending(self, row):
        if row['state'] == 'invalidated':
            raise CloudError('upload_invalidated', 410)
        if row['state'] == 'expired' or (row['state'] == 'pending' and row['expires'] < time.time()):
            raise CloudError('upload_expired', 410)
        if row['state'] != 'pending':
            raise CloudError('upload_not_pending', 409)

    def _upload_response(self, db, row):
        state = 'expired' if row['state'] == 'pending' and row['expires'] < time.time() else row['state']
        chunks = ([dict(r) for r in db.execute('SELECT idx,sha256,size FROM chunks WHERE upload=? ORDER BY idx', (row['id'],))]
                  if state == 'pending' else [])
        return self.envelope(upload_id=row['id'], state=state, expires=row['expires'],
                             chunk_bytes=self.settings.chunk_size, chunks=chunks,
                             result=json.loads(row['result']) if row['result'] else None)

    def upload_status(self, device, ident):
        self.collect_expired(ident)
        with self.connection() as db:
            return self._upload_response(db, self._get_upload(db, device, ident))

    def chunk(self, device, ident, index, data, checksum):
        with self._upload_lock(ident):
            try:
                return self._chunk(device, ident, index, data, checksum)
            finally:
                self._cleanup_terminal(ident)

    def _chunk(self, device, ident, index, data, checksum):
        digest_checked(checksum)
        if hashlib.sha256(data).hexdigest() != checksum:
            raise CloudError('integrity_error', 422)
        with self.transaction() as db:
            row = self._get_upload(db, device, ident)
            self._pending(row)
            size = json.loads(row['spec'])['size']
            expected = min(self.settings.chunk_size, size - index * self.settings.chunk_size)
            if index < 0 or expected <= 0 or len(data) != expected:
                raise CloudError('invalid_chunk')
            old = db.execute('SELECT sha256 FROM chunks WHERE upload=? AND idx=?', (ident, index)).fetchone()
            if old:
                if old['sha256'] != checksum:
                    raise CloudError('chunk_conflict', 409)
                return self.envelope(received=index)
            target = self.parts / (ident + '-' + str(index))
            with target.open('wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            sync_dir(self.parts)
            db.execute('INSERT INTO chunks VALUES (?,?,?,?)', (ident, index, checksum, len(data)))
        return self.envelope(received=index)

    def commit(self, device, ident):
        with self._upload_lock(ident):
            try:
                return self._commit(device, ident)
            finally:
                self._cleanup_terminal(ident)
                self.collect_garbage()

    def _commit(self, device, ident):
        with self.connection() as db:
            row = self._get_upload(db, device, ident)
            if row['state'] == 'committed':
                return self._upload_response(db, row)
            self._pending(row)
            spec = json.loads(row['spec'])
            chunks = list(db.execute('SELECT * FROM chunks WHERE upload=? ORDER BY idx', (ident,)))
        expected_count = (spec['size'] + self.settings.chunk_size - 1) // self.settings.chunk_size
        if len(chunks) != expected_count or any(r['idx'] != i for i, r in enumerate(chunks)):
            raise CloudError('incomplete_upload', 409)
        blob = new_id()
        target = self.blobs / blob
        published = False
        try:
            total, digest = 0, hashlib.sha256()
            with target.open('xb') as dest:
                for chunk in chunks:
                    path = self.parts / (ident + '-' + str(chunk['idx']))
                    with path.open('rb') as src:
                        while data := src.read(1024**2):
                            total += len(data)
                            digest.update(data)
                            dest.write(data)
                dest.flush()
                os.fsync(dest.fileno())
            if total != spec['size'] or digest.hexdigest() != spec['sha256']:
                raise CloudError('integrity_error', 422)
            sync_dir(self.blobs)
            with self.transaction() as db:
                current = self._get_upload(db, device, ident)
                if current['state'] == 'committed':
                    return self._upload_response(db, current)
                self._pending(current)
                existing = db.execute('SELECT * FROM entries WHERE parent=? AND name=?',
                                      (spec['parent_id'], spec['name'])).fetchone()
                name = spec['name']
                if spec['policy'] == 'replace':
                    if existing is None or existing['kind'] != 'file' or existing['revision'] != spec['expected_revision']:
                        raise CloudError('version_conflict', 412,
                                         current=self.public(dict(existing)) if existing else None)
                elif existing:
                    if spec['policy'] == 'create':
                        raise CloudError('version_conflict', 412, current=self.public(dict(existing)))
                    stem, suffix = os.path.splitext(name)
                    number = 2
                    while db.execute('SELECT 1 FROM entries WHERE parent=? AND name=?', (spec['parent_id'], name)).fetchone():
                        name = name_checked(stem[:80] + ' (' + str(number) + ')' + suffix[:30])
                        number += 1
                    existing = None
                parent = self._entry(db, spec['parent_id'])
                if parent['kind'] != 'directory':
                    raise CloudError('destination_changed', 409)
                now, revision = time.time(), new_id()
                if existing:
                    db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?)',
                               (existing['revision'], existing['id'], existing['blob'], existing['size'], existing['sha256'], now))
                    entry_id = existing['id']
                    db.execute('UPDATE entries SET revision=?,blob=?,size=?,sha256=?,modified=? WHERE id=?',
                               (revision, blob, total, spec['sha256'], now, entry_id))
                else:
                    entry_id = new_id()
                    db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)',
                               (entry_id, spec['parent_id'], name, 'file', revision, blob, total, spec['sha256'], now))
                self._touch(db, spec['parent_id'])
                self._trim_versions(db, entry_id)
                result = self.public(self._entry(db, entry_id))
                db.execute("UPDATE uploads SET state='committed',result=? WHERE id=?", (json.dumps(result), ident))
                response = self._upload_response(db, self._get_upload(db, device, ident))
            published = True
            return response
        except OSError as exc:
            if exc.errno == 28:
                raise CloudError('storage_full', 507) from None
            raise
        finally:
            if not published:
                # A disk/commit error can make the outcome uncertain. Never remove
                # a blob unless the database can prove it is unreferenced.
                try:
                    with self.connection() as db:
                        referenced = db.execute('SELECT 1 FROM entries WHERE blob=? UNION SELECT 1 FROM versions WHERE blob=?',
                                                (blob, blob)).fetchone()
                    if not referenced:
                        target.unlink(missing_ok=True)
                except (sqlite3.Error, OSError):
                    pass  # offline maintenance can recover the orphan later

    def cancel(self, device, ident):
        with self.transaction() as db:
            row = self._get_upload(db, device, ident)
            if row['state'] == 'pending':
                db.execute("UPDATE uploads SET state='cancelled' WHERE id=?", (ident,))
            response = self._upload_response(db, self._get_upload(db, device, ident))
        self.collect_expired(ident)
        return response

    def maintenance(self):
        """Run with service stopped: clean orphan blobs/parts, keep all committed receipts and versions."""
        with self.transaction() as db:
            db.execute("UPDATE uploads SET state='expired' WHERE state='pending' AND expires<?", (time.time(),))
            referenced = {r[0] for r in db.execute('SELECT blob FROM entries WHERE blob IS NOT NULL UNION SELECT blob FROM versions')}
            active = {r[0] for r in db.execute("SELECT id FROM uploads WHERE state='pending'")}
            for path in self.blobs.iterdir():
                if path.name not in referenced and path.is_file():
                    path.unlink()
            for path in self.parts.iterdir():
                if path.name.split('-')[0] not in active and path.is_file():
                    path.unlink()
