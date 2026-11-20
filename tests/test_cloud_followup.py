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


def test_changed_directory_rejects_stale_mkdir_and_upload_commit(service):
    store, _, device = service
    ident = device['device_id']
    transfer = prepared(store, ident)
    revision = store.entry('root')['revision']
    store.mkdir(ident, 'root', 'autre', revision)
    with pytest.raises(CloudError, match='destination_changed'):
        store.mkdir(ident, 'root', 'encore', revision)
    with pytest.raises(CloudError, match='destination_changed'):
        store.commit(ident, transfer)


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
    original = published(store, ident)['result']
    second = store.associate('second')['device_id']
    transfers = []
    for policy in ('create', 'replace', 'keep_both'):
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
