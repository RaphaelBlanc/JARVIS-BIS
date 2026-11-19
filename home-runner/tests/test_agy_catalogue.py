"""Catalogue natif et contrat Hydrogen/xhigh, sans compte AGY ni serveur réseau."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import agy_bridge
import agy_catalogue as catalogue
import nc_engines


MODELS = [dict(id=f'{family}-{level}', name=f'{family} ({level})')
          for family in ('gemini-4-hydrogen', 'gemini-4-argon', 'gemini-3.8-flash')
          for level in ('low', 'medium', 'high')]
MODELS += [dict(id='gemini-4-hydrogen-xhigh', name='Hydrogen (Xhigh)')]
LISTING = ''.join(m['id'] + '\t' + m['name'] + '\n' for m in MODELS)


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='jarvis-agy-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / '.nc-jarvis-agy-models.json'
        self.threads = []
        self.addCleanup(self.join_threads)

    def join_threads(self):
        for obj in self.threads:
            if obj.thread:
                obj.thread.join(5)
                self.assertFalse(obj.thread.is_alive())

    def manager(self, fallback=None, cli=lambda: '/fake/agy'):
        obj = catalogue.CatalogueAGY(self.path, fallback or [], cli)
        self.threads.append(obj)
        return obj

    def fake_cli(self, body):
        path = self.root / 'agy'
        path.write_text('#!' + sys.executable + '\nimport os,sys,time,subprocess\n' + body)
        path.chmod(0o700)
        return str(path)

    def test_parse_real_ids_and_reject_entire_malformed_listing(self):
        self.assertEqual(catalogue.parse_models(LISTING), MODELS)
        for bad in ('--yolo\tFlag', 'a/b\tSlash', 'id with space\tName',
                    'missing-tab', 'id\tName\textra', 'x' * 56 + '\tName', '$(whoami)\tName'):
            with self.subTest(bad=bad):
                self.assertIsNone(catalogue.parse_models(LISTING + bad))

    def test_empty_duplicate_and_bounded_names(self):
        self.assertIsNone(catalogue.parse_models('\n  \n'))
        parsed = catalogue.parse_models('a\t@everyone **hi**\r\na\tDuplicate\nb\t\x1b\x07\n')
        self.assertEqual(parsed, [dict(id='a', name='everyone hi'), dict(id='b', name='b')])
        self.assertEqual(len(catalogue.parse_models(''.join(f'm{i}\tM\n' for i in range(80)))), 50)

    def test_private_cache_compatible_with_noctalia(self):
        now = time.time()
        catalogue.write_cache(self.path, MODELS, now)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        obj = self.manager()
        self.assertEqual(obj.models, MODELS)
        self.assertEqual(obj.updated, now)
        with patch.object(catalogue, 'run_cli', side_effect=AssertionError('CLI must not start')):
            obj.refresh()
        self.assertIsNone(obj.thread)

    def test_cache_rejects_permissions_symlink_malformed_and_nonfinite_date(self):
        catalogue.write_cache(self.path, MODELS, time.time())
        self.path.chmod(0o644)
        self.assertIsNone(catalogue.read_cache(self.path))
        self.path.unlink()
        self.path.symlink_to(self.root / 'target')
        with self.assertRaises(ValueError):
            catalogue.write_cache(self.path, MODELS, time.time())
        self.assertIsNone(catalogue.read_cache(self.path))
        self.path.unlink()
        for data in ({'t': float('nan'), 'modeles': MODELS}, {'t': True, 'modeles': MODELS},
                     {'t': 10**400, 'modeles': MODELS}, {'t': 1, 'modeles': [{'id': '--x', 'name': 'X'}]}):
            self.path.write_text(json.dumps(data))
            self.path.chmod(0o600)
            self.assertIsNone(catalogue.read_cache(self.path))

    def test_cli_stdin_closed_and_bot_tokens_not_inherited(self):
        cli = self.fake_cli("assert sys.stdin.read() == ''\n"
                            "assert 'SECRET_KEY' not in os.environ\n"
                            "assert 'TELEGRAM_TOKEN' not in os.environ\n"
                            "print('id\\tName')\n")
        with patch.dict(os.environ, SECRET_KEY='test-private', TELEGRAM_TOKEN='test-private'):
            self.assertEqual(catalogue.run_cli(cli, 'models'), 'id\tName\n')

    def test_cli_help_on_stderr_nonzero_invalid_utf8_and_overflow(self):
        cli = self.fake_cli("print('models List available models', file=sys.stderr)\n")
        self.assertIn('models', catalogue.run_cli(cli, '--help'))
        cli = self.fake_cli("print('id\\tName')\nsys.exit(1)\n")
        self.assertIsNone(catalogue.run_cli(cli, 'models'))
        cli = self.fake_cli("sys.stdout.buffer.write(b'\\xff')\n")
        with self.assertRaises(UnicodeDecodeError):
            catalogue.run_cli(cli, 'models')
        cli = self.fake_cli("while True: sys.stdout.write('x' * 8192); sys.stdout.flush()\n")
        self.assertIsNone(catalogue.run_cli(cli, 'models'))

    def test_timeout_kills_cli_and_child_holding_the_pipe(self):
        pids = self.root / 'pids'
        cli = self.fake_cli("p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                            f"open({str(pids)!r}, 'w').write(str(os.getpid()) + ' ' + str(p.pid))\n"
                            "time.sleep(60)\n")
        with patch.object(catalogue, 'TIMEOUT', 2):
            self.assertIsNone(catalogue.run_cli(cli, 'models'))
        for pid in pids.read_text().split():
            proc = Path('/proc') / pid / 'stat'
            deadline = time.monotonic() + 3
            while proc.exists() and proc.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                if time.monotonic() >= deadline:
                    self.fail(f'Process {pid} still running')
                time.sleep(.01)

    def test_background_refresh_single_flight_then_disk_cache(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run(path, arg):
            if arg == '--help':
                return 'Usage:\n  models List available models\n'
            entered.set()
            release.wait(3)
            return LISTING
        obj = self.manager([dict(id='old', name='Old')])
        with patch.object(catalogue, 'run_cli', side_effect=run) as cli:
            obj.refresh()
            self.assertTrue(entered.wait(2))
            self.assertTrue(obj.lock.locked())
            self.assertEqual(obj.models[0]['id'], 'old')
            for _ in range(5): obj.refresh()
            release.set()
            obj.thread.join(3)
            self.assertEqual(cli.call_count, 2)
        self.assertEqual(obj.models, MODELS)
        self.assertEqual(catalogue.read_cache(self.path)[0], MODELS)

    def test_failure_keeps_last_catalogue_and_throttles_retry(self):
        obj = self.manager([dict(id='old', name='Old')])
        with patch.object(catalogue, 'run_cli', side_effect=['  models List\n', 'invalid']) as cli:
            obj.refresh(); obj.thread.join(3)
            obj.refresh()
            self.assertEqual(cli.call_count, 2)
        self.assertEqual(obj.models, [dict(id='old', name='Old')])
        self.assertEqual(obj.updated, 0)
        self.assertFalse(self.path.exists())

    def test_old_cli_does_not_receive_models_as_a_prompt(self):
        obj = self.manager()
        with patch.object(catalogue, 'run_cli', return_value='Usage: agy -p PROMPT\n') as cli:
            obj.refresh(); obj.thread.join(3)
            cli.assert_called_once_with('/fake/agy', '--help')

    def test_failed_help_can_be_retried_and_future_cache_is_refreshed(self):
        catalogue.write_cache(self.path, MODELS, time.time() + 3600)
        obj = self.manager()
        with patch.object(catalogue, 'run_cli', return_value=None):
            obj.refresh(); obj.thread.join(3)
        self.assertIsNone(obj.supports_models)
        obj.attempt = None
        with patch.object(catalogue, 'run_cli', side_effect=['  models List\n', LISTING]) as cli:
            obj.refresh(); obj.thread.join(3)
            self.assertEqual(cli.call_count, 2)
        self.assertLessEqual(obj.updated, time.time())


class NativeEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='jarvis-engine-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        sessions = patch.dict(agy_bridge._SESSIONS, {}, clear=True)
        sessions.start(); self.addCleanup(sessions.stop)
        self.up = types.SimpleNamespace(__file__=str(self.root / 'main.py'), KNOWN_MODELS=list(agy_bridge.KNOWN_MODELS))
        for name in ('run_agy', 'get_session_info', 'update_session', 'reset_session_async',
                     'list_recent_conversations', 'get_conversation_history', 'resume_conversation',
                     'delete_conversation', 'delete_all_conversations', 'get_token_usage'):
            setattr(self.up, name, getattr(agy_bridge, name))
        with patch.dict(os.environ, NC_JARVIS_ENGINE='agy'):
            self.engine = nc_engines.Engines(self.up)
        self.engine.agy_catalogue.models = list(MODELS)
        self.engine.agy_catalogue.updated = time.time()
        self.key = 'desktop:catalogue-test'

    def choose(self, **values):
        return self.engine.choose(session_key=self.key, **values)

    def test_hydrogen_xhigh_selection_and_session_per_pc(self):
        selected = self.choose(model='gemini-4-hydrogen-high', effort='xhigh')
        self.assertEqual((selected['model'], selected['effort']), ('gemini-4-hydrogen-xhigh', 'xhigh'))
        self.assertEqual(self.engine.session(session_key=self.key)['model'], 'gemini-4-hydrogen-xhigh')
        self.assertEqual(self.engine.session(session_key='desktop:other')['model'], 'gemini-3.8-flash-low')
        self.assertFalse(self.engine.snapshot(self.key)['shared_session'])

    def test_effort_max_and_model_without_effort(self):
        self.choose(model='gemini-4-hydrogen-low')
        selected = self.choose(effort='max')
        self.assertEqual((selected['model'], selected['effort']), ('gemini-4-hydrogen-xhigh', 'xhigh'))
        self.choose(model='gemini-4-hydrogen-low')
        selected = self.choose(model='gemini-4-hydrogen-xhigh')
        self.assertEqual(selected['effort'], 'xhigh')

    def test_argon_xhigh_falls_back_to_high_and_missing_variant_is_not_invented(self):
        for effort in ('xhigh', 'max'):
            selected = self.choose(model='gemini-4-argon-low', effort=effort)
            self.assertEqual((selected['model'], selected['effort']), ('gemini-4-argon-high', 'high'))
        self.engine.agy_catalogue.models = [m for m in MODELS if m['id'] != 'gemini-4-argon-high']
        selected = self.choose(model='gemini-4-argon-low', effort='high')
        self.assertEqual((selected['model'], selected['effort']), ('gemini-4-argon-low', 'low'))

    def test_flash_model_does_not_inherit_previous_family_effort(self):
        self.choose(model='gemini-4-hydrogen-xhigh')
        selected = self.choose(model='gemini-3.8-flash-low')
        self.assertEqual((selected['model'], selected['effort']), ('gemini-3.8-flash-low', 'low'))
        selected = self.choose(model='gemini-3.8-flash-high', effort='high')
        self.assertEqual((selected['model'], selected['effort']), ('gemini-3.8-flash-high', 'high'))

    def test_thinking_opus_still_has_no_effort(self):
        selected = self.choose(model='claude-opus-4-6-thinking', effort='high')
        self.assertIsNone(selected['effort'])

    def test_snapshot_reports_account_refresh_and_xhigh_only_when_advertised(self):
        snapshot = self.engine.snapshot(self.key)
        agy = next(e for e in snapshot['engines'] if e['id'] == 'agy')
        self.assertIn('xhigh', agy['efforts'])
        self.assertTrue(agy['catalogue_compte'])
        self.assertFalse(agy['catalogue_en_cours'])
        self.engine.agy_catalogue.models = [m for m in MODELS if not m['id'].endswith('-xhigh')]
        agy = self.engine.snapshot(self.key)['engines'][0]
        self.assertNotIn('xhigh', agy['efforts'])
        self.engine.agy_catalogue.lock.acquire()
        try: self.assertTrue(self.engine.snapshot(self.key)['engines'][0]['catalogue_en_cours'])
        finally: self.engine.agy_catalogue.lock.release()

    def test_restore_aligns_old_settings_without_leaking_public_session_fields(self):
        self.choose(model='gemini-4-hydrogen-high', effort='xhigh')
        state = json.loads(self.engine.path.read_text())
        self.assertEqual(set(state['settings']['agy']), {'model', 'effort'})
        state['settings']['agy'] = dict(model='gemini-4-hydrogen-low', effort='xhigh')
        self.engine.path.write_text(json.dumps(state))
        self.engine.path.chmod(0o600)
        catalogue.write_cache(self.engine.agy_catalogue.path, MODELS, time.time())
        restored = nc_engines.Engines(self.up).session()
        self.assertEqual((restored['model'], restored['effort']), ('gemini-4-hydrogen-xhigh', 'xhigh'))

    def test_direct_run_aligns_explicit_model_and_effort_for_the_requested_pc(self):
        run = AsyncMock(return_value=dict(success=True, output='OK'))
        self.engine.original['run_agy'] = run
        asyncio.run(self.engine.run('test', model='gemini-4-hydrogen-low', effort='max', session_key=self.key))
        self.assertEqual(run.call_args.kwargs['model'], 'gemini-4-hydrogen-xhigh')
        self.assertEqual(run.call_args.kwargs['effort'], 'xhigh')
        self.assertEqual(run.call_args.kwargs['session_key'], self.key)

    def test_actual_bridge_sends_matching_cli_arguments_to_a_fake_executable(self):
        cli = self.root / 'agy'
        args_file = self.root / 'args.json'
        cli.write_text('#!' + sys.executable + '\nimport json,sys\n'
                       f'open({str(args_file)!r}, "w").write(json.dumps(sys.argv[1:]))\nprint("OK")\n')
        cli.chmod(0o700)
        self.choose(model='gemini-4-hydrogen-xhigh')
        # Le vrai bridge et son sous-processus, mais aucun compte ni appel réseau.
        with patch.object(agy_bridge, 'find_agy', return_value=str(cli)):
            response = asyncio.run(self.engine.run('Réponds simplement OK.', continue_session=False,
                                                   permission_mode='request-review', session_key=self.key))
        self.assertTrue(response['success'], response)
        args = json.loads(args_file.read_text())
        self.assertEqual(args[args.index('--model') + 1], 'gemini-4-hydrogen-xhigh')
        self.assertEqual(args[args.index('--effort') + 1], 'xhigh')


if __name__ == '__main__':
    unittest.main()
