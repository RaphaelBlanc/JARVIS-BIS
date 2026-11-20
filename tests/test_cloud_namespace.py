"""Namespace operations: concurrent PCs, safe recursive delete and persistence."""
import hashlib
import importlib.util
from pathlib import Path
import uuid

import pytest
from cloud.storage import CloudError, Store

SPEC = importlib.util.spec_from_file_location('namespace_helpers', Path(__file__).with_name('test_cloud.py'))
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)
service = H.service


def intent(store, data=b"hello", name="rapport.pdf", **extra):
    return H.intent(store, data, name) | extra


def directory(store, device, name, parent='root'):
    return store.mkdir(device, parent, name, store.entry(parent)['revision'])['entry']


def publish(store, device, name, data=b'hello', parent='root', previous=None):
    spec = intent(store, data, name, parent_id=parent, parent_revision=store.entry(parent)['revision'])
    if previous:
        spec.update(policy='replace', expected_revision=previous['revision'])
    return store.commit(device, H.prepared(store, device, data, spec))['result']


def test_move_preserves_content_id_history_and_receipt(service):
    store, client, device = service
    ident = device['device_id']
    folder = directory(store, ident, 'été #100%?')
    first = publish(store, ident, 'fichier.txt')
    entry = publish(store, ident, 'fichier.txt', b'new', previous=first)
    url = '/api/cloud/v1/entries/' + entry['id'] + '/move'
    body = {'parent_id': folder['id'], 'parent_revision': folder['revision'], 'name': 'renommé.txt'}
    headers = {'If-Match': '"' + entry['revision'] + '"', 'Idempotency-Key': uuid.uuid4().hex}
    result = client.post(url, json=body, headers=headers)
    assert result.status_code == 200
    moved = result.json()['entry']
    assert moved['id'] == entry['id'] and moved['revision'] != entry['revision']
    assert store.resolve('été #100%?/renommé.txt')['id'] == entry['id']
    assert client.get('/api/cloud/v1/files/' + entry['id'] + '/content').content == b'new'
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM versions WHERE file_id=?', (entry['id'],)).fetchone()[0] == 1
    assert client.post(url, json=body, headers=headers).json() == result.json()
    restarted = Store(store.settings)
    assert restarted.move_entry(ident, entry['id'], headers['If-Match'], body['parent_id'], body['parent_revision'], body['name'], headers['Idempotency-Key']) == result.json()
    assert client.post(url, json=body | {'name': 'other'}, headers=headers).status_code == 409


def test_move_rejects_stale_source_target_collision_cycle_and_root(service):
    store, _, device = service
    ident = device['device_id']
    a = directory(store, ident, 'a')
    b = directory(store, ident, 'b', a['id'])
    def move(entry, parent, revision=None, name=None, target_revision=None):
        return store.move_entry(ident, entry['id'], revision or '"' + store.entry(entry['id'])['revision'] + '"',
                                parent, target_revision or store.entry(parent)['revision'], name or entry['name'], uuid.uuid4().hex)
    with pytest.raises(CloudError, match='invalid_destination'):
        move(a, b['id'])
    with pytest.raises(CloudError, match='invalid_destination'):
        move(a, a['id'])
    with pytest.raises(CloudError, match='root_protected'):
        move(store.entry('root'), b['id'], name='root')
    f = publish(store, ident, 'f.txt')
    with pytest.raises(CloudError, match='version_conflict'):
        move(f, a['id'], revision='"stale"')
    with pytest.raises(CloudError, match='destination_changed'):
        move(f, a['id'], target_revision='stale')
    publish(store, ident, 'occupied', parent=a['id'])
    with pytest.raises(CloudError, match='name_conflict'):
        move(f, a['id'], name='occupied')
    assert store.entry(f['id'])['parent'] == 'root'


