"""Read-only WebDAV view of the transactional namespace.

Mutations intentionally go through the versioned upload API in v1. WsgiDAV
FilesystemProvider must never be used: it would bypass publication checks.
"""
import base64
import mimetypes

from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from cloud.storage import CloudError


class Provider(DAVProvider):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def is_readonly(self):
        return True

    def get_resource_inst(self, path, environ):
        try:
            entry = self.store.resolve(path)
        except CloudError as exc:
            if exc.status == 404:
                return None
            raise
        cls = Directory if entry['kind'] == 'directory' else File
        return cls(path, environ, entry)


class Directory(DAVCollection):
    def __init__(self, path, environ, entry):
        super().__init__(path, environ)
        self.entry = entry

    def get_member_names(self):
        with self.provider.store.connection() as db:
            return [r[0] for r in db.execute('SELECT name FROM entries WHERE parent=? ORDER BY name', (self.entry['id'],))]

    def get_display_name(self):
        return self.entry['name'] or 'Cloud JARVIS'

    def get_last_modified(self):
        return self.entry['modified']


class File(DAVNonCollection):
    def __init__(self, path, environ, entry):
        super().__init__(path, environ)
        self.entry = entry

    def get_content_length(self):
        return self.entry['size']

    def get_content_type(self):
        return mimetypes.guess_type(self.entry['name'])[0] or 'application/octet-stream'

    def get_content(self):
        return (self.provider.store.blobs / self.entry['blob']).open('rb')

    def get_etag(self):
        return self.entry['revision']

    def support_etag(self):
        return True

    def support_ranges(self):
        return True

    def get_last_modified(self):
        return self.entry['modified']


def application(store):
    dav = WsgiDAVApp({'provider_mapping': {'/': Provider(store)}, 'mount_path': '/cloud/dav',
                      'http_authenticator': {'domain_controller': None},
                      'simple_dc': {'user_mapping': {'*': True}},
                      'verbose': 0, 'logging': {'enable': False},
                      'dir_browser': {'enable': False}, 'property_manager': False,
                      'lock_storage': False})

    def authenticated(environ, start_response):
        try:
            kind, value = environ.get('HTTP_AUTHORIZATION', '').split(' ', 1)
            if kind.lower() != 'basic':
                raise ValueError
            device, token = base64.b64decode(value, validate=True).decode().split(':', 1)
            if store.authenticate(token) != device:
                raise CloudError('authentication_required', 401)
        except (CloudError, ValueError, UnicodeError):
            start_response('401 Unauthorized', [('WWW-Authenticate', 'Basic realm="Cloud JARVIS"'),
                                              ('Content-Type', 'text/plain'), ('Cache-Control', 'no-store')])
            return [b'Cloud authentication required']
        if environ['REQUEST_METHOD'] not in ('OPTIONS', 'PROPFIND', 'GET', 'HEAD'):
            start_response('403 Forbidden', [('Content-Type', 'text/plain'), ('Cache-Control', 'no-store')])
            return [b'Read-only WebDAV. Use the Cloud JARVIS upload window.']
        return dav(environ, start_response)

    return authenticated
