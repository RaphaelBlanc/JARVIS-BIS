#!/usr/bin/env python3
"""Exercise an unmodified Noctalia client against a disposable real cloud container.

Requires the gateway Python dependencies, Podman, dbus-run-session,
gnome-keyring-daemon and secret-tool. Never uses the desktop's configuration,
keyring, cloud volume or personal server. The gateway lifespan stays disabled.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def run(*command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, check=True, **kwargs).stdout.strip()


@contextmanager
def serve(app):
    import uvicorn
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        if not server.started:
            raise RuntimeError('Gateway did not start')
        yield 'http://127.0.0.1:' + str(listener.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()


class Interruptions:
    """Faults at the public boundary; every successful operation uses the real backend."""
    def __init__(self, app):
        self.app = app
        self.upload = self.download = False
        self.journal = []

    async def __call__(self, scope, receive, send):
        path, method = scope.get('path', ''), scope.get('method', '')
        headers = dict(scope.get('headers', []))
        self.journal.append((method, path, headers.get(b'range')))
        if self.upload and method == 'PUT' and path.endswith('/chunks/1'):
            self.upload = False
            while (await receive()).get('more_body'):
                pass
            await send({'type': 'http.response.start', 'status': 503,
                        'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': b'{"error":"cloud_unavailable"}'})
            return
        cutoff = self.download and method == 'GET' and path.endswith('/content')
        sent = 0
        async def response(message):
            nonlocal sent
            if cutoff and message['type'] == 'http.response.body':
                sent += len(message.get('body', b''))
                if sent > 1024 * 1024:
                    self.download = False
                    raise ConnectionResetError('Injected download interruption')
            await send(message)
        await self.app(scope, receive, response)


def exercise(args, temporary):
    import httpx
    key, admin = secrets.token_hex(32), secrets.token_hex(32)
    name = 'jarvis-cloud-client-check-' + uuid.uuid4().hex[:10]
    volume = name + '-data'
    container_environment = dict(os.environ)
    def container(*arguments, check=True):
        result = subprocess.run(['podman', '--cgroup-manager=cgroupfs', *arguments], capture_output=True, text=True,
                                env=container_environment)
        if check and result.returncode:
            raise RuntimeError('Podman failed: ' + result.stderr.strip())
        return result.stdout.strip()
    environment = dict(container_environment)
    for variable in ('GNOME_KEYRING_CONTROL', 'GNOME_KEYRING_PID', 'SSH_AUTH_SOCK'):
        environment.pop(variable, None)
    environment.update(HOME=str(temporary), XDG_CONFIG_HOME=str(temporary / 'config'),
                       XDG_STATE_HOME=str(temporary / 'state'), XDG_DATA_HOME=str(temporary / 'data'),
                       XDG_CACHE_HOME=str(temporary / 'cache'), XDG_RUNTIME_DIR=str(temporary / 'runtime'),
                       TMPDIR=str(temporary / 'tmp'), GIO_USE_PORTALS='0')
    for directory in ('runtime', 'tmp'):
        (temporary / directory).mkdir(mode=0o700)
    os.environ.clear()
    os.environ.update(environment)
    with (temporary / 'keyring.log').open('w') as log:
        keyring = subprocess.Popen(['gnome-keyring-daemon', '--foreground', '--unlock', '--components=secrets',
                                    '--control-directory', str(temporary / 'runtime/keyring')],
                                   stdin=subprocess.PIPE, stdout=log, stderr=log)
        keyring.stdin.write(secrets.token_hex(32).encode())
        keyring.stdin.close()
        try:
            # A genuine isolated Secret Service, including store/lookup/clear.
            for _ in range(50):
                probe = subprocess.run(['secret-tool', 'store', '--label=Cloud integration check',
                                        'service', name], input='probe', capture_output=True, text=True, timeout=3)
                if probe.returncode == 0:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Disposable keyring did not unlock')
            assert run('secret-tool', 'lookup', 'service', name) == 'probe'
            run('secret-tool', 'clear', 'service', name)
            envfile = temporary / 'cloud.env'
            envfile.write_text('DEPLOY_MODE=server\nCLOUD_ENABLED=1\nCLOUD_INTERNAL_KEY=' + key +
                               '\nCLOUD_CHUNK_BYTES=65536\n')
            envfile.chmod(0o600)
            container('volume', 'create', volume)
            container('run', '-d', '--name', name, '--env-file', str(envfile), '--read-only',
                '--cgroups=disabled',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--tmpfs', '/tmp:rw,size=64m',
                '-v', volume + ':/data:U', '-p', '127.0.0.1::8091', args.image)
            port = container('port', name, '8091').rsplit(':', 1)[1]
            internal = 'http://127.0.0.1:' + port
            for _ in range(100):
                try:
                    if httpx.get(internal + '/health', headers={'X-Cloud-Internal-Key': key}, timeout=.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            else:
                raise RuntimeError('Cloud container did not start')
            os.environ.update(DEPLOY_MODE='server', CLOUD_ENABLED='1', CLOUD_INTERNAL_KEY=key,
                              CLOUD_INTERNAL_URL=internal, SECRET_KEY=admin, TELEGRAM_TOKEN='', DISCORD_TOKEN='',
                              HOME_SERVER_URL='http://127.0.0.1:1', ASSISTANT_DATA_DIR=str(temporary / 'assistant'))
            sys.path[:0] = [str(args.noctalia_root / 'home/.local/share/nc-jarvis/server'),
                            str(ROOT / 'gateway'), str(ROOT)]
            import nc_gateway
            faults = Interruptions(nc_gateway.app)
            with serve(faults) as public:
                command = [sys.executable, str(args.noctalia_root / 'home/.local/bin/nc-cloud'), '--json']
                homes = {}
                for pc in ('pc-a', 'pc-b'):
                    home = temporary / pc
                    conf = home / '.config/nc/assistant.conf'
                    conf.parent.mkdir(parents=True)
                    conf.write_text('URL=' + public + '\nSECRET_KEY=' + admin + '\nUSER_ID=integration-check\nTIMEOUT=5\n')
                    conf.chmod(0o600)
                    homes[pc] = dict(environment, HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                                     XDG_STATE_HOME=str(home / '.local/state'))

                def cli(pc, *arguments, code=0):
                    result = subprocess.run(command + list(map(str, arguments)), env=homes[pc],
                                            capture_output=True, text=True, timeout=45)
                    assert result.returncode == code, (arguments, result.returncode, result.stdout, result.stderr)
                    assert admin not in result.stdout + result.stderr and key not in result.stdout + result.stderr
                    return [json.loads(line) for line in result.stdout.splitlines() if line]

                def entries():
                    return cli('pc-b', 'lister')[-1]['entries']

                cli('pc-a', 'associer', 'integration-pc-a')
                cli('pc-b', 'associer', 'integration-pc-b')
                cli('pc-a', 'verifier')
                cli('pc-a', 'etat')
                tokens = []
                for pc in homes:
                    state = Path(homes[pc]['XDG_STATE_HOME']) / 'nc-cloud'
                    assert state.stat().st_mode & 0o077 == 0
                    device = json.loads((state / 'appareil.json').read_text())['device_id']
                    token = run('secret-tool', 'lookup', 'service', 'nc-cloud', 'device', device)
                    assert len(token) >= 16
                    tokens.append(token)
                first, second = temporary / 'parallel-a.bin', temporary / 'parallel-b.bin'
                first.write_bytes(secrets.token_bytes(256 * 1024))
                second.write_bytes(secrets.token_bytes(256 * 1024))
                with ThreadPoolExecutor(2) as pool:
                    futures = [pool.submit(cli, pc, 'deposer', source)
                               for pc, source in [('pc-a', first), ('pc-b', second)]]
                    for future in futures:
                        future.result()
                assert {first.name, second.name} <= {e['name'] for e in entries()}
                cli('pc-a', 'dossier', 'root', 'été #100%?')
                source = temporary / 'été #100%?.bin'
                original = secrets.token_bytes(2 * 1024 * 1024 + 123)
                source.write_bytes(original)
                faults.upload = True
                cli('pc-a', 'deposer', source, code=3)
                manifests = list((Path(homes['pc-a']['XDG_STATE_HOME']) / 'nc-cloud/envois').glob('*.json'))
                assert len(manifests) == 1
                upload = json.loads(manifests[0].read_text())['upload_id']
                assert sum(p.endswith('/' + upload + '/chunks/0') for _, p, _ in faults.journal) == 1
                cli('pc-a', 'reprendre', '1')
                assert not manifests[0].exists()
                assert sum(p.endswith('/' + upload + '/chunks/0') for _, p, _ in faults.journal) == 1
                entry = next(e for e in entries() if e['name'] == source.name)
                destination = temporary / 'download.bin'
                faults.download = True
                cli('pc-b', 'telecharger', entry['id'], destination, code=3)
                assert not destination.exists()
                cli('pc-b', 'telecharger', entry['id'], destination)
                assert destination.read_bytes() == original
                assert any(method == 'GET' and path.endswith('/content') and byte_range
                           for method, path, byte_range in faults.journal)
                assert any(path.endswith('/entries/' + entry['id']) for _, path, _ in faults.journal)
                cli('pc-a', 'deposer', source, code=6)
                cli('pc-a', 'deposer', '--si-existe', 'garder', source)
                source.write_bytes(b'replacement')
                cli('pc-a', 'deposer', '--si-existe', 'remplacer:' + entry['revision'], source)
                cli('pc-b', 'supprimer', entry['id'], '--revision', entry['revision'], code=6)
                current = next(e for e in entries() if e['id'] == entry['id'])
                cli('pc-b', 'supprimer', current['id'], '--revision', current['revision'])
                assert all(e['id'] != current['id'] for e in entries())
                assert destination.read_bytes() == original and source.read_bytes() == b'replacement'
                os.environ['DEPLOY_MODE'] = 'local'
                cli('pc-a', 'deposer', source, code=5)
                os.environ['DEPLOY_MODE'] = 'server'
                container('stop', '-t', '1', name)
                before = list((Path(homes['pc-a']['XDG_STATE_HOME']) / 'nc-cloud/envois').glob('*.json'))
                cli('pc-a', 'deposer', source, code=3)
                assert list((Path(homes['pc-a']['XDG_STATE_HOME']) / 'nc-cloud/envois').glob('*.json')) == before
                container('start', name)
                for _ in range(100):
                    try:
                        if httpx.get(internal + '/health', headers={'X-Cloud-Internal-Key': key}, timeout=.5).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.05)
                cli('pc-a', 'etat')
                for pc in homes:
                    state = Path(homes[pc]['XDG_STATE_HOME']) / 'nc-cloud'
                    for path in state.rglob('*.json'):
                        assert path.stat().st_mode & 0o077 == 0
                        assert all(token not in path.read_text() for token in tokens)
                        assert admin not in path.read_text() and key not in path.read_text()
                cli('pc-a', 'dissocier')
                cli('pc-b', 'dissocier')
                cli('pc-a', 'etat', code=4)
            print('PASS: official nc-cloud + real nc_gateway + container + isolated real keyring')
            print('PASS: two devices, Unicode, upload resume without resending chunk 0, Range download resume,')
            print('      concurrent PCs, conflict, keep both, CAS replacement/deletion, disabled local mode,')
            print('      offline without queue, restart, private state without secrets, unpair')
            print('Container image: ' + container('image', 'inspect', args.image, '--format', '{{.Id}}'))
        finally:
            container('rm', '-f', name, check=False)
            container('volume', 'rm', '-f', volume, check=False)
            keyring.terminate()
            try:
                keyring.wait(timeout=5)
            except subprocess.TimeoutExpired:
                keyring.kill()
                keyring.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--noctalia-root', type=Path, required=True)
    parser.add_argument('--image', default='localhost/jarvis-cloud:review')
    parser.add_argument('--isolated-session', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.noctalia_root = args.noctalia_root.resolve()
    if not (args.noctalia_root / 'home/.local/bin/nc-cloud').is_file():
        parser.error('--noctalia-root must contain the official nc-cloud client')
    if not args.isolated_session:
        return subprocess.run(['dbus-run-session', '--', sys.executable, str(Path(__file__).resolve()),
                               *sys.argv[1:], '--isolated-session']).returncode
    with tempfile.TemporaryDirectory(prefix='jarvis-cloud-client-check-') as temporary:
        exercise(args, Path(temporary))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
