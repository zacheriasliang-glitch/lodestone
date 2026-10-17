"""Regression checks using temporary files and pure observations, never live commands."""
import importlib.util
from pathlib import Path
import tempfile
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location('_repair_' + name, ROOT / 'tools/ecosystem' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='conscience repairs ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.c = load('conscience')
        self.policy = self.c.load_policy()
        self.policy['record_roots'] = []
        self.policy['writers'] = []
        self.policy['writable_roots_by_actor'] = {'test': [str(self.base)]}
        self.workspace = self.base / 'workspace'
        self.record_dir = self.workspace / 'organs/conscience/records'
        self.record_dir.mkdir(parents=True)
        self.record = self.record_dir / 'evidence.json'
        self.record.write_text('preserved evidence')

    def judge(self, *argv):
        return self.c.assess(list(argv), cwd=self.base, actor='test', policy=self.policy,
                             woken=True, temp={'state': 'WARM'}, workspace=self.workspace)

    def test_record_sources_are_readable_but_mutations_refused(self):
        before = self.record.read_bytes()
        result = self.judge('cp', str(self.record), str(self.base / 'copy.json'))
        self.assertEqual(result['verdict'], 'ALLOW', result)
        commands = [
            ['rm', str(self.record)], ['unlink', str(self.record)],
            ['truncate', '-s', '0', str(self.record)], ['shred', str(self.record)],
            ['dd', 'if=/dev/zero', 'of=' + str(self.record)],
            ['tee', '-a', str(self.record)], ['mv', str(self.record), str(self.base / 'moved')],
            ['cp', str(self.base / 'input'), str(self.record)],
            ['sh', '-c', 'echo changed >>' + shlex.quote(self.record.as_posix())],
            ['rm', '-r', str(self.record_dir)], ['rm', '-r', str(self.workspace)],
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.judge(*command)
                self.assertEqual(result['verdict'], 'REFUSE', result)
                self.assertIn('records-append-only', [r['rule'] for r in result['rules']])
        self.assertEqual(self.record.read_bytes(), before)

    def test_copy_into_directory_checks_actual_destination(self):
        source = self.base / 'evidence.json'
        source.write_text('different')
        result = self.judge('cp', str(source), str(self.record_dir))
        self.assertEqual(result['verdict'], 'REFUSE', result)
        new_source = self.base / 'new.json'
        new_source.write_text('new evidence')
        result = self.judge('cp', str(new_source), str(self.record_dir))
        self.assertEqual(result['verdict'], 'ALLOW', result)
        self.assertEqual(result['mutation_targets'], [str(self.record_dir / 'new.json')])

    def test_record_root_boundary_and_symlink_alias(self):
        sibling = self.workspace / 'organs-other'
        sibling.mkdir()
        unprotected = sibling / 'other.json'
        unprotected.write_text('outside')
        self.assertEqual(self.judge('rm', str(unprotected))['verdict'], 'ALLOW')
        alias = self.base / 'alias'
        try:
            alias.symlink_to(self.record_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable on this platform")
        self.assertEqual(self.judge('rm', str(alias / self.record.name))['verdict'], 'REFUSE')

    def test_ambiguous_shell_requires_host(self):
        result = self.judge('sh', '-c', 'echo ok; rm something')
        self.assertNotEqual(result['verdict'], 'ALLOW')
        self.assertIn('unparsed-write', [r['rule'] for r in result['rules']])

    def test_git_executable_and_effective_working_directory(self):
        mine = self.base / 'mine'
        other = self.base / 'other'
        mine.mkdir()
        other.mkdir()
        self.policy['writers'] = [{'prefix': str(other), 'owner': 'peer'}]
        def judge(argv):
            return self.c.assess(argv, cwd=mine, actor='test', policy=self.policy,
                                 woken=True, temp={'state': 'WARM'}, workspace=self.workspace)
        for command in (
                ['/usr/bin/git', '-C', str(other), 'reset', '--hard'],
                ['git', '-C', '..', '-C', 'other', 'reset', '--hard'],
                ['sh', '-c', '/usr/bin/git -C ' + shlex.quote(other.as_posix()) + ' reset --hard']):
            with self.subTest(command=command):
                self.assertEqual(judge(command)['verdict'], 'REFUSE')
        self.assertEqual(judge(['/usr/bin/git', '-C', '../other', 'status'])['verdict'], 'ALLOW')
        self.assertEqual(judge(['echo', 'git', '-C', str(other), 'reset', '--hard'])['verdict'], 'ALLOW')
        self.assertNotEqual(judge(['git', '--git-dir=' + str(other), 'reset', '--hard'])['verdict'], 'ALLOW')

    def test_shared_doctor_readiness_requires_sources_and_workspace(self):
        # Public doctor observes only core source capability and workspace access.
        cli = load('cli')
        self.assertEqual(cli.doctor_status({'status': 'READY'}, {'status': 'READY'}), 'READY')
        for source_state, workspace_state in (('ATTENTION', 'READY'),
                                              ('READY', 'ATTENTION'),
                                              ('READY', 'SKIPPED')):
            self.assertEqual(cli.doctor_status({'status': source_state},
                                              {'status': workspace_state}), 'ATTENTION')

    def test_restart_directory_is_explicit_workspace_scoped(self):
        with patch.object(Path, 'home', return_value=self.base):
            self.assertEqual(self.c.default_runtime_dir(self.workspace), self.workspace / 'continuity')
            self.assertEqual(self.c.default_runtime_dir(), self.base / '.lodestone/workspace/continuity')
        self.assertFalse((self.workspace / 'continuity').exists())

    def test_temperature_labels_logical_cpus_and_keeps_registered_threshold(self):
        with patch.object(self.c.os, 'cpu_count', return_value=4), \
                patch.object(self.c.os, 'getloadavg', return_value=(2.67822265625, 3, 3), create=True), \
                patch.object(self.c.shutil, 'disk_usage', return_value=SimpleNamespace(free=6_000_000_000)):
            result = self.c.temperature(self.policy, self.workspace, woken=True)
        self.assertEqual(result['state'], 'WARM')
        self.assertEqual(result['cores'], 4)
        self.assertEqual(result['cpu_count_kind'], 'logical')


if __name__ == '__main__':
    unittest.main()
