"""Optional cloud routes: whitelist requests and stream without AI or body buffering."""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit, quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from cloud.body_limit import BodyRejected, bounded_dav_body, bounded_dav_stream, DAV_UPLOAD_TIMEOUT


def configuration():
    server = os.environ.get('DEPLOY_MODE', 'local').strip().lower() == 'server'
    enabled = server and os.environ.get('CLOUD_ENABLED', '0') == '1'
    url = os.environ.get('CLOUD_INTERNAL_URL', 'http://127.0.0.1:8091').rstrip('/')
    key = os.environ.get('CLOUD_INTERNAL_KEY', '')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        enabled = False
    return server, enabled, url, key


async def capability():
    server, enabled, url, key = configuration()
    result = dict(api_version=1, enabled=enabled, state='disabled', reason=None,
                  server_id=None, api_path='/api/cloud/v1', dav_path='/cloud/dav/')
    if not enabled:
        result['reason'] = 'local_deployment' if not server else 'not_configured'
        return result
    result['state'] = 'unavailable'
    if len(key) < 32:
        result['reason'] = 'not_configured'
        return result
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            response = await client.get(url + '/health', headers={'X-Cloud-Internal-Key': key})
            response.raise_for_status()
            data = response.json()
        result['features'] = dict(data.get('features', {}))
        result['features']['webdav_write'] = (result['features'].get('webdav_write') is True
                                             and os.environ.get('CLOUD_DAV_WRITE', '0') == '1')
        result.update(state='ready', server_id=data['server_id'])
    except (httpx.HTTPError, ValueError, KeyError):
        result['reason'] = 'cloud_unavailable'
    return result


_ALLOWED = {
    'GET': r'(?:status|entries(?:/(?:root|[0-9a-f]{32})(?:/delete-preview)?)?|files/[0-9a-f]{32}/content|uploads/[0-9a-f]{32})',
    'POST': r'(?:directories|uploads|uploads/[0-9a-f]{32}/commit|entries/(?:root|[0-9a-f]{32})/move)',
    'PUT': r'uploads/[0-9a-f]{32}/chunks/[0-9]{1,10}',
    'DELETE': r'(?:uploads|files|entries)/[0-9a-f]{32}',
}
_HEADERS = {'authorization', 'content-type', 'content-length', 'idempotency-key', 'x-content-sha256',
            'range', 'if-range', 'if-match', 'if-none-match', 'depth', 'translate', 'destination', 'overwrite'}
_RESPONSE_HEADERS = {'content-type', 'content-length', 'content-range', 'accept-ranges', 'etag',
                     'last-modified', 'content-disposition', 'www-authenticate', 'dav', 'allow', 'retry-after'}


def rate_limited_cloud_route(method, path):
    return ((method == 'GET' and path == '/api/cloud/v1/capabilities') or
            (method == 'POST' and path == '/api/cloud/v1/devices') or
            (method == 'DELETE' and re.fullmatch(r'/api/cloud/v1/devices/[^/]+', path) is not None))


