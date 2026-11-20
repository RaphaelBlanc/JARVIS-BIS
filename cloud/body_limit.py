"""Bound WebDAV bodies before Starlette's WSGI adapter can collect them."""
import asyncio

from starlette.responses import JSONResponse

DAV_BODY_BYTES = 8192
DAV_BODY_TIMEOUT = 5


class BodyRejected(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


async def bounded_dav_body(scope, receive, limit=DAV_BODY_BYTES, timeout=DAV_BODY_TIMEOUT):
    headers = dict(scope.get('headers', []))
    try:
        length = int(headers.get(b'content-length', b'0'))
    except ValueError:
        raise BodyRejected('invalid_length', 400) from None
    if length < 0:
        raise BodyRejected('invalid_length', 400)
    if length > limit:
        raise BodyRejected('body_too_large', 413)
    body = bytearray()
    try:
        async with asyncio.timeout(timeout):
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    raise BodyRejected('client_disconnected', 400)
                chunk = message.get('body', b'')
                if len(body) + len(chunk) > limit:
                    raise BodyRejected('body_too_large', 413)
                body.extend(chunk)
                if not message.get('more_body', False):
                    return bytes(body)
    except TimeoutError:
        raise BodyRejected('body_timeout', 408) from None


class DavBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not (path == '/cloud/dav' or path.startswith('/cloud/dav/')):
            return await self.app(scope, receive, send)
        try:
            body = await bounded_dav_body(scope, receive)
        except BodyRejected as exc:
            return await JSONResponse({'error': exc.code}, status_code=exc.status,
                                      headers={'Cache-Control': 'no-store'})(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        return await self.app(scope, replay, send)
