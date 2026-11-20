"""Transactional namespace over immutable blobs on a local persistent volume."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
import uuid


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

    @classmethod
    def environment(cls):
        return cls(Path(os.environ.get('CLOUD_DATA_DIR', '/data')),
                   int(os.environ.get('CLOUD_QUOTA_BYTES', 20 * 1024**3)),
                   int(os.environ.get('CLOUD_MAX_FILE_BYTES', 5 * 1024**3)),
                   int(os.environ.get('CLOUD_CHUNK_BYTES', 4 * 1024**2)),
                   int(os.environ.get('CLOUD_UPLOAD_TTL', 86400)),
                   int(os.environ.get('CLOUD_MAX_UPLOADS', 8)))


def new_id():
    return uuid.uuid4().hex


def name_checked(name):
    if not isinstance(name, str):
        raise CloudError('invalid_name')
    name = unicodedata.normalize('NFC', name)
    if (not name or name in ('.', '..') or len(name.encode()) > 240
            or any(c in '/\\' or unicodedata.category(c).startswith('C') for c in name)):
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
        self.root = settings.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs, self.parts = self.root / 'objects', self.root / 'uploads'
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        self.parts.mkdir(mode=0o700, exist_ok=True)
        self.database = self.root / 'cloud.sqlite3'
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
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
                    result TEXT,UNIQUE(device,idem));
                CREATE TABLE IF NOT EXISTS chunks(upload TEXT NOT NULL,idx INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,size INTEGER NOT NULL,PRIMARY KEY(upload,idx));
                PRAGMA user_version=1;
            ''')
        os.chmod(self.database, 0o600)
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('server_id', new_id()))
            db.execute('INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?,?)',
                       ('root', None, '', 'directory', new_id(), None, 0, None, time.time()))
            self.server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]

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
        rows = db.execute("SELECT spec FROM uploads WHERE state='pending' AND expires>?", (time.time(),)).fetchall()
        return sum(json.loads(r['spec'])['size'] for r in rows)

    def status(self, device):
        with self.connection() as db:
            self._active(db, device)
            return self.envelope(state='ready', device_id=device, quota_bytes=self.settings.quota,
                                 used_bytes=self._usage(db), reserved_bytes=self._reserved(db),
                                 max_file_bytes=self.settings.max_file, chunk_bytes=self.settings.chunk_size,
                                 root=self.public(self._entry(db, 'root')), webdav='read_only')

    def create_upload(self, device, spec, idem):
        # The caller supplies a complete immutable intent, not a filename used as a disk path.
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
            if db.execute("SELECT COUNT(*) FROM uploads WHERE state='pending' AND expires>?", (time.time(),)).fetchone()[0] >= self.settings.max_uploads:
                raise CloudError('too_many_transfers', 429)
            reserved = self._reserved(db)
            if self._usage(db) + reserved + size > self.settings.quota:
                raise CloudError('quota_exceeded', 507)
            # Parts plus assembly coexist until cleanup. Keep room for both and other reservations.
            if shutil.disk_usage(self.root).free < 2 * (size + reserved) + 1024**2:
                raise CloudError('storage_full', 507)
            ident = new_id()
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?,?)',
                       (ident, device, idem, encoded, 'pending', time.time() + self.settings.ttl, None))
            return self._upload_response(db, self._get_upload(db, device, ident))

    def _get_upload(self, db, device, ident):
        self._active(db, device)
        row = db.execute('SELECT * FROM uploads WHERE id=? AND device=?', (ident, device)).fetchone()
        if row is None:
            raise CloudError('not_found', 404)
        return row

    def _pending(self, row):
        if row['state'] != 'pending':
            raise CloudError('upload_not_pending', 409)
        if row['expires'] < time.time():
            raise CloudError('upload_expired', 410)

    def _upload_response(self, db, row):
        chunks = [dict(r) for r in db.execute('SELECT idx,sha256,size FROM chunks WHERE upload=? ORDER BY idx', (row['id'],))]
        state = 'expired' if row['state'] == 'pending' and row['expires'] < time.time() else row['state']
        return self.envelope(upload_id=row['id'], state=state, expires=row['expires'],
                             chunk_bytes=self.settings.chunk_size, chunks=chunks,
                             result=json.loads(row['result']) if row['result'] else None)

    def upload_status(self, device, ident):
        with self.connection() as db:
            return self._upload_response(db, self._get_upload(db, device, ident))

    def chunk(self, device, ident, index, data, checksum):
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
                self._parent(db, spec['parent_id'], spec['parent_revision'])
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
            if row['state'] != 'committed':
                db.execute("UPDATE uploads SET state='cancelled' WHERE id=?", (ident,))
            return self._upload_response(db, self._get_upload(db, device, ident))

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