def router(require_secret):
    routes = APIRouter()

    @routes.get('/api/cloud/v1/capabilities', dependencies=[Depends(require_secret)])
    async def cloud_capabilities():
        return await capability()

    async def proxy(request, path, admin=False, content=None, streamed=None):
        _, enabled, url, key = configuration()
        if not enabled:
            return JSONResponse({'error': 'cloud_disabled'}, status_code=403)
        if len(key) < 32:
            return JSONResponse({'error': 'cloud_unavailable'}, status_code=503)
        headers = {k: v for k, v in request.headers.items() if k.lower() in _HEADERS}
        headers['X-Cloud-Internal-Key'] = key
        if 'destination' in headers:
            destination = urlsplit(headers['destination'])
            if (destination.query or destination.fragment or destination.username or destination.password
                    or (destination.netloc and destination.netloc.lower() != request.url.netloc.lower())
                    or not destination.path.startswith('/cloud/dav/')):
                return JSONResponse({'error': 'invalid_destination'}, status_code=400)
            headers['destination'] = destination.path
        if admin:
            headers['X-Cloud-Admin'] = '1'
        if content is not None:
            headers['content-length'] = str(len(content))
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=3, pool=3), trust_env=False,
                                   follow_redirects=False)
        try:
            if streamed is not None:
                # Authenticate against storage before reading a single PUT byte.
                # OPTIONS performs Basic/device checks without reserving quota.
                probe = await client.options(url + '/cloud/dav/', headers={
                    k: v for k, v in headers.items() if k in ('authorization', 'X-Cloud-Internal-Key')}, timeout=5)
                if not probe.is_success:
                    await client.aclose()
                    if probe.status_code in (401, 403):
                        return JSONResponse({'error': 'authentication_required'}, status_code=probe.status_code,
                                            headers={'Cache-Control': 'no-store', 'WWW-Authenticate': 'Basic realm="Cloud JARVIS"'})
                    return JSONResponse({'error': 'cloud_unavailable'}, status_code=503)
            target = url + quote(path, safe='/')
            if request.scope.get('query_string'):
                target += '?' + request.scope['query_string'].decode('ascii')
            outgoing = client.build_request(request.method, target, headers=headers,
                                             content=content if content is not None else streamed if streamed is not None else request.stream())
            async with asyncio.timeout(DAV_UPLOAD_TIMEOUT):
                upstream = await client.send(outgoing, stream=True)
        except TimeoutError:
            await client.aclose()
            return JSONResponse({'error': 'body_timeout'}, status_code=408)
        except BodyRejected as exc:
            await client.aclose()
            return JSONResponse({'error': exc.code}, status_code=exc.status)
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse({'error': 'cloud_unavailable'}, status_code=503)
        except BaseException:
            await client.aclose()
            raise

        if path.startswith('/cloud/dav/') and upstream.status_code in (301, 302, 303, 307, 308):
            await upstream.aclose()
            await client.aclose()
            return JSONResponse({'error': 'cloud_unavailable'}, status_code=502)
        if path == '/api/cloud/v1/status' and upstream.status_code == 200 and os.environ.get('CLOUD_DAV_WRITE', '0') != '1':
            try:
                data = json.loads(await upstream.aread())
                data['webdav'] = 'read_only'
                if isinstance(data.get('features'), dict):
                    data['features']['webdav_write'] = False
            finally:
                await upstream.aclose()
                await client.aclose()
            return JSONResponse(data, headers={'Cache-Control': 'no-store'})

        async def close():
            await upstream.aclose()
            await client.aclose()

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await close()

        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() in _RESPONSE_HEADERS}
        response_headers['Cache-Control'] = 'no-store'
        return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers,
                                 background=BackgroundTask(close))

    @routes.post('/api/cloud/v1/devices', dependencies=[Depends(require_secret)])
    async def associate(request: Request):
        return await proxy(request, '/api/cloud/v1/devices', admin=True)

    @routes.delete('/api/cloud/v1/devices/{device_id}', dependencies=[Depends(require_secret)])
    async def revoke(device_id: str, request: Request):
        if not re.fullmatch('[0-9a-f]{32}', device_id):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        return await proxy(request, '/api/cloud/v1/devices/' + device_id, admin=True)

    @routes.api_route('/api/cloud/v1/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD'])
    async def api(path: str, request: Request):
        if not re.fullmatch(_ALLOWED.get(request.method, '(?!)'), path):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        if request.method == 'DELETE':
            try:
                content = await bounded_dav_body(request.scope, request.receive)
            except BodyRejected as exc:
                return JSONResponse({'error': exc.code}, status_code=exc.status)
            return await proxy(request, '/api/cloud/v1/' + path, content=content)
        return await proxy(request, '/api/cloud/v1/' + path)

    @routes.api_route('/cloud/dav/{path:path}', methods=['GET', 'HEAD', 'PROPFIND', 'OPTIONS', 'PUT', 'DELETE', 'MOVE', 'COPY', 'MKCOL', 'LOCK', 'UNLOCK', 'PROPPATCH'])
    async def dav(path: str, request: Request):
        if any(p in ('.', '..') for p in path.split('/')) or '\\' in path:
            return JSONResponse({'error': 'invalid_path'}, status_code=400)
        writable = os.environ.get('CLOUD_DAV_WRITE', '0') == '1'
        if request.method == 'PUT' and not writable:
            return JSONResponse({'error': 'read_only'}, status_code=403)
        if request.method == 'PUT' and os.environ.get('CLOUD_DAV_WRITE', '0') == '1':
            return await proxy(request, '/cloud/dav/' + path,
                streamed=bounded_dav_stream(request.scope, request.receive,
                                            limit=int(os.environ.get('CLOUD_MAX_FILE_BYTES', 5 * 1024**3))))
        try:
            content = await bounded_dav_body(request.scope, request.receive)
        except BodyRejected as exc:
            return JSONResponse({'error': exc.code}, status_code=exc.status,
                                headers={'Cache-Control': 'no-store'})
        if request.method not in ('GET', 'HEAD', 'PROPFIND', 'OPTIONS') and not writable:
            return JSONResponse({'error': 'read_only'}, status_code=403)
        return await proxy(request, '/cloud/dav/' + path, content=content)

    return routes
