"""Optional cloud routes: whitelist requests and stream without AI or body buffering."""
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


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
        result.update(state='ready', server_id=data['server_id'])
    except (httpx.HTTPError, ValueError, KeyError):
        result['reason'] = 'cloud_unavailable'
    return result


_ALLOWED = {
    'GET': r'(?:status|entries|files/[0-9a-f]{32}/content|uploads/[0-9a-f]{32})',
    'POST': r'(?:directories|uploads|uploads/[0-9a-f]{32}/commit)',
    'PUT': r'uploads/[0-9a-f]{32}/chunks/[0-9]{1,10}',
    'DELETE': r'uploads/[0-9a-f]{32}',
}
_HEADERS = {'authorization', 'content-type', 'content-length', 'idempotency-key', 'x-content-sha256',
            'range', 'if-range', 'if-match', 'if-none-match', 'depth', 'translate'}
_RESPONSE_HEADERS = {'content-type', 'content-length', 'content-range', 'accept-ranges', 'etag',
                     'last-modified', 'content-disposition', 'www-authenticate', 'dav', 'allow', 'retry-after'}


def router(require_secret):
    routes = APIRouter()

    @routes.get('/api/cloud/v1/capabilities', dependencies=[Depends(require_secret)])
    async def cloud_capabilities():
        return await capability()

    async def proxy(request, admin=False):
        _, enabled, url, key = configuration()
        if not enabled:
            return JSONResponse({'error': 'cloud_disabled'}, status_code=403)
        if len(key) < 32:
            return JSONResponse({'error': 'cloud_unavailable'}, status_code=503)
        headers = {k: v for k, v in request.headers.items() if k.lower() in _HEADERS}
        headers['X-Cloud-Internal-Key'] = key
        if admin:
            headers['X-Cloud-Admin'] = '1'
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=3, pool=3), trust_env=False,
                                   follow_redirects=False)
        try:
            target = url + request.url.path
            if request.url.query:
                target += '?' + request.url.query
            outgoing = client.build_request(request.method, target, headers=headers, content=request.stream())
            upstream = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse({'error': 'cloud_unavailable'}, status_code=503)

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
        return await proxy(request, admin=True)

    @routes.delete('/api/cloud/v1/devices/{device_id}', dependencies=[Depends(require_secret)])
    async def revoke(device_id: str, request: Request):
        if not re.fullmatch('[0-9a-f]{32}', device_id):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        return await proxy(request, admin=True)

    @routes.api_route('/api/cloud/v1/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD'])
    async def api(path: str, request: Request):
        if not re.fullmatch(_ALLOWED.get(request.method, '(?!)'), path):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        return await proxy(request)

    @routes.api_route('/cloud/dav/{path:path}', methods=['GET', 'HEAD', 'PROPFIND', 'OPTIONS', 'PUT', 'DELETE', 'MOVE', 'COPY', 'MKCOL', 'LOCK', 'UNLOCK', 'PROPPATCH'])
    async def dav(path: str, request: Request):
        if any(p in ('.', '..') for p in path.split('/')) or '\\' in path:
            return JSONResponse({'error': 'invalid_path'}, status_code=400)
        return await proxy(request)

    return routes
