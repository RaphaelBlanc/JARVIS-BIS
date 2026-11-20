"""Optional WebDAV writes through the same revisions, quota and immutable blobs.

PUT never enters Starlette's buffering WSGI adapter. Each chunk is persisted by
Store; no visible file exists until the verified final commit.
"""
import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit
import uuid

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from cloud.body_limit import BodyRejected, bounded_dav_stream
from cloud.storage import CloudError, name_checked

ALLOW = 'OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, MOVE, DELETE'


def match_header(value):
    # GVfs supplies its queried DAV ETag without quotes on replacement. Only
    # accept this exact opaque token, not weak tags or lists. The v1 JSON API
    # retains its stricter quoted If-Match contract.
    return '"' + value + '"' if value and re.fullmatch('[0-9a-f]{32}', value) else value


def authenticated(store, request):
    try:
        kind, value = request.headers.get('authorization', '').split(' ', 1)
        if kind.lower() != 'basic':
            raise ValueError
        device, token = base64.b64decode(value, validate=True).decode().split(':', 1)
        if store.authenticate(token) != device:
            raise ValueError
        return device
    except (ValueError, UnicodeError):
        raise CloudError('authentication_required', 401) from None


def checked_path(path):
    parts = path.strip('/').split('/') if path.strip('/') else []
    return '/'.join(name_checked(part) for part in parts)


def target(store, path):
    parts = checked_path(path).split('/')
    if not parts or not parts[-1]:
        raise CloudError('root_protected', 403)
    return store.resolve('/'.join(parts[:-1])), parts[-1]


def current(store, path):
    try:
        return store.resolve(path)
    except CloudError as exc:
        if exc.status == 404:
            return None
        raise


def finish_intent(store, device, upload, size, digest):
    with store._upload_lock(upload), store.transaction() as db:
        row = store._get_upload(db, device, upload)
        store._pending(row)
        spec = json.loads(row['spec'])
        if size > spec['size']:
            raise CloudError('file_too_large', 413)
        spec.update(size=size, sha256=digest)
        db.execute('UPDATE uploads SET spec=? WHERE id=?',
                   (json.dumps(spec, sort_keys=True, separators=(',', ':')), upload))


async def put(store, device, path, request):
    parent, name = await run_in_threadpool(target, store, path)
    entry = await run_in_threadpool(current, store, path)
    if entry and entry['kind'] != 'file':
        raise CloudError('not_a_file', 409)
    match = match_header(request.headers.get('if-match'))
    if match:
        if not entry or (match != '*' and match != '"' + entry['revision'] + '"'):
            raise CloudError('version_conflict', 412)
    if request.headers.get('if-none-match') == '*' and entry:
        raise CloudError('version_conflict', 412)
    try:
        declared = int(request.headers['content-length']) if 'content-length' in request.headers else None
    except ValueError:
        raise CloudError('invalid_length') from None
    size = declared if declared is not None else store.settings.max_file
    if not 0 <= size <= store.settings.max_file:
        raise CloudError('file_too_large', 413)
    spec = dict(parent_id=parent['id'], parent_revision=parent['revision'], name=name, size=size,
                sha256='0' * 64, policy='replace' if entry else 'create',
                expected_revision=entry['revision'] if entry else None)
    transfer = await run_in_threadpool(store.create_upload, device, spec, uuid.uuid4().hex)
    upload = transfer['upload_id']
    block, total, index, digest = bytearray(), 0, 0, hashlib.sha256()
    published = False
    try:
        async with asyncio.timeout(3600):
            async for data in bounded_dav_stream(request.scope, request.receive, limit=size):
                total += len(data)
                digest.update(data)
                # Copy at most one configured chunk into our accumulation buffer.
                offset = 0
                while offset < len(data):
                    count = min(len(data) - offset, store.settings.chunk_size - len(block))
                    block.extend(data[offset:offset + count]); offset += count
                    if len(block) == store.settings.chunk_size:
                        content = bytes(block); block.clear()
                        await run_in_threadpool(store.chunk, device, upload, index, content, hashlib.sha256(content).hexdigest())
                        index += 1
            if declared is not None and total != declared:
                raise CloudError('integrity_error', 422)
            await run_in_threadpool(finish_intent, store, device, upload, total, digest.hexdigest())
            if block:
                content = bytes(block)
                await run_in_threadpool(store.chunk, device, upload, index, content, hashlib.sha256(content).hexdigest())
            result = await run_in_threadpool(store.commit, device, upload)
            published = True
            return Response(status_code=204 if entry else 201,
                            headers={'ETag': '"' + result['result']['revision'] + '"', 'Cache-Control': 'no-store'})
    except TimeoutError:
        raise CloudError('body_timeout', 408) from None
    finally:
        if not published:
            try:
                await asyncio.shield(run_in_threadpool(store.cancel, device, upload))
            except CloudError:
                pass  # Revocation has already cancelled and scheduled its cleanup.


def namespace(store, device, path, request):
    if request.method == 'MKCOL':
        parent, name = target(store, path)
        store.mkdir(device, parent['id'], name, parent['revision'])
        return Response(status_code=201)
    entry = store.resolve(checked_path(path))
    revision = match_header(request.headers.get('if-match', '"' + entry['revision'] + '"'))
    if revision == '*':
        revision = '"' + entry['revision'] + '"'
    if request.method == 'DELETE':
        recursive = request.headers.get('depth', 'infinity') != '0'
        snapshot = store.delete_preview(device, entry['id'])
        store.delete_entry(device, entry['id'], revision, uuid.uuid4().hex, recursive, snapshot['tree_revision'])
        return Response(status_code=204)
    destination = urlsplit(request.headers.get('destination', ''))
    # The gateway normalizes same-origin absolute destinations to relative paths.
    if destination.netloc or destination.query or destination.fragment or not destination.path.startswith('/cloud/dav/'):
        raise CloudError('invalid_destination')
    parent, name = target(store, unquote(destination.path[len('/cloud/dav/'):]))
    # Moving into an occupied name never silently removes an existing resource.
    # In-place saves use PUT and retain the previous file version.
    result = store.move_entry(device, entry['id'], revision, parent['id'], parent['revision'], name, uuid.uuid4().hex)
    return Response(status_code=201, headers={'ETag': '"' + result['entry']['revision'] + '"'})


def router(store):
    routes = APIRouter()

    @routes.api_route('/cloud/dav/{path:path}', methods=['OPTIONS', 'PUT', 'DELETE', 'MOVE', 'MKCOL'])
    async def write(path: str, request: Request):
        try:
            device = await run_in_threadpool(authenticated, store, request)
            path = checked_path(path)
            if request.method == 'OPTIONS':
                return Response(headers={'Allow': ALLOW, 'DAV': '1', 'Cache-Control': 'no-store'})
            if request.method == 'PUT':
                return await put(store, device, path, request)
            if request.method in ('MKCOL', 'MOVE') and await request.body():
                raise CloudError('unsupported_media_type', 415)
            return await run_in_threadpool(namespace, store, device, path, request)
        except (CloudError, BodyRejected) as exc:
            headers = {'Cache-Control': 'no-store'}
            if exc.status == 401:
                headers['WWW-Authenticate'] = 'Basic realm="Cloud JARVIS"'
            return Response('<d:error xmlns:d="DAV:"><d:' + exc.code + '/></d:error>',
                            status_code=exc.status, media_type='application/xml', headers=headers)

    return routes
