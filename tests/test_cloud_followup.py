"""Behavior regressions for the fourteen follow-up items in Noctalia #46."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from cloud.app import create_app
from cloud.storage import CloudError, Settings, Store

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cloud_followup_helpers', ROOT / 'tests/test_cloud_security.py')
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)
service, intent, prepared, configuration = H.service, H.intent, H.prepared, H.configuration
from scripts.env_files import EnvFile, load_gateway, load_shared, sync_components


def published(store, device, data=b'hello', name='rapport.pdf', old=None):
    spec = intent(store, data, name)
    if old:
        spec.update(policy='replace', expected_revision=old['revision'])
    return store.commit(device, prepared(store, device, data, spec))


def test_directory_revisions_change_for_create_replace_and_delete(service):
    store, _, device = service
    ident = device['device_id']
    revisions = [store.entry('root')['revision']]
    store.mkdir(ident, 'root', 'dossier', revisions[-1])
    revisions.append(store.entry('root')['revision'])
    entry = published(store, ident)['result']
    revisions.append(store.entry('root')['revision'])
    entry = published(store, ident, b'new', old=entry)['result']
    revisions.append(store.entry('root')['revision'])
    store.delete_file(ident, entry['id'], '"' + entry['revision'] + '"', uuid.uuid4().hex)
    revisions.append(store.entry('root')['revision'])
    assert len(set(revisions)) == 5


def test_changed_directory_rejects_stale_mkdir_but_allows_unrelated_upload_commit(service):
    store, _, device = service
    ident = device['device_id']
    transfer = prepared(store, ident)
    revision = store.entry('root')['revision']
    store.mkdir(ident, 'root', 'autre', revision)
    with pytest.raises(CloudError, match='destination_changed'):
        store.mkdir(ident, 'root', 'encore', revision)
    assert store.commit(ident, transfer)['state'] == 'committed'


def test_transfer_limit_is_per_device(service):
    store, _, first = service
    for _ in range(8):
        store.create_upload(first['device_id'], intent(store), uuid.uuid4().hex)
    with pytest.raises(CloudError, match='too_many_transfers'):
        store.create_upload(first['device_id'], intent(store), uuid.uuid4().hex)
    second = store.associate('second')['device_id']
    assert store.create_upload(second, intent(store), uuid.uuid4().hex)['state'] == 'pending'


@pytest.mark.parametrize('value', ['bytes=' + '9' * 5000 + '-', 'bytes=-' + '9' * 5000, 'bytes=0-' + '9' * 5000, 'bytes=-0'])
def test_invalid_range_is_416(service, value):
    store, client, device = service
    entry = published(store, device['device_id'])['result']
    response = client.get('/api/cloud/v1/files/' + entry['id'] + '/content', headers={'Range': value})
    assert response.status_code == 416
    assert response.headers['Content-Range'] == 'bytes */5'


def test_delete_contract_preconditions_receipts_and_capability(service):
    store, client, device = service
    original = published(store, device['device_id'])
    entry = original['result']
    url = '/api/cloud/v1/files/' + entry['id']
    assert client.get('/api/cloud/v1/status').json()['features']['file_delete']
    headers = {'Idempotency-Key': uuid.uuid4().hex}
    assert client.delete(url, headers=headers).status_code == 428
    for revision in ('"obsolete"', '*', 'W/"' + entry['revision'] + '"', entry['revision']):
        assert client.delete(url, headers=headers | {'If-Match': revision}).status_code == 412
    headers['If-Match'] = '"' + entry['revision'] + '"'
    deleted = client.delete(url, headers=headers)
    assert deleted.status_code == 200 and deleted.json()['deleted']
    assert client.delete(url, headers=headers).json() == deleted.json()
    assert client.get(url + '/content').status_code == 404
    assert store.status(device['device_id'])['used_bytes'] == 0
    assert store.status(device['device_id'])['garbage_bytes'] == 0
    assert list(store.blobs.iterdir()) == []
    # Historical success is a receipt only, never a command to recreate content.
    assert store.commit(device['device_id'], original['upload_id']) == original
    replacement = published(store, device['device_id'])['result']
    assert replacement['id'] != entry['id']
    assert client.delete(url, headers=headers).json() == deleted.json()
    assert store.entry(replacement['id'])
    assert client.delete('/api/cloud/v1/files/' + replacement['id'], headers=headers).status_code == 409


def test_delete_invalidates_all_devices_intents_and_survives_restart(service):
    store, _, first = service
    ident = first['device_id']
    second = store.associate('second')['device_id']
    # A create intent can precede another device's publication of that name.
    transfers = [prepared(store, second)]
    original = published(store, ident)['result']
    for policy in ('replace', 'keep_both'):
        spec = intent(store) | {'policy': policy, 'expected_revision': original['revision']}
        transfers.append(prepared(store, second, spec=spec))
    store.delete_file(ident, original['id'], '"' + original['revision'] + '"', uuid.uuid4().hex)
    restarted = Store(store.settings)
    published(restarted, ident)
    for transfer in transfers:
        assert restarted.upload_status(second, transfer)['state'] == 'invalidated'
        with pytest.raises(CloudError, match='upload_invalidated'):
            restarted.commit(second, transfer)
    assert len(restarted.entries()['entries']) == 1


def test_delete_wins_against_active_assembler(service, monkeypatch):
    import cloud.storage as module
    store, _, device = service
    ident = device['device_id']
    original = published(store, ident)['result']
    transfer = prepared(store, ident, spec=intent(store) | {'policy': 'replace', 'expected_revision': original['revision']})
    entered, release = threading.Event(), threading.Event()
    sync = module.sync_dir
    def paused(path):
        if path == store.blobs and not entered.is_set():
            entered.set()  # complete assembly exists, not yet published
            assert release.wait(5)
        return sync(path)
    monkeypatch.setattr(module, 'sync_dir', paused)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(store.commit, ident, transfer)
        try:
            assert entered.wait(3)
            store.delete_file(ident, original['id'], '"' + original['revision'] + '"', uuid.uuid4().hex)
            assert list(store.parts.iterdir())  # active assembler's lease is respected
        finally:
            release.set()
        with pytest.raises(CloudError, match='upload_invalidated'):
            future.result(timeout=3)
    assert not store.entries()['entries']
    assert list(store.parts.iterdir()) == list(store.blobs.iterdir()) == []


def test_replacement_wins_against_stale_delete(service):
    store, _, device = service
    ident = device['device_id']
    original = published(store, ident)['result']
    new = published(store, ident, b'new', old=original)['result']
    with pytest.raises(CloudError, match='version_conflict'):
        store.delete_file(ident, original['id'], '"' + original['revision'] + '"', uuid.uuid4().hex)
    assert store.entry(new['id'])['revision'] == new['revision']


def test_prune_ten_versions_and_reclaim_all_on_delete(service):
    store, _, device = service
    ident = device['device_id']
    old = None
    for number in range(14):
        old = published(store, ident, str(number).encode(), old=old)['result']
    with store.connection() as db:
        versions = db.execute('SELECT blob FROM versions').fetchall()
    assert len(versions) == 10
    assert len(list(store.blobs.iterdir())) == 11
    assert {int((store.blobs / row['blob']).read_bytes()) for row in versions} == set(range(3, 13))
    store.delete_file(ident, old['id'], '"' + old['revision'] + '"', uuid.uuid4().hex)
    assert store.status(ident)['used_bytes'] == 0
    assert list(store.blobs.iterdir()) == []


def test_failed_object_gc_is_charged_retried_and_download_fd_survives(service, monkeypatch):
    store, _, device = service
    ident = device['device_id']
    old = published(store, ident)['result']
    _, source = store.open_content(old['id'])
    unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path.parent == store.blobs:
            raise PermissionError('injected GC failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', denied)
        store.delete_file(ident, old['id'], '"' + old['revision'] + '"', uuid.uuid4().hex)
        status = store.status(ident)
        assert status['used_bytes'] == 0
        assert status['garbage_bytes'] == status['reserved_bytes'] == 5
    store.collect_garbage()
    assert store.status(ident)['garbage_bytes'] == 0
    with source:
        assert source.read() == b'hello'


def test_v2_migration_does_not_reserve_historical_receipts(service):
    store, _, device = service
    entry = published(store, device['device_id'])['result']
    with store.transaction() as db:
        db.execute('UPDATE uploads SET cleanup_pending=1')
        for table in ('garbage', 'tombstones', 'deletions'):
            db.execute('DROP TABLE ' + table)
        db.execute('PRAGMA user_version=2')
    migrated = Store(store.settings)
    assert migrated.status(device['device_id'])['reserved_bytes'] == 0
    assert migrated.entry(entry['id'])['revision'] == entry['revision']


def test_cleanup_task_logs_unexpected_error_and_retries(tmp_path, monkeypatch, caplog):
    import cloud.app as module
    monkeypatch.setenv('DEPLOY_MODE', 'server')
    monkeypatch.setenv('CLOUD_ENABLED', '1')
    monkeypatch.setattr(module, 'PART_CLEANUP_INTERVAL', .01)
    app = create_app(Settings(tmp_path, chunk_size=4), 'x' * 32)
    store = app.state.store
    device = store.associate('PC')['device_id']
    transfer = prepared(store, device)
    with store.transaction() as db:
        db.execute('UPDATE uploads SET expires=0')
    collect = store.collect_expired
    calls = []
    def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('injected cleanup failure')
        collect()
    monkeypatch.setattr(store, 'collect_expired', fail_once)
    with TestClient(app):
        deadline = time.monotonic() + 3
        while list(store.parts.iterdir()) and time.monotonic() < deadline:
            time.sleep(.01)
        assert list(store.parts.iterdir()) == [] and len(calls) >= 2
    assert 'retrying next cycle' in caplog.text


def test_root_shared_settings_win_and_removed_keys_disappear(configuration, monkeypatch):
    root = configuration
    for key in ('EXAMPLE_TEST', 'REMOVED_TEST', 'DEPLOY_MODE', 'CLOUD_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    (root / '.env').write_text('DEPLOY_MODE=server\nEXAMPLE_TEST=new\nCLOUD_ENABLED=0\n')
    (root / 'gateway/.env').write_text('DEPLOY_MODE=local\nEXAMPLE_TEST=old\nREMOVED_TEST=stale\nCLOUD_ENABLED=1\n')
    (root / 'home-runner/.env').write_text('REMOVED_TEST=stale\n')
    (root / '.env').write_text('DEPLOY_MODE=server\nEXAMPLE_TEST=new\nCLOUD_ENABLED=0\nREMOVED_TEST=initial\n')
    sync_components(root)
    (root / '.env').write_text('DEPLOY_MODE=server\nEXAMPLE_TEST=new\nCLOUD_ENABLED=0\n')
    load_gateway(root)
    assert os.environ['EXAMPLE_TEST'] == 'new'
    assert 'REMOVED_TEST' not in os.environ
    assert os.environ['DEPLOY_MODE'] == 'server' and os.environ['CLOUD_ENABLED'] == '1'
    sync_components(root)
    for component in ('gateway', 'home-runner'):
        assert 'REMOVED_TEST' not in EnvFile(root / component / '.env').values
    # The explicit service environment continues to take precedence.
    monkeypatch.setenv('EXAMPLE_TEST', 'systemd')
    load_gateway(root)
    assert os.environ['EXAMPLE_TEST'] == 'systemd'


@pytest.mark.parametrize('kind', ['syntax', 'encoding', 'symlink'])
def test_start_tolerates_unrelated_env_problems(configuration, kind):
    root = configuration
    (root / 'scripts').mkdir()
    for name in ('deploy.sh', 'scripts/env_files.py'):
        shutil.copyfile(ROOT / name, root / name)
    data = b'DEPLOY_MODE=local\nVALID=yes\n'
    data += b'BROKEN="unfinished\n' if kind == 'syntax' else b'OTHER=\xff\n' if kind == 'encoding' else b''
    (root / '.env').write_bytes(data)
    if kind == 'symlink':
        (root / '.env').rename(root / 'linked.env')
        (root / '.env').symlink_to('linked.env')
    for component in ('gateway', 'home-runner'):
        binary = root / component / '.venv/bin'
        binary.mkdir(parents=True)
        (binary / 'python').symlink_to(sys.executable)
        script = binary / 'uvicorn'
        script.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(__file__).resolve().parents[2].joinpath("started").touch()\n')
        script.chmod(0o755)
    result = subprocess.run(['bash', str(root / 'deploy.sh'), 'start'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (root / 'gateway/started').exists() and (root / 'home-runner/started').exists()
    assert (root / '.env').read_bytes() == data


def test_invalid_cloud_setting_still_fails_closed(configuration):
    root = configuration
    (root / '.env').write_text('DEPLOY_MODE=server\nCLOUD_INTERNAL_KEY="unterminated\n')
    with pytest.raises(ValueError):
        sync_components(root, strict=False)


def test_cloud_configure_then_setup_sync_preserves_enabled_server(configuration):
    root = configuration
    (root / '.env').write_text('DEPLOY_MODE=local\nCLOUD_ENABLED=0\n')
    (root / 'gateway/.env').write_text('DEPLOY_MODE=server\n')
    with pytest.raises(ValueError):
        H.CONTROL.configure(root)
    (root / '.env').write_text('DEPLOY_MODE=server\nCLOUD_ENABLED=0\n')
    H.CONTROL.configure(root)
    sync_components(root)
    assert EnvFile(root / '.env').values['DEPLOY_MODE'] == 'server'
    gateway = EnvFile(root / 'gateway/.env').values
    assert gateway['DEPLOY_MODE'] == 'server' and gateway['CLOUD_ENABLED'] == '1'


def test_modified_multiline_value_round_trips(configuration):
    root = configuration
    path = root / '.env'
    path.write_text('MESSAGE=old # garder\n')
    value = 'été\nsecond "quoted" line\\path\tend'
    path.write_text(EnvFile(path).updated({'MESSAGE': value}))
    assert EnvFile(path).values['MESSAGE'] == value
    assert '\\n' not in path.read_text()


def test_dotenv_interpolation_preserved(configuration, monkeypatch):
    monkeypatch.delenv('TEST_BASE', raising=False)
    monkeypatch.delenv('TEST_EXPANDED', raising=False)
    (configuration / '.env').write_text('TEST_BASE=example\nTEST_EXPANDED=${TEST_BASE}/api\n')
    load_shared(configuration)
    assert os.environ['TEST_EXPANDED'] == 'example/api'


def test_setup_preserves_pc_agent_mode_with_default_answers(configuration, monkeypatch):
    spec = importlib.util.spec_from_file_location('cloud_setup_wizard_test', ROOT / 'setup_wizard.py')
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    (configuration / '.env').write_text('DEPLOY_MODE=pc-agent\nSECRET_KEY=test-only\n')
    monkeypatch.setattr(wizard, 'ROOT_DIR', configuration)
    monkeypatch.setattr(wizard, 'ask_string', lambda prompt, default='', **kwargs: default)
    monkeypatch.setattr(wizard, 'ask_bool', lambda *args, **kwargs: False)
    monkeypatch.setattr(wizard.ToolRegistry, 'list_tools', lambda: [])
    wizard.main()
    values = EnvFile(configuration / '.env').values
    assert values['DEPLOY_MODE'] == 'pc-agent' and values['CLOUD_ENABLED'] == '0'


def test_entry_by_id_exposes_public_metadata_and_observes_deletion(service):
    store, client, device = service
    result = published(store, device['device_id'])['result']
    root = client.get('/api/cloud/v1/entries/root')
    assert root.status_code == 200 and root.json()['entry']['kind'] == 'directory'
    response = client.get('/api/cloud/v1/entries/' + result['id'])
    assert response.status_code == 200
    body = response.json()
    assert body['server_id'] == store.server_id and body['space_id'] == 'personal'
    assert body['entry'] == result and 'blob' not in body['entry']
    assert body['entry']['size'] == 5 and len(body['entry']['sha256']) == 64
    assert client.get('/api/cloud/v1/entries/' + '0' * 32).status_code == 404
    assert client.get('/api/cloud/v1/entries/' + result['id'], headers={'Authorization': ''}).status_code == 401
    store.delete_file(device['device_id'], result['id'], '"' + result['revision'] + '"', uuid.uuid4().hex)
    assert client.get('/api/cloud/v1/entries/' + result['id']).status_code == 404


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_create_conflict_is_rejected_before_reserving_or_receiving_data(service, kind):
    store, client, device = service
    if kind == 'file':
        existing = published(store, device['device_id'], name='été.txt')['result']
    else:
        existing = store.mkdir(device['device_id'], 'root', 'été.txt', store.entry('root')['revision'])['entry']
    before = store.status(device['device_id'])
    with store.connection() as db:
        count = db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0]
    response = client.post('/api/cloud/v1/uploads', json=intent(store, name='e\u0301te\u0301.txt'),
                           headers={'Idempotency-Key': uuid.uuid4().hex})
    assert response.status_code == 412
    assert response.json()['error'] == 'version_conflict'
    assert response.json()['current'] == existing
    assert 'blob' not in response.json()['current']
    assert store.status(device['device_id']) == before
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0] == count
    assert list(store.parts.iterdir()) == []


def test_early_conflict_preserves_replay_of_successful_intent(service):
    store, _, device = service
    ident, key = device['device_id'], uuid.uuid4().hex
    spec = intent(store)
    transfer = prepared(store, ident, spec=spec, idem=key)
    result = store.commit(ident, transfer)
    assert store.create_upload(ident, spec, key) == result
    entry = result['result']
    store.delete_file(ident, entry['id'], '"' + entry['revision'] + '"', uuid.uuid4().hex)
    assert store.create_upload(ident, spec, key) == result
    assert not store.entries()['entries']


@pytest.mark.parametrize('state', ['pending', 'committed', 'cancelled', 'expired', 'invalidated'])
def test_transfer_contract_chunks_and_terminal_http_status(service, state):
    import hashlib
    store, client, device = service
    ident = device['device_id']
    transfer = prepared(store, ident)
    if state == 'committed':
        store.commit(ident, transfer)
    elif state == 'cancelled':
        store.cancel(ident, transfer)
    elif state == 'expired':
        with store.transaction() as db:
            db.execute('UPDATE uploads SET expires=0 WHERE id=?', (transfer,))
    elif state == 'invalidated':
        entry = published(store, ident)['result']
        store.delete_file(ident, entry['id'], '"' + entry['revision'] + '"', uuid.uuid4().hex)
    response = client.get('/api/cloud/v1/uploads/' + transfer)
    assert response.status_code == 200 and response.json()['state'] == state
    chunks = response.json()['chunks']
    if state == 'pending':
        assert chunks == [{'idx': 0, 'size': 4, 'sha256': hashlib.sha256(b'hell').hexdigest()},
                          {'idx': 1, 'size': 1, 'sha256': hashlib.sha256(b'o').hexdigest()}]
    else:
        assert chunks == []
    assert (response.json()['result'] is not None) == (state == 'committed')


def test_status_and_name_contract(service):
    store, client, device = service
    status = client.get('/api/cloud/v1/status').json()
    for field in ('quota_bytes', 'used_bytes', 'reserved_bytes', 'max_file_bytes', 'chunk_bytes'):
        assert type(status[field]) is int
    assert status['root']['id'] == 'root' and isinstance(status['root']['revision'], str)
    assert status['features']['entry_by_id'] and status['features']['early_create_conflict']
    # Limit is UTF-8 bytes after NFC, rather than Python/JSON character count.
    name = 'e\u0301' * 120
    accepted = store.mkdir(device['device_id'], 'root', name, store.entry('root')['revision'])
    assert accepted['entry']['name'] == 'é' * 120
    for invalid in ('é' * 121, 'a\u200db', 'a\ue000b', 'a\u0378b', 'a\ud800b'):
        with pytest.raises(CloudError, match='invalid_name'):
            store.mkdir(device['device_id'], 'root', invalid, store.entry('root')['revision'])
    import unicodedata
    decomposed = unicodedata.normalize('NFD', 'ᾄ' * 80)
    response = client.post('/api/cloud/v1/directories',
                           json={'parent_id': 'root', 'parent_revision': store.entry('root')['revision'], 'name': decomposed})
    assert response.status_code == 200 and response.json()['entry']['name'] == 'ᾄ' * 80


def test_two_devices_publish_unrelated_files_in_same_folder(service):
    store, _, first = service
    second = store.associate('second')['device_id']
    a = prepared(store, first['device_id'], spec=intent(store, name='a'))
    b = prepared(store, second, spec=intent(store, name='b'))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda pair: store.commit(*pair), [(first['device_id'], a), (second, b)]))
    assert all(r['state'] == 'committed' for r in results)
    assert len(store.entries()['entries']) == 2


def test_component_only_settings_and_unreadable_root_are_preserved(configuration, monkeypatch):
    root = configuration
    monkeypatch.delenv('COMPONENT_ONLY', raising=False)
    (root / '.env').write_text('DEPLOY_MODE=local\nTRACKED=first\n')
    (root / 'gateway/.env').write_text('COMPONENT_ONLY=private-setting\n')
    sync_components(root)
    (root / '.env').write_text('DEPLOY_MODE=local\nTRACKED="unterminated\n')
    sync_components(root, strict=False)
    values = EnvFile(root / 'gateway/.env').values
    assert values['TRACKED'] == 'first' and values['COMPONENT_ONLY'] == 'private-setting'
    load_gateway(root)
    assert os.environ['COMPONENT_ONLY'] == 'private-setting'
    # A manual override is preserved even when a previously shared key is removed.
    (root / 'gateway/.env').write_text(EnvFile(root / 'gateway/.env').updated({'TRACKED': 'manual'}))
    (root / '.env').write_text('DEPLOY_MODE=local\n')
    sync_components(root)
    assert EnvFile(root / 'gateway/.env').values['TRACKED'] == 'manual'
    assert (root / 'gateway/.env.jarvis-backup').stat().st_mode & 0o077 == 0


def test_download_open_does_not_need_sqlite_write_lock(service):
    store, _, device = service
    entry = published(store, device['device_id'])['result']
    with store.transaction():
        with ThreadPoolExecutor(1) as pool:
            metadata, source = pool.submit(store.open_content, entry['id']).result(timeout=2)
            with source:
                assert source.read() == b'hello'


def test_gc_does_not_hold_up_downloads_while_sqlite_writer_is_busy(service, monkeypatch):
    store, _, device = service
    ident = device['device_id']
    deleted = published(store, ident, name='to-delete')['result']
    retained = published(store, ident, name='to-read')['result']
    with monkeypatch.context() as patch:
        patch.setattr(store, 'collect_garbage', lambda: None)
        store.delete_file(ident, deleted['id'], '"' + deleted['revision'] + '"', uuid.uuid4().hex)
    with ThreadPoolExecutor(2) as pool:
        with store.transaction():
            pool.submit(store.collect_garbage).result(timeout=2)
            _, source = pool.submit(store.open_content, retained['id']).result(timeout=2)
            with source:
                assert source.read() == b'hello'
            with store.connection() as db:
                assert db.execute('SELECT SUM(size) FROM garbage').fetchone()[0] == 5
    # Deferred objects remain reserved until the writer releases and GC succeeds.
    assert store.status(ident)['garbage_bytes'] == 5
    store.collect_garbage()
    assert store.status(ident)['garbage_bytes'] == 0
    with store.open_content(retained['id'])[1] as source:
        assert source.read() == b'hello'


def test_one_failed_gc_object_does_not_block_others(service, monkeypatch):
    store, _, device = service
    a = published(store, device['device_id'], name='a')['result']
    b = published(store, device['device_id'], name='b')['result']
    blocked = store.entry(a['id'])['blob']
    unlink = Path.unlink
    def selective(path, *args, **kwargs):
        if path.name == blocked:
            raise PermissionError('injected single-object failure')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', selective)
    for entry in (a,b):
        store.delete_file(device['device_id'], entry['id'], '"' + entry['revision'] + '"', uuid.uuid4().hex)
    store.collect_garbage()
    assert [p.name for p in store.blobs.iterdir()] == [blocked]
    assert store.status(device['device_id'])['garbage_bytes'] == 5


def test_existing_history_prune_requires_matching_confirmation(service):
    from dataclasses import replace
    store, _, device = service
    old = published(store, device['device_id'])['result']
    published(store, device['device_id'], old=old)
    restrictive = replace(store.settings, max_versions=0)
    with pytest.raises(RuntimeError, match='confirm-history-prune'):
        Store(restrictive)
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == 1
    accepted = Store(replace(restrictive, confirmed_history_limit=0))
    with accepted.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] == 0


def test_history_confirmation_records_exact_limit(configuration, monkeypatch):
    root = configuration
    monkeypatch.delenv('CLOUD_MAX_VERSIONS', raising=False)
    (root / '.env').write_text('DEPLOY_MODE=server\n')
    (root / 'deploy/cloud.env').write_text('CLOUD_MAX_VERSIONS=0\n')
    H.CONTROL.configure(root, confirm_history_prune=True)
    assert EnvFile(root / 'deploy/cloud.env').values['CLOUD_CONFIRMED_HISTORY_LIMIT'] == '0'
    monkeypatch.setenv('CLOUD_MAX_VERSIONS', '3')
    H.CONTROL.configure(root, confirm_history_prune=True)
    assert EnvFile(root / 'deploy/cloud.env').values['CLOUD_CONFIRMED_HISTORY_LIMIT'] == '3'


@pytest.mark.parametrize('broken', [b'{', b'[]', b'null', b'{"TRACKED": 42}',
                                    b'{"TRACKED": "not-a-digest"}', b'\xff'])
def test_damaged_sync_manifest_preserves_settings_and_runtime_loading(configuration, monkeypatch, broken):
    root = configuration
    (root / '.env').write_text('DEPLOY_MODE=local\nTRACKED=keep-me\n')
    sync_components(root)
    (root / '.env').write_text('DEPLOY_MODE=local\n')
    for component in ('gateway', 'home-runner'):
        (root / component / '.env.jarvis-managed.json').write_bytes(broken)
    # Both runtime loaders used by main.py must tolerate the damaged file.
    for loader in (lambda: load_gateway(root), lambda: load_shared(root, 'home-runner')):
        monkeypatch.delenv('TRACKED', raising=False)
        with pytest.warns(UserWarning, match='Ignoring unreadable sync manifest'):
            loader()
        assert os.environ['TRACKED'] == 'keep-me'
    with pytest.warns(UserWarning, match='Ignoring unreadable sync manifest'):
        sync_components(root)
    sync_components(root)
    for component in ('gateway', 'home-runner'):
        assert EnvFile(root / component / '.env').values['TRACKED'] == 'keep-me'


@pytest.mark.parametrize('limit', ['not-a-number', '', '-1', '11'])
@pytest.mark.parametrize('source', ['file', 'environment'])
def test_invalid_history_limit_cli_is_clear_and_writes_nothing(configuration, monkeypatch, capsys, limit, source):
    root = configuration
    monkeypatch.delenv('CLOUD_MAX_VERSIONS', raising=False)
    (root / '.env').write_text('DEPLOY_MODE=server\n')
    if source == 'file':
        (root / 'deploy/cloud.env').write_text('CLOUD_MAX_VERSIONS=' + limit + '\n')
    else:
        monkeypatch.setenv('CLOUD_MAX_VERSIONS', limit)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr(H.CONTROL, 'ROOT', root)
    monkeypatch.setattr(sys, 'argv', ['cloud-control.py', 'configure', '--confirm-history-prune'])
    with pytest.raises(SystemExit) as exc:
        H.CONTROL.main()
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert 'CLOUD_MAX_VERSIONS must be an integer between 0 and 10' in error
    assert 'Traceback' not in error
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
