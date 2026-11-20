"""Exercise real HTTP streaming through the public gateway to the internal service."""
from contextlib import contextmanager
import hashlib
import importlib.util
from pathlib import Path
import socket
import threading
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest
import uvicorn

from cloud.app import create_app
from cloud.storage import Settings

SPEC = importlib.util.spec_from_file_location('cloud_proxy_http_test', Path(__file__).resolve().parents[1] / 'gateway/cloud_proxy.py')
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


@contextmanager
def serve(app):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    if not server.started:
        raise RuntimeError('Test server did not start')
    try:
        yield 'http://127.0.0.1:' + str(listener.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    monkeypatch.setenv('CLOUD_INTERNAL_KEY', 'a' * 32)
    internal = create_app(Settings(tmp_path, chunk_size=4))
    def admin(x_secret_key: str = Header(default='')):
        if x_secret_key != 'admin-only':
            raise HTTPException(401)
    public = FastAPI()
    public.include_router(PROXY.router(admin))
    with serve(internal) as url:
        monkeypatch.setenv('CLOUD_INTERNAL_URL', url)
        yield TestClient(public), internal.state.store


def test_proxy_round_trip_dav_and_forged_admin(gateway):
    client, store = gateway
    admin = {'X-Secret-Key': 'admin-only'}
    capability = client.get('/api/cloud/v1/capabilities', headers=admin)
    assert capability.json()['state'] == 'ready'
    assert capability.json()['server_id'] == store.server_id
    assert client.post('/api/cloud/v1/devices', json={'name': 'bad'}, headers={'X-Cloud-Admin': '1'}).status_code == 401
    associated = client.post('/api/cloud/v1/devices', json={'name': 'portable'}, headers=admin).json()
    auth = {'Authorization': 'Bearer ' + associated['token']}
    status = client.get('/api/cloud/v1/status', headers=auth).json()
    assert status['server_id'] == store.server_id
    data = b'bonjour'
    spec = dict(parent_id='root', parent_revision=status['root']['revision'], name='été.txt',
                size=len(data), sha256=hashlib.sha256(data).hexdigest())
    created = client.post('/api/cloud/v1/uploads', json=spec, headers=auth | {'Idempotency-Key': uuid.uuid4().hex})
    assert created.status_code == 200, created.text
    transfer = created.json()['upload_id']
    for index, chunk in enumerate([data[:4], data[4:]]):
        result = client.put(f'/api/cloud/v1/uploads/{transfer}/chunks/{index}', content=chunk,
                            headers=auth | {'X-Content-SHA256': hashlib.sha256(chunk).hexdigest()})
        assert result.status_code == 200, result.text
    published = client.post(f'/api/cloud/v1/uploads/{transfer}/commit', headers=auth)
    assert published.status_code == 200, published.text
    entry = published.json()['result']
    download = client.get('/api/cloud/v1/files/' + entry['id'] + '/content', headers=auth | {'Range': 'bytes=2-4'})
    assert download.status_code == 206 and download.content == b'njo'
    dav = client.get('/cloud/dav/%C3%A9t%C3%A9.txt', auth=(associated['device_id'], associated['token']))
    assert dav.status_code == 200 and dav.content == data
    assert client.request('PROPFIND', '/cloud/dav/', auth=(associated['device_id'], associated['token']), headers={'Depth': '1'}).status_code == 207
    assert client.delete('/api/cloud/v1/devices/' + associated['device_id'], headers=auth).status_code == 401
    assert client.delete('/api/cloud/v1/devices/' + associated['device_id'], headers=admin).status_code == 200
    assert client.get('/api/cloud/v1/status', headers=auth).status_code == 403


def test_proxy_does_not_leak_internal_identity_headers(gateway):
    client, store = gateway
    response = client.get('/api/cloud/v1/status', headers={'X-Cloud-Admin': '1', 'X-Cloud-Internal-Key': 'a' * 32})
    assert response.status_code == 401
    assert client.post('/api/cloud/v1/uploads/../devices', json={'name': 'bad'}).status_code == 401
    assert client.post('/api/cloud/v1/%252e%252e/devices', json={'name': 'bad'}).status_code == 404


def test_validated_target_ignores_host_path_and_preserves_dav_names(gateway):
    client, store = gateway
    device = store.associate('PC')
    content = b'content'
    name = 'été #100%?.txt'
    ident = device['device_id']
    spec = dict(parent_id='root', parent_revision=store.entry('root')['revision'], name=name,
                size=len(content), sha256=hashlib.sha256(content).hexdigest(), policy='create')
    upload = store.create_upload(ident, spec, uuid.uuid4().hex)['upload_id']
    for index, offset in enumerate(range(0, len(content), 4)):
        data = content[offset:offset + 4]
        store.chunk(ident, upload, index, data, hashlib.sha256(data).hexdigest())
    entry = store.commit(ident, upload)['result']
    # Starlette's request.url.path can incorporate this Host suffix. Route
    # parameters and the internal target must remain independent of it.
    hostile = {'Host': 'evil.invalid/api/cloud/v1/devices', 'Authorization': 'Bearer ' + device['token']}
    response = client.get('/api/cloud/v1/status', headers=hostile)
    assert response.status_code == 200 and response.json()['device_id'] == ident
    from urllib.parse import quote
    dav = client.get('/cloud/dav/' + quote(name, safe=''), auth=(ident, device['token']),
                     headers={'Host': 'evil.invalid/elsewhere'})
    assert dav.status_code == 200 and dav.content == content
    capability = client.get('/api/cloud/v1/capabilities', headers={'X-Secret-Key': 'admin-only'}).json()
    assert capability['features']['file_delete']
    headers = {'Authorization': 'Bearer ' + device['token'], 'If-Match': '"' + entry['revision'] + '"',
               'Idempotency-Key': uuid.uuid4().hex}
    assert client.delete('/api/cloud/v1/files/' + entry['id'], headers=headers).status_code == 200


def test_proxy_disabled_and_unavailable(gateway, monkeypatch):
    client, _ = gateway
    monkeypatch.setenv('CLOUD_INTERNAL_URL', 'http://127.0.0.1:1')
    assert client.get('/api/cloud/v1/status').status_code == 503
    cap = client.get('/api/cloud/v1/capabilities', headers={'X-Secret-Key': 'admin-only'}).json()
    assert cap['enabled'] and cap['state'] == 'unavailable'
    monkeypatch.setenv('DEPLOY_MODE', 'local')
    assert client.get('/api/cloud/v1/status').status_code == 403


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'OPTIONS', 'PROPFIND', 'DELETE', 'COPY', 'MOVE', 'LOCK', 'UNLOCK', 'MKCOL', 'PROPPATCH'])
def test_gateway_bounds_dav_body_before_forwarding(gateway, monkeypatch, method):
    client, _ = gateway
    # An unreachable upstream proves rejection occurs in the gateway itself, without Basic Auth.
    monkeypatch.setenv('CLOUD_INTERNAL_URL', 'http://127.0.0.1:1')
    assert client.request(method, '/cloud/dav/', content=b'x' * 8193).status_code == 413
    assert client.request(method, '/cloud/dav/', content=iter([b'x' * 4096, b'x' * 4096, b'x'])).status_code == 413
