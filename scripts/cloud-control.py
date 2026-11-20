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


def configure(root, rotate_key=False):
    primary = EnvFile(root / '.env')
    gateway = EnvFile(root / 'gateway/.env')
    runner = EnvFile(root / 'home-runner/.env')
    storage = EnvFile(root / 'deploy/cloud.env')
    config = primary.values | gateway.values | dict(os.environ)
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
    plan = [(storage, storage.updated({'CLOUD_INTERNAL_KEY': key})),
            (gateway, gateway.updated(changes)),
            (primary, primary.updated(remove={'CLOUD_INTERNAL_KEY'})),
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
    args = parser.parse_args()
    gateway = ROOT / 'gateway/.env'
    config = EnvFile(ROOT / '.env').values | EnvFile(gateway).values | dict(os.environ)
    if config.get('DEPLOY_MODE', 'local').strip().lower() != 'server':
        parser.error('Cloud is available only with DEPLOY_MODE=server (gateway/.env).')
    env_file = ROOT / 'deploy/cloud.env'
    if args.action == 'configure':
        rotated = configure(ROOT, args.rotate_key)
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
    command += {'up': ['up', '-d', '--build'], 'down': ['down'], 'status': ['ps']}[args.action]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print('Cloud administration failed; inspect deployment mode, env syntax and file permissions.', file=sys.stderr)
        raise SystemExit(1)
