"""Fresh-user setup and real native retention, with scripted providers only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("LODESTONE_DESKTOP_TEST_RUNTIME", ROOT / "desktop/runtime")).resolve()
spec = importlib.util.spec_from_file_location("bootstrap_learning", ROOT / "desktop/bootstrap_learning.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class BootstrapLearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "preview data"
        if not (RUNTIME / "src/lodestone_machinery/bezoar_learning.py").is_file():
            self.fail("Export desktop/runtime before running desktop bootstrap tests")

    def initialize(self):
        return bootstrap.initialize(self.root, RUNTIME, Path(sys.executable))

    def native(self, script, *arguments):
        code = "import sys\nsys.path.insert(0, sys.argv[1])\n" + script
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                 str(RUNTIME / "src"), *map(str, arguments)],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=90, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_teaching_is_valid_labelled_and_idempotent(self):
        first = self.initialize()
        before = snapshot(self.root)
        second = self.initialize()
        self.assertEqual(first, second)
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(first["observer_definition"]["Label"], "com.lodestone.desktop-preview.learning")
        self.assertFalse(first["services_activated"])
        self.assertEqual(first["provider_calls"], 0)
        observed = self.native('''
import json
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.learning_context import read_learning_policy
from lodestone_nurarihyon import learning
from pathlib import Path
config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
seeds = [memory.read_seed(h) for h in config['seed_handles']]
nacres = [memory.read_nacre(h) for h in memory._catalog('nacre')]
evidence = [memory.read_evidence(h) for h in memory._catalog('evidence')]
print(json.dumps({'seeds': seeds, 'nacres': nacres, 'evidence': evidence,
 'policy': read_learning_policy(sys.argv[3]), 'status': learning.status(memory)}))
''', first["active_config"], first["origami_workspace"])
        self.assertEqual(len(observed["seeds"]), 1)
        self.assertEqual(observed["seeds"][0]["state"], "CANDIDATE")
        self.assertEqual(observed["seeds"][0]["authority"], "NONE")
        self.assertIn("not the user's history", observed["seeds"][0]["frame_limits"][0])
        self.assertEqual(len(observed["nacres"]), 1)
        self.assertEqual(len(observed["evidence"]), 2)
        self.assertTrue(all("Not a user event" in row["attribution"] for row in observed["evidence"]))
        self.assertEqual(observed["policy"]["seed_handles"], first["seed_handles"])
        self.assertEqual(observed["status"]["pending"], [])
        self.assertEqual(observed["status"]["terminals"], [])

    def test_actual_failure_correction_and_interruption_reach_next_practice(self):
        initialized = self.initialize()
        observed = self.native('''
import json
from pathlib import Path
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.learning_context import expand_learning_projection
from lodestone_machinery.loom_unified import UnifiedLoom
config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
loom = UnifiedLoom(memory, config['seed_handles'])
def run(provider, encounter):
 return loom.run(encounter=encounter, purpose='Check the observed condition.',
  reader='scripted bootstrap verification', context='Explicit test input.', provider=provider)
failed = run(lambda packet: {'status':'FAILED','response':'Actual partial fixture output',
 'observation':{'source':'scripted test provider','cause':'explicit fixture failure'}}, 'First fixture')
review = loom.correct(failed['outcome_handle'], author='Test reviewer',
 correction='Check the observed condition before repeating this advice.',
 observation='The fixture output omitted the condition.', event_id='bootstrap-correction-test')
def interrupted(packet):
 raise KeyboardInterrupt()
try:
 run(interrupted, 'Interrupted fixture')
except KeyboardInterrupt:
 pass
received = []
def following(packet):
 practice = json.loads(packet['context'])
 received.append(expand_learning_projection(practice['learning_context'])['content'])
 return 'Actual following fixture output'
following_result = run(following, 'Following fixture')
print(json.dumps({'failed':failed['status'], 'review':review['state'],
 'history':received[0]['history'], 'eligible':memory.fold_eligibility(failed['outcome_handle'])['eligible'],
 'following_status': following_result['status'], 'weight_update':following_result['weight_update'],
 'semantic_learning':following_result['semantic_learning'], 'pending':loom.status()['pending']}))
''', initialized["active_config"])
        self.assertEqual(observed["failed"], "FAILED")
        self.assertEqual(observed["review"], "RETAINED_UNRESOLVED")
        self.assertEqual([row["status"] for row in observed["history"]], ["FAILED", "INTERRUPTED"])
        first = observed["history"][0]
        self.assertEqual(first["outcome"]["response"], "Actual partial fixture output")
        self.assertEqual(first["reviews"][0]["review"]["review"]["correction"],
                         "Check the observed condition before repeating this advice.")
        self.assertFalse(observed["eligible"])
        self.assertEqual(observed["pending"], [])
        self.assertEqual(observed["following_status"], "COMPLETED")
        self.assertFalse(observed["weight_update"])
        self.assertEqual(observed["semantic_learning"], "NOT_ESTABLISHED")
        before = snapshot(self.root)
        self.initialize()
        self.assertEqual(before, snapshot(self.root))
        imported = bootstrap.import_memory(Path(initialized["memory_workspace"]),
            self.directory / "retained history", RUNTIME, Path(sys.executable),
            seed_handles=initialized["seed_handles"])
        captured = self.native('''
import json
from pathlib import Path
from lodestone_machinery.bezoar_learning import LearningMemory
config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
prepared = memory.prepare(config['seed_handles'], encounter='Imported-history test',
 purpose='Inspect retained correction.', reader='scripted import verification')
print(json.dumps(json.loads(prepared['context'])['history']))
''', imported["active_config"])
        self.assertEqual([row["status"] for row in captured], ["FAILED", "INTERRUPTED", "COMPLETED"])
        self.assertEqual(captured[0]["reviews"][0]["review"]["review"]["correction"],
                         "Check the observed condition before repeating this advice.")
        self.assertEqual(before, snapshot(self.root))

    def test_refuses_foreign_existing_learning_and_symlink(self):
        memory = self.root / "learning/memory"
        memory.mkdir(parents=True)
        (memory / "keep.txt").write_text("Foreign state")
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.initialize()
        self.assertEqual(before, snapshot(self.root))
        other = self.directory / "link"
        other.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            bootstrap.initialize(other, RUNTIME, Path(sys.executable))

    def test_existing_policy_conflict_is_not_replaced(self):
        initialized = self.initialize()
        policy = Path(initialized["origami_policy"])
        original = json.loads(policy.read_text())
        original["seed_handles"] = ["bezoar:" + "0" * 64]
        policy.write_text(json.dumps(original))
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "policy differs"):
            self.initialize()
        self.assertEqual(before, snapshot(self.root))

    def test_separate_import_keeps_all_record_kinds_and_original_bytes(self):
        initialized = self.initialize()
        source = Path(initialized["memory_workspace"])
        self.native('''
import json
from lodestone_machinery.records import RecordStore
store = RecordStore(sys.argv[2])
handle = store.publish('auditor', 'test-observation', {'attribution':'Scripted independent fixture'})
print(json.dumps(handle))
''', source)
        (source / "ACTIVE.json").write_text('{"host_grants":["Do not import"]}')
        (source / "queue").mkdir()
        (source / "queue/authorization.json").write_text('{"authority":"Do not import"}')
        before = snapshot(self.root)
        destination = self.directory / "imported memory"
        result = bootstrap.import_memory(source, destination, RUNTIME, Path(sys.executable),
                                         seed_handles=initialized["seed_handles"])
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse(result["default_selection_changed"])
        self.assertEqual(result["records"], 5)
        imported = Path(result["memory_workspace"])
        for path in (source / "organs").rglob("*.json"):
            self.assertEqual(path.read_bytes(), (imported / path.relative_to(source)).read_bytes())
        self.assertFalse((imported / "queue").exists())
        self.assertFalse((imported / "ACTIVE.json").exists())
        config = json.loads(Path(result["active_config"]).read_text())
        self.assertEqual(config["memory_workspace"], str(imported))
        with self.assertRaisesRegex(ValueError, "must be new"):
            bootstrap.import_memory(source, destination, RUNTIME, Path(sys.executable),
                                    seed_handles=initialized["seed_handles"])

    def test_archive_pass_reuses_ancestors_but_later_reads_detect_tampering(self):
        initialized = self.initialize()
        actual = self.native('''
import json
from pathlib import Path
from collections import Counter
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery.yggdrasil_learning import validate_learning_records
config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
loom = UnifiedLoom(memory, config['seed_handles'])
for i in range(3):
    run = loom.run(encounter='Cumulative scripted encounter ' + str(i), purpose='Preserve history.',
        reader='Scripted fixture; no model', provider=lambda packet: 'Fixture answer')
    loom.correct(run['outcome_handle'], correction='Retain this actual fixture correction.', author='Fixture')
calls = Counter()
original = memory.store.relay
def counted(consumer, handle, **kwargs):
    calls[(handle, kwargs.get('kind'))] += 1
    return original(consumer, handle, **kwargs)
memory.store.relay = counted
checked = validate_learning_records(memory.store)
first_counts = list(calls.values())
cache_cleared = not hasattr(checked, '_validated_records')
seed = config['seed_handles'][0]
path = Path(config['memory_workspace']) / 'organs/bezoar/records' / (seed.split(':')[1] + '.json')
path.write_bytes(path.read_bytes() + b' ')
try:
    checked.read_seed(seed)
except ValueError:
    rejected = True
else:
    rejected = False
print(json.dumps({'maximum_reads_per_record':max(first_counts), 'records_read':len(first_counts),
    'cache_cleared':cache_cleared, 'later_tampering_rejected':rejected,
    'failure_cache_cleared':not hasattr(checked, '_validated_records')}))
''', initialized["active_config"])
        self.assertGreater(actual["records_read"], 10)
        self.assertEqual(actual["maximum_reads_per_record"], 1)
        self.assertTrue(actual["cache_cleared"])
        self.assertTrue(actual["later_tampering_rejected"])
        self.assertTrue(actual["failure_cache_cleared"])

    def test_import_requires_selection_and_rejects_corruption_without_publishing(self):
        initialized = self.initialize()
        source = Path(initialized["memory_workspace"])
        destination = self.directory / "bad import"
        with self.assertRaisesRegex(ValueError, "explicitly selected"):
            bootstrap.import_memory(source, destination, RUNTIME, Path(sys.executable), seed_handles=[])
        record = next((source / "organs/bezoar/records").glob("*.json"))
        record.write_bytes(record.read_bytes() + b" ")
        before = snapshot(source)
        with self.assertRaisesRegex(ValueError, "identity or linked evidence"):
            bootstrap.import_memory(source, destination, RUNTIME, Path(sys.executable),
                                    seed_handles=initialized["seed_handles"])
        self.assertFalse(destination.exists())
        self.assertEqual(before, snapshot(source))

    def test_import_rejects_record_symlinks(self):
        initialized = self.initialize()
        source = Path(initialized["memory_workspace"])
        record = next((source / "organs/bezoar/records").glob("*.json"))
        original = self.directory / "original-record.json"
        record.rename(original)
        record.symlink_to(original)
        destination = self.directory / "symlink import"
        with self.assertRaisesRegex(ValueError, "canonical record"):
            bootstrap.import_memory(source, destination, RUNTIME, Path(sys.executable),
                                    seed_handles=initialized["seed_handles"])
        self.assertFalse(destination.exists())

    def test_explicit_import_selection_preserves_host_policy_and_previous_records(self):
        initialized = self.initialize()
        old_config = Path(initialized["active_config"]).read_bytes()
        old_policy_path = Path(initialized["origami_policy"])
        policy = json.loads(old_policy_path.read_text())
        policy["purpose"] = "The local user's own practice purpose."
        old_policy_path.write_text(json.dumps(policy, indent=4) + "\n")
        old_policy = old_policy_path.read_bytes()
        before_records = snapshot(Path(initialized["memory_workspace"]))
        imported = bootstrap.import_memory(Path(initialized["memory_workspace"]),
            self.root / "imports/example", RUNTIME, Path(sys.executable),
            seed_handles=initialized["seed_handles"])
        result = bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual(result["status"], "SELECTED")
        selected = json.loads(Path(initialized["active_config"]).read_text())
        self.assertEqual(selected["memory_workspace"], imported["memory_workspace"])
        self.assertEqual(selected["scope"], json.loads(old_config)["scope"])
        changed_policy = json.loads(old_policy_path.read_text())
        self.assertEqual(changed_policy["purpose"], policy["purpose"])
        self.assertEqual(changed_policy["memory_workspace"], imported["memory_workspace"])
        transaction = Path(result["transaction"])
        self.assertEqual((transaction / "before-ACTIVE.json").read_bytes(), old_config)
        self.assertEqual((transaction / "before-learning-policy.json").read_bytes(), old_policy)
        self.assertFalse((self.root / "learning/STOP").exists())
        self.assertFalse((Path(imported["destination"]) / "STOP").exists())
        for name, digest in before_records.items():
            self.assertEqual(snapshot(Path(initialized["memory_workspace"]))[name], digest)
        self.assertEqual(self.initialize()["memory_workspace"], imported["memory_workspace"])
        second = bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual(second["status"], "UNCHANGED")

    def test_selection_refuses_active_practice_and_corrupt_import(self):
        initialized = self.initialize()
        imported = bootstrap.import_memory(Path(initialized["memory_workspace"]),
            self.root / "imports/example", RUNTIME, Path(sys.executable),
            seed_handles=initialized["seed_handles"])
        before_config = Path(initialized["active_config"]).read_bytes()
        import fcntl
        with (Path(initialized["memory_workspace"]) / "learning-weaving.lock").open("a+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual(Path(initialized["active_config"]).read_bytes(), before_config)
        record = next((Path(imported["memory_workspace"]) / "organs/bezoar/records").glob("*.json"))
        record.write_bytes(record.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual(Path(initialized["active_config"]).read_bytes(), before_config)

    def test_selection_write_failure_restores_both_exact_preimages(self):
        initialized = self.initialize()
        imported = bootstrap.import_memory(Path(initialized["memory_workspace"]),
            self.root / "imports/example", RUNTIME, Path(sys.executable),
            seed_handles=initialized["seed_handles"])
        before_config = Path(initialized["active_config"]).read_bytes()
        before_policy = Path(initialized["origami_policy"]).read_bytes()
        observed = self.native('''
import importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location('bootstrap_failure_test', sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module._replace
calls = []
def fail_once(path, raw, expected):
 calls.append(str(path))
 if len(calls) == 2:
  raise OSError('Scripted second-config write failure')
 return original(path, raw, expected)
module._replace = fail_once
try:
 module._select(Path(sys.argv[3]), Path(sys.argv[4]))
except OSError as error:
 print(json.dumps({'error':str(error), 'calls':len(calls)}))
else:
 raise AssertionError('Expected an injected write failure')
''', ROOT / "desktop/bootstrap_learning.py", self.root, imported["destination"])
        self.assertEqual(observed["calls"], 3)
        self.assertEqual(Path(initialized["active_config"]).read_bytes(), before_config)
        self.assertEqual(Path(initialized["origami_policy"]).read_bytes(), before_policy)
        self.assertFalse((self.root / "learning/STOP").exists())
        self.assertFalse((Path(imported["destination"]) / "STOP").exists())

    def test_configured_origami_handoff_delivers_actual_correction(self):
        initialized = self.initialize()
        source = self.directory / "sources"
        source.mkdir()
        (source / "note.txt").write_text("Record what was observed before claiming that a proposed action succeeded.\n")
        observed = self.native('''
import json
from pathlib import Path
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery.origami import Origami
config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
organ = Origami(sys.argv[3])
roles = {'constructor':'fixture constructor','consumer':'fixture consumer',
 'provider':'scripted fixture provider','checker':'fixture checker'}
def begin(epoch):
 return organ.start(Path(sys.argv[4]), ['note.txt'], 'What should precede a success claim?',
  epoch, 'Explicit scripted verification, not a host grant', roles=roles)['run_id']
received = []
def provider(request):
 received.append(json.loads(request['learning_context']['context']))
 response = {'request_id':request['request_id'], 'decision':'ANSWER',
  'claims':[{'id':'observation','subject':'note.instruction','value':'record observations',
   'scope':'supplied note','epoch':request['epoch'],'basis':'RECORDED',
   'support':[request['evidence'][0]['id']]}], 'relations':[], 'alternatives':[],
  'answer':'Record what was observed first.', 'answer_claim_ids':['observation'],
  'next_check':None, 'coverage_assumptions':['Only the supplied note was checked.'], 'unresolved':[]}
 return {'provider':roles['provider'],'model':'SCRIPTED_FIXTURE','response_id':None,
  'usage':None,'response':response}
first = organ.run(begin('first-fixture'), provider)
outcome = first['learning_outcome']['outcome_handle']
UnifiedLoom(memory, config['seed_handles']).correct(outcome, author='Scripted fixture reviewer',
 correction='Explicitly distinguish the proposed action from its observed result.',
 observation='Review of the actual first fixture response.', event_id='origami-bootstrap-correction')
second = organ.run(begin('second-fixture'), provider)
print(json.dumps({'first_status':first['status'],'second_status':second['status'],
 'first_native_status':memory.read_outcome(outcome)['status'],
 'second_native_status':memory.read_outcome(second['learning_outcome']['outcome_handle'])['status'],
 'history':received[1]['history'],'fold_status':second['learning_fold_status'],
 'eligible':memory.fold_eligibility(outcome)['eligible'], 'scripted_calls':len(received)}))
''', initialized["active_config"], initialized["origami_workspace"], source)
        self.assertEqual(observed["first_status"], "ANSWER")
        self.assertEqual(observed["second_status"], "ANSWER")
        self.assertEqual(observed["first_native_status"], "COMPLETED")
        self.assertEqual(observed["second_native_status"], "COMPLETED")
        self.assertEqual(observed["scripted_calls"], 2)
        self.assertEqual(observed["history"][0]["reviews"][0]["review"]["review"]["correction"],
                         "Explicitly distinguish the proposed action from its observed result.")
        self.assertFalse(observed["eligible"])
        self.assertEqual(observed["fold_status"], "PROVISIONAL_NOT_DEMONSTRATED")

    def test_reconciliation_is_model_free_and_respects_stop(self):
        initialized = self.initialize()
        result = bootstrap.reconcile(self.root, RUNTIME, Path(sys.executable))
        self.assertEqual(result["status"], "RECONCILED")
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["automatic_retry"])
        import fcntl
        with (Path(initialized["memory_workspace"]) / "learning-broker.lock").open("a+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = bootstrap.reconcile(self.root, RUNTIME, Path(sys.executable))
            self.assertEqual(busy["status"], "BUSY")
            self.assertEqual(busy["provider_calls"], 0)
        (self.root / "learning/STOP").write_text("User stopped practice")
        before = snapshot(self.root)
        result = bootstrap.reconcile(self.root, RUNTIME, Path(sys.executable))
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(before, snapshot(self.root))


if __name__ == "__main__":
    unittest.main()
