"""Exercise automatic pickup with a fake CLI and real local Git transport.

These tests prove worker control flow, not model reasoning or Mac compatibility.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("lodestone_worker", Path(__file__).with_name("worker.py"))
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
transport = worker.transport


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


FAKE = r'''
import json, pathlib, sys, time, os
a = sys.argv[1:]
root = pathlib.Path(__file__).parent
if a == ['--version']:
    print('codex-cli test-fixture'); sys.exit(0)
if a == ['exec', '--help']:
    print('--sandbox --ephemeral --cd --output-last-message'); sys.exit(0)
if a == ['login', 'status']:
    print('Logged in using ChatGPT'); sys.exit(0)
if 'features' in a and 'list' in a:
    with (root/'feature-calls.jsonl').open('a') as f:
        f.write(json.dumps({'args':a, 'cwd':os.getcwd()})+'\n')
    options = [a[i+1] for i, value in enumerate(a[:-1]) if value == '-c']
    scoped = 'features.code_mode_host=false' in options
    mode = (root/'feature-mode').read_text() if (root/'feature-mode').exists() else 'ok'
    if mode == ('scoped-error' if scoped else 'baseline-error'):
        print('Deliberate feature inventory failure', file=sys.stderr); sys.exit(7)
    raw = ((root/'features.txt').read_text() if (root/'features.txt').exists() else
           'code_mode under development false\ncode_mode_host stable true\n'
           'js_repl removed false\nshell_tool stable true\nunified_exec stable true\n')
    if scoped and (root/'features-scoped.txt').exists():
        raw = (root/'features-scoped.txt').read_text()
    elif scoped and mode != 'ignore-overrides':
        raw = raw.replace('code_mode_host stable true', 'code_mode_host stable false')
    print(raw, end=''); sys.exit(0)
if 'mcp' in a and 'list' in a:
    with (root/'mcp-calls.jsonl').open('a') as f:
        f.write(json.dumps({'args':a, 'cwd':os.getcwd()})+'\n')
    options = [a[i+1] for i, value in enumerate(a[:-1]) if value == '-c']
    disabled = [value[len('mcp_servers.'):-len('.enabled=false')]
                for value in options
                if value.startswith('mcp_servers.') and value.endswith('.enabled=false')]
    raw = (root/'mcp.json').read_text() if (root/'mcp.json').exists() else '[]'
    mode = (root/'mcp-mode').read_text() if (root/'mcp-mode').exists() else 'ok'
    if disabled and mode == 'scoped-error':
        print('Deliberate scoped inventory failure', file=sys.stderr); sys.exit(7)
    if disabled and (root/'mcp-scoped.json').exists():
        print((root/'mcp-scoped.json').read_text()); sys.exit(0)
    if disabled and mode != 'ignore-overrides':
        servers = json.loads(raw)
        for server in servers:
            if server['name'] in disabled:
                server['enabled'] = False
        print(json.dumps(servers)); sys.exit(0)
    print(raw); sys.exit(0)
if 'exec' not in a:
    print('Unsupported fake command', file=sys.stderr); sys.exit(2)
prompt = sys.stdin.read()
with (root/'calls.jsonl').open('a') as f:
    f.write(json.dumps({'pid':os.getpid(), 'args':a, 'prompt':prompt, 'cwd':os.getcwd()})+'\n')
mode = (root/'mode').read_text() if (root/'mode').exists() else 'ok'
if mode == 'timeout':
    time.sleep(20)
if mode == 'error':
    print('Deliberate failure', file=sys.stderr); sys.exit(7)
if mode == 'oversize':
    sys.stdout.write('x' * (2*1024*1024)); sys.stdout.flush(); time.sleep(20)
answer = pathlib.Path(a[a.index('--output-last-message')+1])
answer.write_text('Fixture answer for '+prompt[-80:])
print('Fixture completed')
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'lab'
        self.repo.mkdir()
        self.remote = self.root/'remote.git'
        git(self.root, 'init', '--bare', str(self.remote))
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.name', 'Worker test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo/'README.md').write_text('Read-only fixture\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'Fixture')
        git(self.repo, 'push', str(self.remote), 'HEAD:refs/heads/worker-test')
        self.cloud = transport.Mailbox(str(self.remote), 'worker-test', self.root/'cloud')
        self.mac = transport.Mailbox(str(self.remote), 'worker-test', self.root/'mac')
        self.cli = self.root/'fake-codex'
        self.cli.write_text('#!'+sys.executable+'\n'+FAKE)
        self.cli.chmod(0o700)
        self.manifest = []

    def queue(self, name, prompt='Read README.md and report its purpose.'):
        payload = json.dumps({'schema':'lodestone-readonly-job/v1', 'id':name, 'prompt':prompt}).encode()
        self.cloud.send(transport.pack(payload, name, 'mac'))
        self.manifest.append({'id':name, 'sha256':hashlib.sha256(payload).hexdigest()})

    def runner(self, **kwargs):
        return worker.Worker(self.mac, self.repo, self.manifest, codex=str(self.cli),
                             timeout=kwargs.pop('timeout', 5), interval=0.01, **kwargs)

    def calls(self):
        p = self.root/'calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def mcp_calls(self):
        p = self.root/'mcp-calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def feature_calls(self):
        p = self.root/'feature-calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def features(self, text=None, filename='features.txt'):
        if text is None:
            text = ('code_mode under development false\ncode_mode_host stable true\n'
                    'js_repl removed false\nshell_tool stable true\nunified_exec stable true\n')
        path = self.root/filename
        path.write_text(text)
        return path

    def assert_host_blocked(self, name):
        self.queue(name)
        mailbox = transport.Mailbox(str(self.remote), 'worker-test', self.root/('mac-'+name))
        worker.Worker(mailbox, self.repo, self.manifest[-1:], codex=str(self.cli),
                      timeout=5, interval=0.01, disable_code_mode_host=True).run()
        result, _ = self.reply(name)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(result['error'])
        self.assertIsNone(result['invocation_pid'])
        self.assertIsNone(result['exit_code'])
        self.assertEqual(self.calls(), [])

    def inventory(self, value, filename='mcp.json'):
        path = self.root/filename
        path.write_text(json.dumps(value))
        return path

    def assert_mcp_blocked(self, name, error, disable_mcp=True):
        self.queue(name)
        self.runner(disable_mcp=disable_mcp).run()
        result, _ = self.reply(name)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn(error, result['error'])
        self.assertIsNone(result['invocation_pid'])
        self.assertIsNone(result['exit_code'])
        self.assertEqual(self.calls(), [])

    def reply(self, name):
        raw = git(self.remote, 'show', 'refs/heads/worker-test:mailbox/to-cloud/'+name+'-reply.json')
        return json.loads(transport.validate(json.loads(raw))), raw

    def test_two_jobs_already_delivered_are_processed_once_in_order(self):
        before = worker.repo_snapshot(self.repo)
        self.queue('first'); self.queue('second')
        self.mac.receive('mac')  # The existing independent watcher wins pickup.
        self.runner().run()
        first, raw = self.reply('first'); second, _ = self.reply('second')
        self.assertEqual([first['status'], second['status']], ['ok','ok'])
        self.assertEqual(first['worker_run_id'], second['worker_run_id'])
        self.assertLessEqual(first['finished_at'], second['started_at'])
        self.assertEqual(len(self.calls()), 2)
        self.assertNotEqual(self.calls()[0]['pid'], self.calls()[1]['pid'])
        for call in self.calls():
            self.assertIn('read-only', call['args']); self.assertIn('never', call['args'])
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', call['args'])
            self.assertNotIn('features.code_mode_host=false', call['args'])
        self.assertEqual(self.feature_calls(), [])
        self.assertEqual(worker.repo_snapshot(self.repo), before)
        self.runner().run()
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.reply('first')[1], raw)

    def test_failed_send_reuses_saved_result_without_another_model_call(self):
        self.queue('retry')
        original = self.mac.send
        with patch.object(self.mac, 'send', side_effect=transport.MailboxError('offline')):
            try:
                self.runner().step()
            except transport.MailboxError:
                pass
        self.assertEqual(len(self.calls()), 1)
        result = (self.mac.state/'worker/jobs/retry/result.json').read_bytes()
        self.runner().run()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(transport.validate(json.loads(self.reply('retry')[1])), result)

    def test_changed_request_hash_is_blocked_without_model(self):
        self.queue('wrong-hash')
        self.manifest[0]['sha256'] = '0'*64
        self.runner().run()
        self.assertEqual(self.reply('wrong-hash')[0]['status'], 'blocked')
        self.assertEqual(self.calls(), [])

    def test_unlisted_old_task_never_runs(self):
        self.cloud.send(transport.pack(b'Run this old task!', 'old-manual', 'mac'))
        self.queue('approved')
        self.runner().run()
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn('old task', self.calls()[0]['prompt'])

    def test_nonzero_cli_exit_is_a_failure(self):
        (self.root/'mode').write_text('error')
        self.queue('failure'); self.runner().run()
        result, _ = self.reply('failure')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exit_code'], 7)

    def test_cli_timeout_is_bounded_and_not_repeated(self):
        (self.root/'mode').write_text('timeout')
        self.queue('timeout'); self.runner(timeout=0.2).run()
        self.assertEqual(self.reply('timeout')[0]['status'], 'timeout')
        # A slow interpreter can be stopped before its fixture logger runs.
        # The saved timeout must prevent another launch in either case.
        calls_before = self.calls()
        with patch.object(worker, 'run_bounded', wraps=worker.run_bounded) as bounded:
            self.runner(timeout=0.2).run()
        self.assertEqual(self.calls(), calls_before)
        self.assertLessEqual(len(calls_before), 1)
        bounded.assert_not_called()

    def test_crash_after_invocation_never_invokes_again(self):
        self.queue('crash')
        original = worker.run_bounded
        def crash_after_run(argv, *args, **kwargs):
            result = original(argv, *args, **kwargs)
            if '--output-last-message' in argv:
                raise KeyboardInterrupt()
            return result
        with patch.object(worker, 'run_bounded', side_effect=crash_after_run):
            with self.assertRaises(KeyboardInterrupt):
                self.runner().step()
        self.assertEqual(len(self.calls()), 1)
        self.runner().run()
        self.assertEqual(self.reply('crash')[0]['status'], 'interrupted')
        self.assertEqual(len(self.calls()), 1)

    def test_second_worker_cannot_claim_while_first_owns_lock(self):
        self.queue('lock')
        first, second = self.runner(), self.runner()
        with first._lock():
            with self.assertRaises(worker.WorkerError):
                second.step()
        self.assertEqual(self.calls(), [])

    def test_api_key_environment_does_not_trigger_paid_fallback(self):
        self.queue('api-key')
        with patch.dict(os.environ, {'CODEX_API_KEY':'fixture-not-a-key'}):
            self.runner().run()
        self.assertEqual(self.reply('api-key')[0]['status'], 'blocked')
        self.assertEqual(self.calls(), [])

    def test_configured_mcp_still_blocks_without_explicit_opt_in(self):
        self.inventory([{'name':'computer-use', 'enabled':False}])
        self.assert_mcp_blocked('mcp-default', 'explicit read-only scoping', disable_mcp=False)
        self.assertEqual(len(self.mcp_calls()), 1)

    def test_scoped_mcp_disables_every_server_without_changing_inventory(self):
        inventory = self.inventory([{'name':'node_repl', 'enabled':True},
                                    {'name':'computer-use', 'enabled':False}])
        original = inventory.read_bytes()
        before = worker.repo_snapshot(self.repo)
        self.queue('mcp-scoped')
        self.runner(disable_mcp=True).run()
        self.assertEqual(self.reply('mcp-scoped')[0]['status'], 'ok')
        self.assertEqual(inventory.read_bytes(), original)
        self.assertEqual(worker.repo_snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(self.mcp_calls()), 2)
        first, scoped = self.mcp_calls()
        execution = self.calls()[0]
        expected = ['mcp_servers.node_repl.enabled=false',
                    'mcp_servers.computer-use.enabled=false']
        for value in expected:
            self.assertNotIn(value, first['args'])
            self.assertIn(value, scoped['args'])
        self.assertEqual(scoped['args'][:scoped['args'].index('mcp')],
                         execution['args'][:execution['args'].index('exec')])
        self.assertEqual([call['cwd'] for call in (first, scoped, execution)],
                         [str(self.repo)] * 3)
        self.assertIn('read-only', execution['args'])
        self.assertIn('never', execution['args'])

    def test_ignored_mcp_override_blocks_before_model_invocation(self):
        self.inventory([{'name':'node_repl', 'enabled':True}])
        (self.root/'mcp-mode').write_text('ignore-overrides')
        self.assert_mcp_blocked('mcp-ignored', 'still has enabled MCP servers')
        self.assertEqual(len(self.mcp_calls()), 2)

    def test_malformed_mcp_json_blocks_before_model_invocation(self):
        (self.root/'mcp.json').write_text('[{"name":"node_repl",')
        self.assert_mcp_blocked('mcp-malformed', 'Invalid JSON')
        self.assertEqual(len(self.mcp_calls()), 1)

    def test_ambiguous_or_invalid_mcp_inventory_is_rejected_before_scoping(self):
        self.queue('mcp-invalid')
        invalid = [
            ('not-list', {}),
            ('not-object', ['node_repl']),
            ('missing-name', [{'enabled':True}]),
            ('missing-enabled', [{'name':'node_repl'}]),
            ('string-enabled', [{'name':'node_repl', 'enabled':'false'}]),
            ('numeric-enabled', [{'name':'node_repl', 'enabled':0}]),
            ('null-enabled', [{'name':'node_repl', 'enabled':None}]),
            ('duplicate-name', [{'name':'node_repl', 'enabled':True},
                                {'name':'node_repl', 'enabled':False}]),
        ]
        for name in ('', 'node.repl', 'node repl', 'node"repl', 'node/repl', 'nøderepl', 'x'*129):
            invalid.append(('ambiguous-name-'+repr(name), [{'name':name, 'enabled':True}]))
        for label, value in invalid:
            with self.subTest(label=label):
                self.inventory(value)
                before = len(self.mcp_calls())
                _, error = self.runner(disable_mcp=True)._preflight()
                self.assertIsNotNone(error)
                self.assertIn('MCP inventory', error)
                self.assertEqual(len(self.mcp_calls()), before+1)
        self.assertEqual(self.calls(), [])

    def test_duplicate_json_fields_in_mcp_inventory_are_rejected(self):
        (self.root/'mcp.json').write_text(
            '[{"name":"node_repl","enabled":true,"enabled":false}]')
        self.assert_mcp_blocked('mcp-duplicate-field', 'Duplicate JSON field')

    def test_malformed_scoped_mcp_json_blocks_before_model_invocation(self):
        self.inventory([{'name':'node_repl', 'enabled':True}])
        (self.root/'mcp-scoped.json').write_text('not JSON')
        self.assert_mcp_blocked('mcp-scoped-malformed', 'Invalid JSON')
        self.assertEqual(len(self.mcp_calls()), 2)

    def test_scoped_mcp_failure_blocks_before_model_invocation(self):
        self.inventory([{'name':'node_repl', 'enabled':True}])
        (self.root/'mcp-mode').write_text('scoped-error')
        self.assert_mcp_blocked('mcp-scoped-error', 'scoped MCP preflight did not succeed')
        self.assertEqual(len(self.mcp_calls()), 2)

    def test_scoped_mcp_enabled_state_must_be_boolean(self):
        self.inventory([{'name':'node_repl', 'enabled':True}])
        self.inventory([{'name':'node_repl', 'enabled':0}], 'mcp-scoped.json')
        self.assert_mcp_blocked('mcp-scoped-state', 'unsupported name or enabled state')

    def test_new_disabled_server_in_scoped_inventory_blocks(self):
        self.inventory([{'name':'node_repl', 'enabled':True}])
        self.inventory([{'name':'node_repl', 'enabled':False},
                        {'name':'unexpected', 'enabled':False}], 'mcp-scoped.json')
        self.assert_mcp_blocked('mcp-new-server', 'MCP inventory')

    def test_missing_server_in_scoped_inventory_blocks(self):
        self.inventory([{'name':'node_repl', 'enabled':True}])
        self.inventory([], 'mcp-scoped.json')
        self.assert_mcp_blocked('mcp-missing-server', 'MCP inventory')

    def test_host_override_is_identical_for_scoped_features_mcp_and_execution(self):
        inventory = self.inventory([{'name':'node_repl', 'enabled':True},
                                    {'name':'computer-use', 'enabled':False}])
        features = self.features()
        originals = [inventory.read_bytes(), features.read_bytes()]
        before = worker.repo_snapshot(self.repo)
        self.queue('host-scoped')
        runner = self.runner(disable_mcp=True, disable_code_mode_host=True)
        self.assertEqual(runner.run(), 0)
        self.assertEqual(self.reply('host-scoped')[0]['status'], 'ok')
        self.assertEqual([inventory.read_bytes(), features.read_bytes()], originals)
        self.assertEqual(worker.repo_snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(self.feature_calls()), 2)
        self.assertEqual(len(self.mcp_calls()), 2)
        baseline, scoped = self.feature_calls()
        execution = self.calls()[0]
        mcp_baseline, mcp_scoped = self.mcp_calls()
        host_option = 'features.code_mode_host=false'
        for call in (baseline, mcp_baseline):
            self.assertNotIn(host_option, call['args'])
        for value in ('mcp_servers.node_repl.enabled=false',
                      'mcp_servers.computer-use.enabled=false'):
            self.assertIn(value, baseline['args'])
        prefixes = [call['args'][:call['args'].index(command)] for call, command in
                    ((scoped, 'features'), (mcp_scoped, 'mcp'), (execution, 'exec'))]
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertEqual(prefixes[1], prefixes[2])
        self.assertEqual(prefixes[0].count(host_option), 1)
        expected_baseline = list(prefixes[0])
        index = expected_baseline.index(host_option)
        self.assertEqual(expected_baseline[index-1], '-c')
        del expected_baseline[index-1:index+1]
        self.assertEqual(baseline['args'][:baseline['args'].index('features')], expected_baseline)
        self.assertEqual([call['cwd'] for call in (baseline, scoped, mcp_scoped, execution)],
                         [str(self.repo)] * 4)
        self.assertIn('read-only', execution['args'])
        self.assertIn('never', execution['args'])
        stdout = [path.read_text() for path in runner.directory.glob('preflight-*.stdout')]
        self.assertIn(features.read_text(), stdout)
        self.assertIn(features.read_text().replace('code_mode_host stable true',
                                                  'code_mode_host stable false'), stdout)

    def test_ignored_host_override_blocks_before_model_invocation(self):
        (self.root/'feature-mode').write_text('ignore-overrides')
        self.assert_host_blocked('host-ignored')
        self.assertEqual(len(self.feature_calls()), 2)

    def test_feature_inventory_command_failure_blocks_before_model_invocation(self):
        for mode in ('baseline-error', 'scoped-error'):
            with self.subTest(mode=mode):
                (self.root/'feature-mode').write_text(mode)
                self.assert_host_blocked('host-'+mode)

    def test_invalid_feature_inventory_blocks_before_model_invocation(self):
        valid = self.features().read_text()
        invalid = {
            'missing': valid.replace('code_mode_host stable true\n', ''),
            'malformed': valid.replace('code_mode_host stable true', 'code_mode_host stable 0'),
            'duplicate': valid + 'code_mode_host stable true\n',
            'garbage-row': valid + 'unexpected output\n',
        }
        for location in ('features.txt', 'features-scoped.txt'):
            for label, text in invalid.items():
                with self.subTest(location=location, label=label):
                    self.features(valid)
                    (self.root/'features-scoped.txt').unlink(missing_ok=True)
                    effective = (text.replace('code_mode_host stable true',
                                              'code_mode_host stable false')
                                 if location == 'features-scoped.txt' else text)
                    self.features(effective, location)
                    self.assert_host_blocked('host-'+location.replace('.', '-')+'-'+label)

    def test_host_scoping_must_preserve_every_other_feature(self):
        valid = self.features().read_text().replace('code_mode_host stable true',
                                                  'code_mode_host stable false')
        invalid = {
            'changed-value': valid.replace('code_mode under development false',
                                            'code_mode under development true'),
            'changed-stage': valid.replace('js_repl removed false', 'js_repl stable false'),
            'missing-feature': valid.replace('js_repl removed false\n', ''),
            'added-feature': valid + 'unexpected stable false\n',
        }
        for label, text in invalid.items():
            with self.subTest(label=label):
                self.features(text, 'features-scoped.txt')
                self.assert_host_blocked('host-'+label)

    def test_disabled_or_missing_shell_routes_block_before_model_invocation(self):
        valid = self.features().read_text()
        for name in ('shell_tool', 'unified_exec'):
            for state in ('disabled', 'missing'):
                with self.subTest(feature=name, state=state):
                    replacement = name+' stable false\n' if state == 'disabled' else ''
                    self.features(valid.replace(name+' stable true\n', replacement))
                    self.assert_host_blocked('host-'+name+'-'+state)

    def test_already_disabled_host_can_be_verified_without_changing_other_features(self):
        features = self.features()
        self.features(features.read_text().replace('code_mode_host stable true',
                                                   'code_mode_host stable false'))
        self.queue('host-already-disabled')
        self.assertEqual(self.runner(disable_code_mode_host=True).run(), 0)
        self.assertEqual(len(self.feature_calls()), 2)
        self.assertEqual(len(self.calls()), 1)

    def test_single_job_file_manifest_is_admitted_and_consumed_once(self):
        self.queue('one-file-job')
        manifest = self.root/'one-job.json'
        manifest.write_text(json.dumps({'schema':worker.MANIFEST_SCHEMA, 'jobs':self.manifest}))
        self.assertEqual(worker.load_manifest(manifest), self.manifest)
        args = (self.mac, self.repo, manifest)
        kwargs = dict(codex=str(self.cli), timeout=5, interval=0.01,
                      disable_code_mode_host=True)
        self.assertEqual(worker.Worker(*args, **kwargs).run(), 0)
        self.assertEqual(worker.Worker(*args, **kwargs).run(), 0)
        self.assertEqual(len(self.calls()), 1)

    def test_file_manifest_still_rejects_empty_or_oversized_batches(self):
        manifest = self.root/'invalid-batch.json'
        for count in (0, 3):
            with self.subTest(count=count):
                jobs = [{'id':'job-'+str(i), 'sha256':'a'*64} for i in range(count)]
                manifest.write_text(json.dumps({'schema':worker.MANIFEST_SCHEMA, 'jobs':jobs}))
                with self.assertRaises(worker.WorkerError):
                    worker.load_manifest(manifest)

    def test_cli_passes_explicit_host_opt_in_to_worker(self):
        with patch.object(worker, 'Worker') as constructor:
            constructor.return_value.run.return_value = 0
            self.assertEqual(worker.main(['--repo', str(self.repo), '--manifest', 'trial.json',
                                          '--remote', str(self.remote), '--branch', 'worker-test',
                                          '--state', str(self.root/'cli-state'), '--disable-mcp',
                                          '--disable-code-mode-host']), 0)
        self.assertTrue(constructor.call_args.kwargs['disable_code_mode_host'])
        self.assertTrue(constructor.call_args.kwargs['disable_mcp'])

    def test_runaway_output_is_bounded(self):
        (self.root/'mode').write_text('oversize')
        self.queue('overflow'); self.runner().run()
        self.assertEqual(self.reply('overflow')[0]['status'], 'error')
        folder = self.mac.state/'worker/jobs/overflow'
        self.assertLessEqual((folder/'stdout.log').stat().st_size, worker.MAX_OUTPUT)

    def test_interrupted_claim_blocks_remaining_job(self):
        self.queue('interrupted-first'); self.queue('must-not-start')
        original = worker.run_bounded
        def crash_after_run(argv, *args, **kwargs):
            result = original(argv, *args, **kwargs)
            if '--output-last-message' in argv:
                raise KeyboardInterrupt()
            return result
        with patch.object(worker, 'run_bounded', side_effect=crash_after_run):
            with self.assertRaises(KeyboardInterrupt):
                self.runner().step()
        self.runner().run()
        self.assertEqual(self.reply('interrupted-first')[0]['status'], 'interrupted')
        self.assertEqual(self.reply('must-not-start')[0]['status'], 'blocked')
        self.assertEqual(len(self.calls()), 1)

    def test_detached_descendant_cannot_hold_capture_open_forever(self):
        import time
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)'],start_new_session=True); time.sleep(4)"
        started = time.monotonic()
        outcome = worker.run_bounded([sys.executable, '-c', code], b'', self.root,
                    0.1, self.root/'detached.out', self.root/'detached.err')
        self.assertTrue(outcome['timed_out'])
        self.assertLess(time.monotonic()-started, 2.0)

    def test_reaped_group_is_never_signalled(self):
        from unittest.mock import Mock
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with patch.object(worker.os, 'killpg') as kill_group:
            self.assertFalse(worker.signal_owned_group(process, worker.signal.SIGKILL))
        kill_group.assert_not_called()

    def test_group_permission_failure_is_reported(self):
        with patch.object(worker.os, 'killpg', side_effect=PermissionError('denied fixture')):
            with self.assertRaisesRegex(PermissionError, 'denied fixture'):
                worker.run_bounded([sys.executable, '-c', 'import time; time.sleep(5)'],
                    b'', self.root, 0.05, self.root/'denied.out', self.root/'denied.err')

    def test_transient_pickup_error_does_not_consume_job(self):
        self.queue('network')
        original = self.mac.receive
        attempts = []
        def flaky_receive(side):
            attempts.append(side)
            if len(attempts) == 1:
                raise transport.MailboxError('temporary offline fixture')
            return original(side)
        with patch.object(self.mac, 'receive', side_effect=flaky_receive):
            self.runner().run()
        self.assertEqual(self.reply('network')[0]['status'], 'ok')
        self.assertEqual(len(self.calls()), 1)


if __name__ == '__main__':
    unittest.main()
