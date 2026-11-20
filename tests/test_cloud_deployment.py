"""Regressions found during the Debian server deployment."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('cloud_deployment_control', ROOT / 'scripts/cloud-control.py')
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


class TestStandaloneRunner(unittest.TestCase):
    def test_import_without_repository_on_pythonpath(self):
        # Start like the systemd unit: cwd=home-runner, no root PYTHONPATH.
        # Copy code to avoid loading personal .env files or persistent state.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for component in ('home-runner', 'scripts'):
                shutil.copytree(ROOT / component, root / component,
                                ignore=shutil.ignore_patterns('.venv', '.env*', '__pycache__', 'tests'))
            environment = dict(os.environ, SECRET_KEY='deployment-regression', PYTHONPATH='')
            result = subprocess.run(
                [sys.executable, '-c', 'import main; assert main.SECRET_KEY == "deployment-regression"'],
                cwd=root / 'home-runner', env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(importlib.util.find_spec('podman_compose'), 'podman-compose is optional')
class TestDebianCompose(unittest.TestCase):
    def test_defaults_overrides_and_missing_key(self):
        import podman_compose
        import yaml
        document = podman_compose.normalize(yaml.safe_load((ROOT / 'deploy/cloud.compose.yaml').read_text()))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / 'cloud.env'
            env_file.write_text('CLOUD_INTERNAL_KEY=' + 'a' * 64 + '\n')
            environment = CONTROL.compose_environment(env_file)
            expanded = podman_compose.rec_subs(document, environment)
        values = expanded['services']['jarvis-cloud']['environment']
        self.assertEqual(values['CLOUD_QUOTA_BYTES'], '21474836480')
        self.assertEqual(values['CLOUD_MAX_FILE_BYTES'], '5368709120')
        self.assertEqual(values['CLOUD_CHUNK_BYTES'], '4194304')
        self.assertEqual(values['CLOUD_MAX_VERSIONS'], '10')
        self.assertEqual(values['CLOUD_CONFIRMED_HISTORY_LIMIT'], '-1')
        self.assertTrue(all('${' not in value for value in values.values()))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CLOUD_MAX_VERSIONS': '0'}, clear=True):
            env_file = Path(directory) / 'cloud.env'
            env_file.write_text('CLOUD_INTERNAL_KEY=' + 'a' * 64 + '\nCLOUD_QUOTA_BYTES=123456\nCLOUD_MAX_VERSIONS=5\n')
            environment = CONTROL.compose_environment(env_file)
            expanded = podman_compose.rec_subs(document, environment)
        values = expanded['services']['jarvis-cloud']['environment']
        self.assertEqual(values['CLOUD_QUOTA_BYTES'], '123456')
        self.assertEqual(values['CLOUD_MAX_VERSIONS'], '0')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / 'cloud.env'
            env_file.write_text('')
            with self.assertRaises(ValueError):
                CONTROL.compose_environment(env_file)


if __name__ == '__main__':
    unittest.main()