def test_recursive_delete_detects_deep_change_and_invalidates_all_pc_uploads(service):
    store, client, device = service
    ident = device['device_id']
    other = store.associate('other')['device_id']
    top = directory(store, ident, 'top')
    nested = directory(store, ident, 'nested', top['id'])
    first = publish(store, ident, 'file', parent=nested['id'])
    preview = store.delete_preview(ident, top['id'])
    publish(store, other, 'file', b'changed', parent=nested['id'], previous=first)
    # The top directory itself was not touched, but its confirmed subtree changed.
    assert store.entry(top['id'])['revision'] == preview['entry']['revision']
    with pytest.raises(CloudError, match='tree_changed'):
        store.delete_entry(ident, top['id'], '"' + preview['entry']['revision'] + '"', uuid.uuid4().hex,
                           True, preview['tree_revision'])
    with pytest.raises(CloudError, match='directory_not_empty'):
        store.delete_entry(ident, top['id'], '"' + preview['entry']['revision'] + '"', uuid.uuid4().hex)
    pending = []
    for pc in (ident, other):
        spec = intent(store, b'part', 'pending', parent_id=nested['id'], parent_revision=store.entry(nested['id'])['revision'])
        pending.append((pc, H.prepared(store, pc, b'part', spec)))
    snapshot = store.delete_preview(ident, top['id'])
    assert snapshot['files'] == 1 and snapshot['directories'] == 2
    headers = {'If-Match': '"' + snapshot['entry']['revision'] + '"', 'Idempotency-Key': uuid.uuid4().hex}
    body = {'recursive': True, 'tree_revision': snapshot['tree_revision']}
    url = '/api/cloud/v1/entries/' + top['id']
    result = client.request('DELETE', url, headers=headers, json=body)
    assert result.status_code == 200 and result.json()['count'] == 3
    assert client.request('DELETE', url, headers=headers, json=body).json() == result.json()
    for pc, upload in pending:
        assert store.upload_status(pc, upload)['state'] == 'invalidated'
        with pytest.raises(CloudError, match='upload_invalidated'):
            store.commit(pc, upload)
    assert store.status(ident)['used_bytes'] == 0 and store.status(ident)['reserved_bytes'] == 0
    assert not list(store.parts.iterdir())
    assert client.get('/api/cloud/v1/entries/' + first['id']).status_code == 404


def test_move_invalidates_intent_for_old_name_without_recreating_it(service):
    store, _, device = service
    ident = device['device_id']
    f = publish(store, ident, 'old')
    pending = H.prepared(store, ident, b'hello', intent(store, b'hello', 'old', policy='keep_both'))
    target = directory(store, ident, 'target')
    store.move_entry(ident, f['id'], '"' + f['revision'] + '"', target['id'], target['revision'], 'new', uuid.uuid4().hex)
    with pytest.raises(CloudError, match='upload_invalidated'):
        store.commit(ident, pending)
    assert store.resolve('target/new')['id'] == f['id']
    with pytest.raises(CloudError, match='not_found'):
        store.resolve('old')


def test_move_directory_keeps_children_and_active_uploads_inside_it(service):
    store, _, device = service
    ident = device['device_id']
    folder = directory(store, ident, 'folder')
    child = publish(store, ident, 'child', parent=folder['id'])
    pending = H.prepared(store, ident, b'hello', intent(store, b'hello', 'new', parent_id=folder['id'], parent_revision=store.entry(folder['id'])['revision']))
    target = directory(store, ident, 'target')
    store.move_entry(ident, folder['id'], '"' + store.entry(folder['id'])['revision'] + '"', target['id'], target['revision'], 'moved', uuid.uuid4().hex)
    assert store.resolve('target/moved/child')['id'] == child['id']
    assert store.commit(ident, pending)['state'] == 'committed'


def test_new_routes_require_tokens_and_exact_revision(service):
    store, client, device = service
    f = publish(store, device['device_id'], 'file')
    path = '/api/cloud/v1/entries/' + f['id']
    assert client.get(path + '/delete-preview', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    headers = {'Idempotency-Key': uuid.uuid4().hex}
    body = {'parent_id': 'root', 'parent_revision': store.entry('root')['revision'], 'name': 'renamed'}
    assert client.post(path + '/move', json=body, headers=headers).status_code == 428
    assert client.request('DELETE', path, json={}, headers=headers).status_code == 428
    for value in ('*', 'W/"' + f['revision'] + '"', f['revision']):
        assert client.request('DELETE', path, json={}, headers=headers | {'If-Match': value}).status_code == 412
