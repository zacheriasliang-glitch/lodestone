"""New-user checks in an extracted, no-Git package and an empty temporary HOME."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class PortableTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='portable lodestone ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / 'empty home'; self.home.mkdir()
        self.package = self.base / 'extracted package'; self.package.mkdir()
        for directory in ('src', 'tools/ecosystem'):
            shutil.copytree(ROOT / directory, self.package / directory, ignore=shutil.ignore_patterns('__pycache__'))
        (self.package / 'tools/machinery').mkdir()
        shutil.copy2(ROOT / 'tools/machinery/run', self.package / 'tools/machinery/run')
        shutil.copy2(ROOT / 'README.md', self.package / 'README.md')
        (self.package / 'docs').mkdir()
        shutil.copy2(ROOT / 'docs/conscience.md', self.package / 'docs/conscience.md')
        self.workspace = self.base / 'new output' / 'workspace'
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                    'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': ''}
        self.runner = self.package / 'tools/ecosystem/lodestone'

    def invoke(self, *args, workspace=True):
        command = [sys.executable, '-B', str(self.runner)]
        if workspace:
            command += ['--workspace', str(self.workspace)]
        return subprocess.run(command + list(map(str, args)), cwd=self.base, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_doctor_ready_without_git_home_state_or_processes(self):
        before = sorted(self.home.rglob('*'))
        result = self.invoke('doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'READY')
        self.assertEqual(sorted(self.home.rglob('*')), before)
        self.assertFalse(self.workspace.exists())
        self.assertFalse((self.package / '.git').exists())
        spec = importlib.util.spec_from_file_location('_portable_cli', self.package / 'tools/ecosystem/cli.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with patch.object(cli.subprocess, 'run', side_effect=AssertionError('unexpected process')):
            self.assertEqual(cli.doctor(self.package, self.workspace)['status'], 'READY')

    def test_default_workspace_uses_new_home_and_is_not_created_by_doctor(self):
        result = self.invoke('doctor', workspace=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['workspace']['path'], str(self.home / '.lodestone/workspace'))
        self.assertFalse((self.home / '.lodestone').exists())

    def test_missing_source_and_file_workspace_are_attention(self):
        (self.package / 'tools/ecosystem/conscience_targets.py').unlink()
        result = self.invoke('doctor')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'ATTENTION')
        self.workspace.parent.mkdir(); self.workspace.write_text('not a directory')
        result = self.invoke('doctor')
        self.assertEqual(json.loads(result.stdout)['workspace']['status'], 'ATTENTION')

    def test_two_wakes_have_causal_history_and_keep_original_bytes(self):
        first = self.invoke('wake')
        self.assertEqual(first.returncode, 0, first.stderr)
        a = json.loads(first.stdout)
        self.assertEqual(a['shape']['doctor_status'], 'READY')
        self.assertEqual(a['restart_readme'], 'ABSENT')
        record = self.workspace / 'organs/conscience/records' / (a['handle'].split(':')[1] + '.json')
        before = record.read_bytes()
        second = self.invoke('wake')
        self.assertEqual(second.returncode, 0, second.stderr)
        b = json.loads(second.stdout)
        self.assertEqual(b['history']['last'], a['handle'])
        self.assertEqual(b['history']['previous_wakings'], 1)
        self.assertEqual(record.read_bytes(), before)
        compared = self.invoke('conscience', 'compare', a['handle'], b['handle'])
        self.assertFalse(json.loads(compared.stdout)['history_equal'])
        self.assertEqual(len(list(self.home.iterdir())), 0)

    def test_restart_sidecar_mismatch_is_visible(self):
        notes = self.workspace / 'continuity'; notes.mkdir(parents=True)
        note = notes / 'RESTART-README-20260101.md'; note.write_text('hello')
        note.with_name(note.name + '.sha256').write_text('0' * 64)
        result = self.invoke('wake')
        self.assertEqual(result.returncode, 2)
        self.assertIn('restart_readme_checksum', json.loads(result.stdout)['failed_checks'])

    def test_assess_does_not_execute_and_records_custom_actor(self):
        target = self.base / 'must not exist'
        command = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(target)) + ').touch()']
        result = self.invoke('conscience', '--actor', 'reviewer', 'assess', '--', *command)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertFalse(summary['executed']); self.assertFalse(target.exists())
        record = self.workspace / 'organs/conscience/records' / (summary['handle'].split(':')[1] + '.json')
        payload = json.loads(record.read_text())['payload']
        self.assertEqual(payload['verdict']['actor'], 'reviewer')

    def test_child_separator_spaces_stdout_and_exit_code_are_preserved(self):
        result = self.invoke('conscience', 'run', '--', sys.executable, '-c',
                             'import json,sys; print(json.dumps(sys.argv[1:])); sys.exit(7)', '--', 'two words', '-x')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['--', 'two words', '-x'])
        self.assertEqual(json.loads(result.stderr)['outcome']['returncode'], 7)

    def test_refused_child_is_not_executed(self):
        target = self.base / 'sealed-store'
        result = self.invoke('conscience', 'run', '--', sys.executable, '-c',
                             'from pathlib import Path; Path(' + repr(str(target)) + ').touch()')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)['executed'])
        self.assertFalse(target.exists())

    def test_context_contains_only_available_local_docs(self):
        result = self.invoke('context')
        self.assertEqual(result.returncode, 0, result.stderr)
        content = json.loads(result.stdout)
        self.assertEqual(len(content['documents']), 2)
        self.assertNotIn('notion', content); self.assertNotIn('mailbox', content)

    def test_unicode_json_survives_ascii_streams_without_changing_record_bytes(self):
        label = 'Saṃsāra — 窯'
        self.env['PYTHONIOENCODING'] = 'ascii:strict'
        (self.package / 'README.md').write_text(label, encoding='utf-8')
        context = self.invoke('context')
        self.assertEqual(context.returncode, 0, context.stderr)
        self.assertTrue(context.stdout.isascii())
        self.assertEqual(json.loads(context.stdout)['documents'][0]['text'], label)

        assessed = self.invoke('conscience', '--literal', label, 'assess', '--', 'cat', 'sealed-store/' + label)
        self.assertEqual(assessed.returncode, 2, assessed.stderr)
        self.assertTrue(assessed.stdout.isascii())
        result = json.loads(assessed.stdout)
        self.assertEqual(result['verdict'], 'REFUSE')
        self.assertIn(label, result['rules'][0]['token'])
        identity = result['handle'].split(':')[1]
        record = self.workspace / 'organs/conscience/records' / (identity + '.json')
        raw = record.read_bytes()
        self.assertIn(label.encode('utf-8'), raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), identity)
        self.assertEqual(json.loads(raw)['payload']['host_literal'], label)

        policy = json.loads((self.package / 'tools/ecosystem/conscience-policy.json').read_text(encoding='utf-8'))
        policy['launch_patterns'] = [label]
        policy_path = self.base / 'unicode-policy.json'
        policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding='utf-8')
        executed = self.invoke('conscience', '--policy', policy_path, 'run', '--',
                               sys.executable, '-c', 'pass', '--preregistration', label)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout, '')
        self.assertTrue(executed.stderr.isascii())
        self.assertEqual(json.loads(executed.stderr)['rules'][0]['pattern'], label)

    def test_optional_porcelain_missing_is_actionable(self):
        result = self.invoke('porcelain', 'run', '--help')
        self.assertEqual(result.returncode, 2)
        self.assertIn('optional porcelain', result.stderr.lower())

    def test_local_help_needs_no_optional_environment_or_project_files(self):
        before = sorted(self.home.rglob('*'))
        # The fixture contains no toolkit catalogue or porcelain environment.
        for command in ('doctor', 'context', 'toolkits', 'porcelain'):
            for flag in ('--help', '-h'):
                with self.subTest(command=command, flag=flag):
                    result = self.invoke('--repo', self.base / 'absent package', command, flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)
                    self.assertIn(command, result.stdout)
        overview = self.invoke('porcelain')
        self.assertEqual(overview.returncode, 0, overview.stderr)
        self.assertIn('Subcommand help and execution require', overview.stdout)
        self.assertEqual(sorted(self.home.rglob('*')), before)
        self.assertFalse(self.workspace.exists())

    def test_local_help_does_not_accept_unknown_arguments(self):
        for command in ('doctor', 'context', 'toolkits'):
            with self.subTest(command=command):
                result = self.invoke(command, '--help', '--unexpected')
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.workspace.exists())

    def test_repository_canon_requires_separate_configuration(self):
        result = self.invoke('yggdrasil', 'canon')
        self.assertEqual(result.returncode, 2)
        expected = 'Use Linux or macOS' if os.name == 'nt' else 'separately configured source corpus'
        self.assertIn(expected, result.stderr)

    def test_windows_rejects_posix_routes_including_machinery_aliases(self):
        spec = importlib.util.spec_from_file_location('_portable_windows_routes', self.package / 'tools/ecosystem/cli.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with patch.object(cli.platform, 'system', return_value='Windows'), patch.object(cli.subprocess, 'run') as run:
            for command in (['origami', '--help'], ['yggdrasil', 'archive', '--help'],
                            ['machinery', 'origami', 'ask', '--help'],
                            ['machinery', 'yggdrasil', 'corpus-fold', '--help']):
                with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(cli.main(['--workspace', str(self.workspace), *command]), 2)
                    self.assertIn('Use Linux or macOS', error.getvalue())
            run.assert_not_called()

    def test_windows_preview_keeps_doctor_demo_and_conscience_routes(self):
        spec = importlib.util.spec_from_file_location('_portable_windows_preview', self.package / 'tools/ecosystem/cli.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        options = ['--workspace', str(self.workspace)]
        with patch.object(cli.platform, 'system', return_value='Windows'), patch.object(cli.subprocess, 'run') as run:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(options + ['doctor']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'READY')
            run.return_value.returncode = 0
            self.assertEqual(cli.main(options + ['machinery', 'demo']), 0)
            self.assertIn('demo', run.call_args.args[0])
            run.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(options + ['wake']), 0)
            self.assertEqual(json.loads(output.getvalue())['shape']['doctor_status'], 'READY')
            run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'Optional POSIX launcher; root Python entry works on Windows')
    def test_installer_is_idempotent_pinned_and_refuses_replacement(self):
        bindir = self.base / 'local bin'
        command = [sys.executable, '-B', str(self.package / 'tools/ecosystem/install.py'), '--bin-dir', str(bindir)]
        first = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        launcher = bindir / 'lodestone'
        run = subprocess.run([str(launcher), '--workspace', str(self.workspace), 'doctor'], env=self.env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        second = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(second.stdout)['status'], 'UNCHANGED')
        launcher.write_text('keep me')
        rejected = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(launcher.read_text(), 'keep me')


if __name__ == '__main__':
    unittest.main()
