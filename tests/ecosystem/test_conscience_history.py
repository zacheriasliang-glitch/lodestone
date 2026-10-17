"""Causal waking history and exact command forwarding, using temporary stores only.

Host observations are mocked for the publication regression: no real wake,
memory inspection, mailbox inspection, service inspection or command is run.
"""
import hashlib
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


REPOSITORY = Path(__file__).resolve().parents[2]
SOURCE = REPOSITORY / "tools/ecosystem/conscience.py"


def load():
    spec = importlib.util.spec_from_file_location("_conscience_history_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conscience history ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.c = load()
        self.workspace = self.base / "workspace"
        self.store = self.c.record_store(self.workspace)
        self.policy = {
            "actor_default": "test actor",
            "context_documents": [],
            "checks": [],
            "schedule": {"budget_per_wake": 3},
            "_pin": {"sha256": "0" * 64},
        }
        # Public readiness has no host mailbox, services or private-memory probes.
        self.cli = SimpleNamespace(doctor=Mock(return_value={"status": "READY"}))

    def fixture(self):
        first = self.store.publish("conscience", "waking", {"seq": 1})
        second = self.store.publish("conscience", "waking", {"seq": 2}, links=[first])
        third = self.store.publish("conscience", "waking", {"seq": 3}, links=[second])
        # Fixed canonical payloads ensure this exercises the original hash-order bug.
        self.assertLess(third, first)
        self.assertLess(first, second)
        return [first, second, third]

    def snapshot(self):
        return {str(p.relative_to(self.workspace)): p.read_bytes()
                for p in self.workspace.rglob("*.json")}

    def isolated_wake(self):
        with patch.object(self.c, "record_store", return_value=self.store), \
                patch.object(self.c, "load_module", return_value=self.cli), \
                patch.object(self.c, "temperature", return_value={"state": "WARM"}), \
                patch.object(self.c, "now", return_value="2000-01-01T00:00:00+00:00"):
            return self.c.wake(
                root=self.base, workspace=self.workspace,
                runtime_dir=self.base / "runtime", policy=self.policy,
            )

    def test_empty_history(self):
        self.assertEqual(self.c.previous_wakings(self.store), [])

    def test_fourth_wake_links_tip_and_preserves_digest_and_original_bytes(self):
        chain = self.fixture()
        before = self.snapshot()
        self.assertEqual(self.c.previous_wakings(self.store), chain)
        result = self.isolated_wake()
        record = self.store.read(result["handle"])
        self.assertEqual(record["links"], [chain[-1]])
        self.assertEqual(record["payload"]["history"]["last"], chain[-1])
        self.assertEqual(record["payload"]["history"]["previous_wakings"], 3)
        legacy_digest = hashlib.sha256(self.c.canonical_bytes(sorted(chain))).hexdigest()
        self.assertEqual(record["payload"]["history"]["digest"], legacy_digest)
        self.assertEqual(self.c.previous_wakings(self.store), chain + [result["handle"]])
        after = self.snapshot()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(len(after), len(before) + 1)
        self.cli.doctor.assert_called_once_with(self.base, self.workspace)

    def test_links_determine_order_when_clocks_reverse_or_tie(self):
        chain = []
        for seq, timestamp in enumerate(("2030-01-01", "2000-01-01", "2000-01-01"), 1):
            chain.append(self.store.publish(
                "conscience", "waking", {"seq": seq, "woke_at": timestamp},
                links=chain[-1:],
            ))
        self.assertEqual(self.c.previous_wakings(self.store), chain)

    def test_competing_tip_rejects_wake_before_publication(self):
        chain = self.fixture()
        self.isolated_wake()
        self.store.publish("conscience", "waking", {"seq": "competing tip"}, links=[chain[1]])
        before = self.snapshot()
        with patch.object(self.store, "publish", wraps=self.store.publish) as publish:
            with self.assertRaises(ValueError):
                self.isolated_wake()
            publish.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    @staticmethod
    def entry(handle, links=()):
        return {"handle": handle, "owner": "conscience", "kind": "waking",
                "status": "INTACT", "links": list(links)}

    def test_invalid_graphs_are_rejected(self):
        a, b, c, d = ["conscience:" + digit * 64 for digit in "1234"]
        cases = {
            "multiple parents": [self.entry(a), self.entry(b, [a]), self.entry(c, [a, b])],
            "cycle": [self.entry(a, [b]), self.entry(b, [a])],
            "disconnected roots": [self.entry(a), self.entry(b)],
            "one tip plus disconnected cycle": [self.entry(a), self.entry(b, [a]),
                                                 self.entry(c, [d]), self.entry(d, [c])],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                catalog = SimpleNamespace(catalog=Mock(return_value={"entries": entries}))
                with self.assertRaises(ValueError):
                    self.c.previous_wakings(catalog)

    def test_intact_assessment_does_not_change_waking_order(self):
        chain = self.fixture()
        self.store.publish("conscience", "assessment", {"verdict": "ALLOW"}, links=[chain[-1]])
        self.assertEqual(self.c.previous_wakings(self.store), chain)

    def test_damaged_assessment_blocks_history_explicitly(self):
        self.fixture()
        assessment = self.store.publish("conscience", "assessment", {"verdict": "ALLOW"})
        self.store.path(assessment).write_bytes(b"damaged temporary assessment")
        with self.assertRaises(ValueError):
            self.c.previous_wakings(self.store)

    def test_unavailable_conscience_owner_blocks_history(self):
        catalog = SimpleNamespace(catalog=Mock(return_value={"entries": [
            {"owner": "conscience", "status": "UNAVAILABLE", "reason": "test"},
        ]}))
        with self.assertRaises(ValueError):
            self.c.previous_wakings(catalog)

    def test_other_owner_damage_is_outside_waking_history(self):
        catalog = SimpleNamespace(catalog=Mock(return_value={"entries": [
            {"owner": "other", "status": "DAMAGED", "reason": "test"},
        ]}))
        self.assertEqual(self.c.previous_wakings(catalog), [])

    def test_direct_links_to_non_waking_records_are_rejected(self):
        for owner, kind in (("conscience", "assessment"), ("other", "note")):
            with self.subTest(owner=owner, kind=kind):
                store = self.c.record_store(self.base / kind)
                other = store.publish(owner, kind, {"test": True})
                store.publish("conscience", "waking", {"seq": 1}, links=[other])
                with self.assertRaises(ValueError):
                    self.c.previous_wakings(store)

    def test_cli_preserves_command_argument_separators(self):
        command = ["example-program", "--", "literal-argument"]
        for operation in ("assess", "run"):
            for separator in ([], ["--"]):
                with self.subTest(operation=operation, separator=separator):
                    with patch.object(self.c, "load_policy", return_value=self.policy), \
                            patch.object(self.c, "run", return_value={"verdict": "ALLOW", "executed": False}) as run, \
                            redirect_stdout(io.StringIO()):
                        result = self.c.main(
                            [operation, *separator, *command], root=self.base,
                            workspace=self.workspace,
                        )
                    self.assertEqual(result, 0)
                    self.assertEqual(run.call_args.args[0], command)
                    self.assertEqual(run.call_args.kwargs["execute"], operation == "run")


if __name__ == "__main__":
    unittest.main()
