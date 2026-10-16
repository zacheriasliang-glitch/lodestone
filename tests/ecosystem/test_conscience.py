"""Portable command guards and causal-history regressions; no host state is used."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location('_test_' + name, ROOT / 'tools/ecosystem' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='conscience guards ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.cwd = self.base / 'current directory'
        self.cwd.mkdir()
        self.workspace = self.base / 'separate workspace'
        self.workspace.mkdir()
        self.c = module('conscience')
        self.policy = self.c.load_policy()

    def assess(self, argv, **kwargs):
        return self.c.assess(argv, cwd=self.cwd, actor='local-user', policy=self.policy,
                             workspace=self.workspace, woken=kwargs.get('woken', True),
                             temp={'state': kwargs.get('temperature', 'WARM')})

    def test_default_policy_is_neutral(self):
        self.assertEqual(self.policy['actor_default'], 'local-user')
        self.assertEqual(self.policy['writers'], [])
        self.assertNotIn('host_grants', self.policy)
        self.assertNotIn('pinned', self.policy['interpreter'])

    def test_source_read_does_not_become_a_write(self):
        protected = self.workspace / 'organs/example/records'
        protected.mkdir(parents=True)
        record = protected / 'existing.json'
        record.write_text('{}')
        self.assertEqual(self.assess(['cp', str(record), 'copy.json'])['verdict'], 'ALLOW')
        self.assertEqual(self.assess(['cp', 'copy.json', str(record)])['verdict'], 'REFUSE')
        self.assertEqual(self.assess(['rm', '-rf', str(self.workspace)])['verdict'], 'REFUSE')

    def test_directory_destination_checks_effective_basename(self):
        records = self.workspace / 'organs/example/records'
        records.mkdir(parents=True)
        (records / 'existing.json').write_text('{}')
        self.assertEqual(self.assess(['cp', 'existing.json', str(records)])['verdict'], 'REFUSE')
        self.assertEqual(self.assess(['cp', 'new.json', str(records)])['verdict'], 'ALLOW')

    def test_current_directory_and_selected_workspace_are_write_scopes(self):
        self.assertEqual(self.assess(['cp', 'a.json', 'b.json'])['verdict'], 'ALLOW')
        self.assertEqual(self.assess(['cp', 'a.json', str(self.workspace / 'b.json')])['verdict'], 'ALLOW')
        self.assertEqual(self.assess(['cp', 'a.json', str(self.base / 'elsewhere.json')])['verdict'], 'NEEDS_HOST')

    def test_custom_owner_overrides_current_directory_and_workspace(self):
        self.policy['writers'] = [{'prefix': str(self.cwd), 'owner': 'another-user'}]
        self.assertEqual(self.assess(['cp', 'a', 'b'])['verdict'], 'REFUSE')
        self.assertEqual(self.assess(['cat', 'a'])['verdict'], 'ALLOW')

    def test_git_classifies_executable_and_subcommand_flags(self):
        foreign = self.base / 'foreign'
        self.policy['writers'] = [{'prefix': str(foreign), 'owner': 'another-user'}]
        git = ['/usr/bin/git', '-C', str(foreign)]
        for args in (['diff', '-M'], ['log', '-m', '-1'], ['branch', '--list', 'NEW']):
            self.assertEqual(self.assess(git + args)['verdict'], 'ALLOW', args)
        for args in (['branch', '-m', 'a', 'b'], ['branch', '-v', 'NEW'], ['worktree', 'remove', 'x']):
            self.assertEqual(self.assess(git + args)['verdict'], 'REFUSE', args)
        self.assertEqual(self.assess(['echo', 'git', '-C', str(foreign), 'commit'])['verdict'], 'ALLOW')

    def test_sequential_git_directories_use_command_cwd(self):
        foreign = self.cwd / 'one' / 'two'
        self.policy['writers'] = [{'prefix': str(foreign), 'owner': 'another-user'}]
        self.assertEqual(self.assess(['git', '-C', 'one', '-C', 'two', 'commit'])['verdict'], 'REFUSE')

    def test_shell_redirects_and_unsupported_forms(self):
        target = self.workspace / 'organs/example/records/item'
        target.parent.mkdir(parents=True)
        target.write_text('saved')
        import shlex
        self.assertEqual(self.assess(['sh', '-c', 'echo changed > ' + shlex.quote(str(target))])['verdict'], 'REFUSE')
        for script in ('! cp a b', 'time cp a b', 'echo x; cp a b', 'cp "$SRC" b'):
            self.assertEqual(self.assess(['sh', '-c', script])['verdict'], 'NEEDS_HOST', script)

    def test_path_with_spaces_stays_one_operand(self):
        target = self.workspace / 'output with spaces.json'
        result = self.assess(['cp', 'source with spaces.json', str(target)])
        self.assertEqual(result['verdict'], 'ALLOW')
        self.assertIn(str(target), result['mutation_targets'])

    def test_custom_sealed_rule_and_training_marker(self):
        self.assertEqual(self.assess(['cat', 'sealed-store/input'])['verdict'], 'REFUSE')
        self.assertEqual(self.assess(['python3', 'train.py'])['verdict'], 'NEEDS_HOST')
        self.assertEqual(self.assess(['python3', 'train.py', '--preregistration', 'explicit-reference'])['verdict'], 'ALLOW')

    def test_heavy_commands_defer_when_cold_or_busy(self):
        command = ['find', '.', '-exec', 'sha256sum', '{}', '+']
        self.assertEqual(self.assess(command, woken=False)['verdict'], 'DEFER')
        self.assertEqual(self.assess(command, temperature='HOT')['verdict'], 'DEFER')
        self.assertEqual(self.assess(command)['verdict'], 'ALLOW')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.c = module('conscience')

    def chain(self, entries):
        class Store:
            def catalog(self):
                return {'entries': entries}
        return self.c.previous_wakings(Store())

    @staticmethod
    def entry(handle, links=(), **changes):
        return dict({'owner': 'conscience', 'kind': 'waking', 'status': 'INTACT',
                     'handle': handle, 'links': list(links)}, **changes)

    def test_predecessor_links_win_over_hash_order(self):
        entries = [self.entry('a', ['z']), self.entry('z'), self.entry('m', ['a'])]
        self.assertEqual(self.chain(entries), ['z', 'a', 'm'])

    def test_fork_missing_parent_and_cycle_fail(self):
        cases = [[self.entry('a'), self.entry('b', ['a']), self.entry('c', ['a'])],
                 [self.entry('a', ['missing'])], [self.entry('a', ['b']), self.entry('b', ['a'])],
                 [self.entry('a'), self.entry('b')]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.chain(rows)

    def test_damaged_nonwaking_record_blocks_history(self):
        with self.assertRaises(ValueError):
            self.chain([self.entry('a'), self.entry('b', kind='assessment', status='DAMAGED')])

    def test_intact_assessment_is_not_a_predecessor(self):
        self.assertEqual(self.chain([self.entry('a'), self.entry('b', kind='assessment')]), ['a'])
        with self.assertRaises(ValueError):
            self.chain([self.entry('a', ['b']), self.entry('b', kind='assessment')])

    def test_golden_schedule_and_accounting(self):
        self.assertAlmostEqual(self.c.GOLDEN_ANGLE_DEGREES, 137.50776405, places=6)
        names = list('abcdefgh'); seen = []; cursor = 0
        for _ in range(4):
            chosen, cursor = self.c.golden_schedule(names, cursor, 2)
            seen.extend(chosen)
        self.assertEqual(sorted(seen), names)
        result = self.c.four_part({'x': 1}, {'m': 1}, {'t': 1}, {'p': 1})
        self.assertEqual(result['rest'], result['bytes']['model'] + result['bytes']['table'])


if __name__ == '__main__':
    unittest.main()
