#!/usr/bin/env python3
"""Explicit, local server administration. Never called by the AI or nc-maj."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                value = value.strip()
                if value.startswith('"'):
                    value = json.loads(value)
                values[key.strip()] = value.strip("'")
    return values


def private_write(path, values):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Refusing symlink: ' + str(path))
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        json_lines = ''.join(k + '=' + json.dumps(v) + '\n' for k, v in values.items())
        f.write(json_lines)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'up', 'down', 'status'])
    args = parser.parse_args()
    gateway = ROOT / 'gateway/.env'
    config = read_env(ROOT / '.env') | read_env(gateway) | dict(os.environ)
    if config.get('DEPLOY_MODE', 'local').strip().lower() != 'server':
        parser.error('Cloud is available only with DEPLOY_MODE=server (gateway/.env).')
    env_file = ROOT / 'deploy/cloud.env'
    if args.action == 'configure':
        values = read_env(env_file)
        key = config.get('CLOUD_INTERNAL_KEY') or values.get('CLOUD_INTERNAL_KEY') or secrets.token_hex(32)
        if len(key) < 32:
            parser.error('CLOUD_INTERNAL_KEY is too short')
        values.update(CLOUD_INTERNAL_KEY=key)
        private_write(env_file, values)
        for path in [ROOT / '.env', gateway]:
            current = read_env(path)
            current.update(DEPLOY_MODE='server', CLOUD_ENABLED='1', CLOUD_INTERNAL_KEY=key,
                           CLOUD_INTERNAL_URL='http://127.0.0.1:8091')
            private_write(path, current)
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
    except (OSError, ValueError) as exc:
        print('Cloud administration failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
