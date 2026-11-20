#!/usr/bin/env python3
"""Explicit, local server administration. Never called by the AI or nc-maj."""
import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

from env_files import EnvFile, private_write

ROOT = Path(__file__).resolve().parents[1]

COMPOSE_DEFAULTS = {
    'CLOUD_DAV_WRITE': '0',
    'CLOUD_QUOTA_BYTES': '21474836480',
    'CLOUD_MAX_FILE_BYTES': '5368709120',
    'CLOUD_CHUNK_BYTES': '4194304',
    'CLOUD_UPLOAD_TTL': '86400',
    'CLOUD_MAX_UPLOADS': '8',
    'CLOUD_CONFIRMED_HISTORY_LIMIT': '-1',
    'CLOUD_MAX_VERSIONS': '10',
}


def compose_environment(env_file):
    # podman-compose 1.3 normalizes environment into a mapping before expanding
    # it and otherwise treats unset self-referencing defaults as literal values.
    values = EnvFile(env_file).values
    environment = dict(os.environ)
    key = environment.get('CLOUD_INTERNAL_KEY', values.get('CLOUD_INTERNAL_KEY', ''))
    if len(key) < 32 or not key.isascii():
        raise ValueError('CLOUD_INTERNAL_KEY must contain at least 32 ASCII characters')
    environment['CLOUD_INTERNAL_KEY'] = key
    for name, default in COMPOSE_DEFAULTS.items():
        environment.setdefault(name, values.get(name, default))
    return environment


def configure(root, rotate_key=False, confirm_history_prune=False, dav_write=None):
    primary = EnvFile(root / '.env')
    gateway = EnvFile(root / 'gateway/.env')
    runner = EnvFile(root / 'home-runner/.env')
    storage = EnvFile(root / 'deploy/cloud.env')
    config = gateway.values | {k: v for k, v in primary.values.items() if not k.startswith('CLOUD_')} | dict(os.environ)
    if config.get('DEPLOY_MODE', 'local').strip().lower() != 'server':
        raise ValueError('Cloud is available only with DEPLOY_MODE=server (gateway/.env).')
    leaked = 'CLOUD_INTERNAL_KEY' in primary.values or 'CLOUD_INTERNAL_KEY' in runner.values
    key = (secrets.token_hex(32) if rotate_key or leaked else
           gateway.values.get('CLOUD_INTERNAL_KEY') or storage.values.get('CLOUD_INTERNAL_KEY')
           or os.environ.get('CLOUD_INTERNAL_KEY') or secrets.token_hex(32))
    if len(key) < 32 or not key.isascii():
        raise ValueError('CLOUD_INTERNAL_KEY must contain at least 32 ASCII characters')
    changes = dict(DEPLOY_MODE='server', CLOUD_ENABLED='1', CLOUD_INTERNAL_KEY=key)
    if not gateway.values.get('CLOUD_INTERNAL_URL'):
        changes['CLOUD_INTERNAL_URL'] = config.get('CLOUD_INTERNAL_URL') or 'http://127.0.0.1:8091'
    # Parse/render every document before touching any of them. Never rewrite other bindings.
    storage_changes = {'CLOUD_INTERNAL_KEY': key}
    if dav_write is not None:
        changes['CLOUD_DAV_WRITE'] = storage_changes['CLOUD_DAV_WRITE'] = '1' if dav_write else '0'
    if confirm_history_prune:
        limit = os.environ.get('CLOUD_MAX_VERSIONS', storage.values.get('CLOUD_MAX_VERSIONS', '10'))
        try:
            limit = int(limit)
        except ValueError:
            raise ValueError('CLOUD_MAX_VERSIONS must be an integer between 0 and 10; history pruning was not confirmed') from None
        if not 0 <= limit <= 10:
            raise ValueError('CLOUD_MAX_VERSIONS must be an integer between 0 and 10; history pruning was not confirmed')
        storage_changes['CLOUD_CONFIRMED_HISTORY_LIMIT'] = str(limit)
    plan = [(storage, storage.updated(storage_changes)),
            (gateway, gateway.updated(changes)),
            (primary, primary.updated({'DEPLOY_MODE': 'server'} if primary.path.exists() else {}, remove={'CLOUD_INTERNAL_KEY'})),
            (runner, runner.updated(remove={'CLOUD_INTERNAL_KEY'}))]
    for doc, text in plan:
        if text != doc.text or (doc in (storage, gateway) and not doc.path.exists()):
            private_write(doc.path, text)
    for doc in (storage, gateway):
        doc.path.chmod(0o600)
    return leaked or rotate_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'up', 'down', 'status'])
    parser.add_argument('--rotate-key', action='store_true', help='Rotate the internal key (configure only)')
    parser.add_argument('--confirm-history-prune', action='store_true', help='Approve startup pruning to the configured history limit after backup')
    dav = parser.add_mutually_exclusive_group()
    dav.add_argument('--dav-write', dest='dav_write', action='store_true', default=None, help='Enable transactional WebDAV writes (configure only)')
    dav.add_argument('--dav-read-only', dest='dav_write', action='store_false', help='Restore read-only WebDAV (configure only)')
    args = parser.parse_args()
    if args.confirm_history_prune and args.action != 'configure':
        parser.error('--confirm-history-prune applies to configure only')
    if args.dav_write is not None and args.action != 'configure':
        parser.error('--dav-write/--dav-read-only apply to configure only')
    gateway = ROOT / 'gateway/.env'
    config = EnvFile(gateway).values | {k: v for k, v in EnvFile(ROOT / '.env').values.items() if not k.startswith('CLOUD_')} | dict(os.environ)
    if config.get('DEPLOY_MODE', 'local').strip().lower() != 'server':
        parser.error('Cloud is available only with DEPLOY_MODE=server (gateway/.env).')
    env_file = ROOT / 'deploy/cloud.env'
    if args.action == 'configure':
        try:
            rotated = configure(ROOT, args.rotate_key, args.confirm_history_prune, args.dav_write)
        except ValueError as exc:
            parser.error(str(exc))
        if rotated:
            print('Internal key rotated; recreate storage and restart the gateway together.')
        print('Cloud configured. Run ./deploy.sh cloud up, then restart the gateway. Data is preserved by down.')
        return 0
    if not env_file.exists():
        parser.error('Run ./deploy.sh cloud configure first.')
    if shutil.which('docker'):
        base = ['docker', 'compose']
    elif shutil.which('podman-compose'):
        base = ['podman-compose']
    else:
        parser.error('Install Docker Compose or podman-compose on the server.')
    command = base + ['--env-file', str(env_file), '-f', str(ROOT / 'deploy/cloud.compose.yaml')]
    command += {'up': ['up', '-d', '--build', '--force-recreate'], 'down': ['down'], 'status': ['ps']}[args.action]
    return subprocess.run(command, cwd=ROOT, env=compose_environment(env_file), check=False).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print('Cloud administration failed; inspect deployment mode, env syntax and file permissions.', file=sys.stderr)
        raise SystemExit(1)
