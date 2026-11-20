"""Internal service. Reach through the gateway, never expose this port publicly."""
import hmac
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.wsgi import WSGIMiddleware

from cloud.storage import CloudError, Settings, Store


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Device(Model):
    name: str = Field(min_length=1, max_length=100)


class Directory(Model):
    parent_id: str = Field(default='root', max_length=64)
    parent_revision: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=240)


class Upload(Directory):
    size: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern='^[0-9a-f]{64}$')
    policy: Literal['create', 'replace', 'keep_both'] = 'create'
    expected_revision: str | None = Field(default=None, max_length=64)


def enabled():
    return os.environ.get('DEPLOY_MODE', 'local').strip().lower() == 'server' and os.environ.get('CLOUD_ENABLED', '0') == '1'


def create_app(settings=None, internal_key=None):
    settings = settings or Settings.environment()
    key = internal_key if internal_key is not None else os.environ.get('CLOUD_INTERNAL_KEY', '')
    if len(key) < 32:
        raise RuntimeError('CLOUD_INTERNAL_KEY must contain at least 32 characters')
    store = Store(settings)
    app = FastAPI(title='JARVIS Cloud internal', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.exception_handler(CloudError)
    async def error_handler(request, exc):
        return JSONResponse(store.envelope(error=exc.code, **exc.context), status_code=exc.status)

    @app.exception_handler(OSError)
    async def disk_error(request, exc):
        return JSONResponse(store.envelope(error='storage_full' if exc.errno == 28 else 'storage_error'),
                            status_code=507 if exc.errno == 28 else 503)

    @app.middleware('http')
    async def internal_only(request, call_next):
        if not enabled():
            return JSONResponse({'error': 'cloud_disabled'}, status_code=403)
        supplied = request.headers.get('X-Cloud-Internal-Key', '')
        if not supplied or not hmac.compare_digest(key, supplied):
            return JSONResponse({'error': 'authentication_required'}, status_code=401)
        if request.method in ('POST', 'PUT', 'PATCH'):
            limit = settings.chunk_size if '/chunks/' in request.url.path else 8192
            # Count streamed bodies too: Content-Length alone is not a size limit.
            size = 0
            original = request._receive

            async def bounded_receive():
                nonlocal size
                message = await original()
                size += len(message.get('body', b''))
                if size > limit:
                    raise HTTPException(413, 'body_too_large')
                return message

            request._receive = bounded_receive
            try:
                if int(request.headers.get('content-length', '0')) > limit:
                    return JSONResponse({'error': 'body_too_large'}, status_code=413)
            except ValueError:
                return JSONResponse({'error': 'invalid_length'}, status_code=400)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def device(authorization: str = Header(default='')):
        kind, _, token = authorization.partition(' ')
        if kind.lower() != 'bearer':
            raise CloudError('authentication_required', 401)
        return store.authenticate(token)

    def admin(x_cloud_admin: str = Header(default='')):
        if x_cloud_admin != '1':
            raise CloudError('authentication_required', 401)

    @app.get('/health')
    def health():
        return store.envelope(state='ready')

    prefix = '/api/cloud/v1'

    @app.post(prefix + '/devices', dependencies=[Depends(admin)])
    def associate(body: Device):
        return store.associate(body.name)

    @app.delete(prefix + '/devices/{ident}', dependencies=[Depends(admin)])
    def revoke(ident: str):
        return store.revoke(ident)

    @app.get(prefix + '/status')
    def status(ident=Depends(device)):
        return store.status(ident)

    @app.get(prefix + '/entries')
    def entries(parent_id: str = 'root', cursor: str = '', ident=Depends(device)):
        return store.entries(parent_id, cursor)

    @app.post(prefix + '/directories')
    def mkdir(body: Directory, ident=Depends(device)):
        return store.mkdir(ident, body.parent_id, body.name, body.parent_revision)

    @app.get(prefix + '/files/{file_id}/content')
    def download(file_id: str, request: Request, ident=Depends(device)):
        entry = store.entry(file_id)
        if entry['kind'] != 'file':
            raise CloudError('not_found', 404)
        etag = '"' + entry['revision'] + '"'
        start, end, code = 0, entry['size'] - 1, 200
        headers = {'ETag': etag, 'Accept-Ranges': 'bytes'}
        value = request.headers.get('range', '')
        if value and request.headers.get('if-range', etag) == etag:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
            if not match or not any(match.groups()) or not entry['size']:
                return JSONResponse({'error': 'invalid_range'}, status_code=416,
                                    headers={'Content-Range': f"bytes */{entry['size']}"})
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(end, int(right)) if right else end
            else:
                start = max(0, entry['size'] - int(right))
            if start > end:
                return JSONResponse({'error': 'invalid_range'}, status_code=416,
                                    headers={'Content-Range': f"bytes */{entry['size']}"})
            code = 206
            headers['Content-Range'] = f"bytes {start}-{end}/{entry['size']}"
        headers['Content-Length'] = str(max(0, end - start + 1))

        def content():
            with (store.blobs / entry['blob']).open('rb') as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    data = source.read(min(1024**2, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(content(), status_code=code, headers=headers, media_type='application/octet-stream')

    @app.post(prefix + '/uploads')
    def create_upload(body: Upload, idempotency_key: str = Header(default=''), ident=Depends(device)):
        return store.create_upload(ident, body.model_dump(), idempotency_key)

    @app.put(prefix + '/uploads/{upload_id}/chunks/{index}')
    async def chunk(upload_id: str, index: int, request: Request,
                    x_content_sha256: str = Header(default=''), ident=Depends(device)):
        # At most one configured chunk in memory, bounded above by middleware.
        data = await request.body()
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(store.chunk, ident, upload_id, index, data, x_content_sha256)

    @app.get(prefix + '/uploads/{upload_id}')
    def transfer(upload_id: str, ident=Depends(device)):
        return store.upload_status(ident, upload_id)

    @app.post(prefix + '/uploads/{upload_id}/commit')
    def commit(upload_id: str, ident=Depends(device)):
        return store.commit(ident, upload_id)

    @app.delete(prefix + '/uploads/{upload_id}')
    def cancel(upload_id: str, ident=Depends(device)):
        return store.cancel(ident, upload_id)

    from cloud.dav import application
    app.mount('/cloud/dav', WSGIMiddleware(application(store)))
    return app
