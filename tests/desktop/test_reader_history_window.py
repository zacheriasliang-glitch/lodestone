"""Declared reader selection; native originals remain complete. No models."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("LODESTONE_DESKTOP_TEST_RUNTIME", ROOT / "desktop/runtime")).resolve()
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import learning_context as context
from lodestone_machinery import loom_runtime as runtime
from lodestone_machinery.records import canonical
from lodestone_nurarihyon import learning_conscience
spec = importlib.util.spec_from_file_location("history_window_bootstrap", ROOT / "desktop/bootstrap_learning.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class ReaderHistoryWindowTests(unittest.TestCase):
    def test_explicit_window_cannot_bypass_validation_by_embedding_full_preparation(self):
        prepared = {"run_handle": "synthetic-native-handle", "binding": {"encounter": "Fixture"}}
        memory = Mock()
        memory.validate_for_dispatch.return_value = prepared
        reference = {"schema": context.PRACTICE_SCHEMA,
                     "literal_context": canonical(prepared).decode(),
                     "reader_history_window": {"schema": "foreign-window", "keep_latest_runs": 1}}
        packet = {"schema": "loom-adapter-request/v1", "context": canonical(reference).decode()}
        with patch.object(context, "validate_practice_packet", side_effect=ValueError("Window mismatch")) as verify:
            refused = learning_conscience.assess(memory, prepared, packet)
        self.assertEqual(refused["verdict"], "REFUSE")
        self.assertEqual(refused["reason"], "Window mismatch")
        verify.assert_called_once_with(memory, prepared, packet, "adapter")
        reference.pop("reader_history_window")
        packet["context"] = canonical(reference).decode()
        with patch.object(context, "validate_practice_packet") as verify:
            allowed = learning_conscience.assess(memory, prepared, packet)
        self.assertEqual(allowed["verdict"], "ALLOW")
        self.assertEqual(allowed["context_delivery"], "FULL_NATIVE_PREPARATION")
        verify.assert_not_called()

    def test_window_requires_explicit_bounded_integer_and_is_execution_bound(self):
        self.assertIsNone(context.history_window_policy(None))
        for value in (0, 33, True, 1.0, "1", {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                context.history_window_policy(value)
        config = {"generation": {"max_tokens": 32}, "max_total_tokens": 1024,
                  "wall_seconds": 3, "max_rss_bytes": 10000000}
        original = runtime._execution_binding(config, {}, encounter="Current", purpose="Purpose", context="Literal")
        windowed = runtime._execution_binding({**config, "history_window": 1}, {},
            encounter="Current", purpose="Purpose", context="Literal")
        self.assertNotEqual(original["input_sha256"], windowed["input_sha256"])
        with patch.object(runtime, "_json", return_value={"schema": runtime.SCHEMA,
                "backend": "ollama_qwen3_base", "history_window": 1}):
            with self.assertRaisesRegex(ValueError, "per-encounter"):
                runtime._config("/unused")

    def test_pure_projection_does_not_mutate_snapshot_or_choose_by_text(self):
        rows = []
        for i in range(3):
            rows.append({"run_handle": "run-" + str(i), "encounter": "Encounter " + str(i),
                "purpose": "Purpose", "outcome": {"response": "Actual " + str(i)},
                "reviews": [{"handle": "review-" + str(i), "review": {"outcome_handle": "outcome-" + str(i),
                    "review": {"reviewer": "Curator", "verdict": "UNRESOLVED",
                               "correction": "Exact multiline\ncorrection " + str(i), "reason": "Details"}}}]})
        snapshot = {"history": rows, "evidence": [], "seeds": [{"conditions": ["Keep condition"]}]}
        saved = deepcopy(snapshot)
        checked = {"run_id": "current", "run_handle": "current-handle", "binding": {"encounter": "Current"}}
        projected = context.history_window_projection(checked, snapshot, 1)
        self.assertEqual(snapshot, saved)
        self.assertEqual(projected["projection"]["departed_history_runs"], ["run-0", "run-1"])
        row = projected["content"]["history"][0]
        self.assertEqual(row["run_handle"], "run-2")
        self.assertEqual(row["folded"], ["encounter", "outcome", "purpose"])
        self.assertEqual(row["reviews"][0]["review"]["review"]["correction"], "Exact multiline\ncorrection 2")
        self.assertEqual(projected["content"]["seeds"], snapshot["seeds"])
        self.assertIn("does not depend on input length", projected["projection"]["fold_rule"])
        all_rows = context.history_window_projection(checked, snapshot, 32)
        self.assertEqual(len(all_rows["content"]["history"]), 3)
        self.assertNotIn("departed_history_runs", all_rows["projection"])

    def test_real_native_packet_window_admission_tamper_refusal_and_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            initialized = bootstrap.initialize(Path(directory) / "preview", RUNTIME, Path(sys.executable))
            code = r'''
import sys, json, hashlib
from pathlib import Path
from copy import deepcopy
sys.path.insert(0, sys.argv[1])
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery import learning_context as c
from lodestone_machinery.records import canonical
from lodestone_nurarihyon.learning_conscience import assess
config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
loom = UnifiedLoom(memory, config['seed_handles'])
outcomes = []
captured = []
for index in range(3):
    result = loom.run(encounter='Scripted prior encounter ' + str(index), purpose='Keep original evidence.',
        reader='Scripted fixture; no model', provider=lambda packet: 'Actual scripted return',
        history_window=1 if index == 0 else None)
    assert result['provider_called'] and result['status'] == 'COMPLETED'
    outcomes.append(result['outcome_handle'])
    loom.correct(result['outcome_handle'], correction='Exact correction for prior ' + str(index), author='Fixture')
def manifest():
    return {str(path.relative_to(memory.workspace)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(memory.workspace).glob('organs/*/records/*.json')}
before = manifest()
def provider(packet):
    captured.append(deepcopy(packet))
    actual = c.expand_learning_projection(json.loads(packet['context'])['learning_context'])
    pending = memory.read_prepared(actual['run_handle'])
    assert assess(memory, pending, packet)['verdict'] == 'ALLOW'
    for mutate in (lambda body: body['reader_history_window'].update(keep_latest_runs=2),
                   lambda body: body['reader_history_window'].update(keep_latest_runs=True),
                   lambda body: body['reader_history_window'].update(schema='foreign-window/v1'),
                   lambda body: body.pop('reader_history_window')):
        changed = deepcopy(packet)
        body = json.loads(changed['context'])
        mutate(body)
        changed['context'] = canonical(body).decode()
        changed['context_sha256'] = hashlib.sha256(changed['context'].encode()).hexdigest()
        assert assess(memory, pending, changed)['verdict'] == 'REFUSE'
    return {'status':'COMPLETED','response':'Actual windowed scripted return',
            'observation':{'model_calls':0,'source':'scripted test'}}
result = loom.run(encounter='Current scripted encounter', purpose='Compare declared exposure.',
    context='Same literal context', reader='Scripted fixture; no model', provider=provider, history_window=1)
assert result['provider_called'] and result['status'] == 'COMPLETED'
after = manifest()
prepared = memory.read_prepared(result['run_handle'])
snapshot = json.loads(prepared['context'])
packet = captured[0]
assert c.validate_practice_packet(memory, prepared, packet, 'adapter') == packet
reference = json.loads(packet['context'])
visible = c.expand_learning_projection(reference['learning_context'])
assert reference['reader_history_window'] == c.history_window_policy(1)
assert visible['projection']['reader_history_window'] == reference['reader_history_window']
assert len(snapshot['history']) == 3 and len(visible['content']['history']) == 1
assert visible['content']['history'][0]['outcome_handle'] == outcomes[-1]
assert visible['content']['history'][0]['reviews'][0]['review']['review']['correction'] == 'Exact correction for prior 2'
assert [row['outcome_handle'] for row in snapshot['history']] == outcomes
assert all(snapshot['history'][i]['outcome'] == memory.read_outcome(handle) for i,handle in enumerate(outcomes))
assert all(after.get(path) == sha for path,sha in before.items())
assert memory.pending() == []
assert all('READER_HISTORY_WINDOW_JSON' in view for view in c.practice_data_candidates(packet))
base = dict(request_id=prepared['run_id'], context='Same literal context',
            encounter=prepared['binding']['encounter'], purpose=prepared['binding']['purpose'])
default = c.build_adapter_request(memory, prepared, **base)
assert default == c.build_adapter_request(memory, prepared, history_window=None, **base)
assert 'reader_history_window' not in json.loads(default['context'])
assert all('READER_HISTORY_WINDOW_JSON' not in view for view in c.practice_data_candidates(default))
def altered(change):
    candidate = deepcopy(packet)
    body = json.loads(candidate['context'])
    change(body)
    candidate['context'] = canonical(body).decode()
    candidate['context_sha256'] = hashlib.sha256(candidate['context'].encode()).hexdigest()
    return candidate
mutations = [lambda p: p['reader_history_window'].update(keep_latest_runs=2),
             lambda p: p['reader_history_window'].update(keep_latest_runs=True),
             lambda p: p['reader_history_window'].update(schema='foreign-window/v1'),
             lambda p: p.pop('reader_history_window'),
             lambda p: p.update(learning_context=json.loads(default['context'])['learning_context'])]
for mutate in mutations:
    candidate = altered(mutate)
    try:
        c.validate_practice_packet(memory, prepared, candidate, 'adapter')
    except ValueError:
        pass
    else:
        raise AssertionError('Tampered declared policy/projection admitted')
try:
    c.validate_practice_packet(memory, prepared, {'task':base['encounter'],
        'reference':packet['context'],'response_schema':{'type':'object'}}, 'subscription')
except ValueError:
    pass
else:
    raise AssertionError('Window admitted to unsupported reader')
try:
    c._practice_reference(memory, prepared, 'Same literal context', encounter=base['encounter'],
        purpose=base['purpose'], fold=True, history_window=1, max_bytes=1)
except ValueError as error:
    assert 'no further departure' in str(error)
else:
    raise AssertionError('Window exceeded original resource bound')
assert all(manifest().get(path) == sha for path,sha in after.items())
print(json.dumps({'passed':True,'scripted_provider_calls':4,'model_calls':0,
    'complete_history_rows':len(snapshot['history']),'visible_history_rows':len(visible['content']['history']),
    'tamper_variants_refused':len(mutations),'native_status':result['status']}))
'''
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                str(RUNTIME / "src"), initialized["active_config"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=100)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertTrue(observed["passed"])
            self.assertEqual(observed["scripted_provider_calls"], 4)
            self.assertEqual(observed["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
