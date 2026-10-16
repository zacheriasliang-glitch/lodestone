"""Saved-state compatibility checks; provider judgments here are test fixtures."""
import json
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery import Origami


ROLES = {"constructor": "fixture-constructor", "consumer": "fixture-consumer",
         "checker": "fixture-checker", "provider": "fixture-provider"}


def reply(request):
    return {"provider": "unit-test fixture", "model": "NONE", "response_id": None,
            "usage": None, "response": {
                "request_id": request["request_id"], "decision": "UNRESOLVED",
                "claims": [], "relations": [], "alternatives": [], "answer": None,
                "answer_claim_ids": [], "next_check": None,
                "coverage_assumptions": ["One supplied fixture document."],
                "unresolved": ["The fixture does not establish an answer."]}}


class SavedStateVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        (self.root / "record.md").write_text("The decision remains open.\n", encoding="utf-8")
        self.workspace = self.base / "workspace"
        self.organ = Origami(self.workspace)

    def start(self, **kwargs):
        return self.organ.start(self.root, ["record.md"], "Which decision applies?",
                                "fixture", "read fixture", roles=dict(ROLES), **kwargs)["run_id"]

    def directory(self, run_id):
        return self.workspace / "origami" / run_id

    def change_version(self, path, version):
        data = json.loads(path.read_text(encoding="utf-8"))
        if version is None:
            del data["schema_version"]
        else:
            data["schema_version"] = version
        path.write_text(json.dumps(data), encoding="utf-8")

    def snapshot(self):
        return {str(path.relative_to(self.workspace)): path.read_bytes() if path.is_file() else None
                for path in self.workspace.rglob("*")}

    def test_supported_pending_versions_resume_with_unversioned_provider_records(self):
        # These are constructed compatibility fixtures, not historical 0.1.0 records.
        for version in ("0.1.0", "0.2.0"):
            for transport in ("advance", "run"):
                with self.subTest(version=version, transport=transport):
                    run_id = self.start()
                    directory = self.directory(run_id)
                    for filename in ("intent.json", "request-000.json"):
                        self.change_version(directory / filename, version)
                    request = self.organ.request(run_id)
                    self.assertEqual(request["schema_version"], version)
                    self.assertNotIn("schema_version", reply(request))
                    if transport == "advance":
                        result = self.organ.advance(run_id, reply(request))
                    else:
                        result = self.organ.run(run_id, reply)
                    self.assertEqual(result["status"], "UNRESOLVED")
                    self.assertEqual(result["schema_version"], "0.2.0")
                    call = json.loads((directory / "call-000.json").read_text(encoding="utf-8"))
                    self.assertNotIn("schema_version", call)
                    self.assertEqual(result["usage"]["calls"], 1)

    def test_unsupported_state_rejects_at_public_entry_points_without_mutation(self):
        for filename in ("intent.json", "request-000.json", "result.json"):
            for version in ("999.0.0", None):
                for operation in ("status", "request", "inspect", "advance", "run"):
                    with self.subTest(file=filename, version=version, operation=operation):
                        run_id = self.start()
                        request = self.organ.request(run_id)
                        envelope = reply(request)
                        if filename == "result.json":
                            self.organ.advance(run_id, envelope)
                        self.change_version(self.directory(run_id) / filename, version)
                        before = self.snapshot()
                        calls = []

                        def provider(observed):
                            calls.append(observed)
                            return reply(observed)

                        with self.assertRaisesRegex(ValueError, "Unsupported Origami saved state schema_version"):
                            if operation == "advance":
                                self.organ.advance(run_id, envelope)
                            elif operation == "run":
                                self.organ.run(run_id, provider)
                            else:
                                getattr(self.organ, operation)(run_id)
                        self.assertEqual(calls, [])
                        self.assertEqual(self.snapshot(), before)

    def test_unsupported_prior_state_does_not_create_a_new_encounter(self):
        for filename in ("intent.json", "result.json"):
            for version in ("999.0.0", None):
                with self.subTest(file=filename, version=version):
                    run_id = self.start()
                    self.organ.advance(run_id, reply(self.organ.request(run_id)))
                    self.change_version(self.directory(run_id) / filename, version)
                    before = self.snapshot()
                    with self.assertRaisesRegex(ValueError, "Unsupported Origami saved state schema_version"):
                        self.start(previous=run_id)
                    self.assertEqual(self.snapshot(), before)

    def test_supported_result_versions_keep_saved_bytes_and_prior_fold(self):
        for version in ("0.1.0", "0.2.0"):
            with self.subTest(version=version):
                run_id = self.start()
                self.organ.advance(run_id, reply(self.organ.request(run_id)))
                directory = self.directory(run_id)
                for filename in ("intent.json", "result.json"):
                    self.change_version(directory / filename, version)
                originals = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
                result = self.organ.status(run_id)
                self.assertEqual(result["schema_version"], version)
                self.assertTrue(self.organ.inspect(run_id)["inspection"]["fresh"])
                next_run = self.start(previous=run_id)
                prior = self.organ.request(next_run)["prior_knowledge"]
                self.assertEqual(prior["fold"], result["fold"])
                self.assertEqual(prior["previous_status"], "UNRESOLVED")
                self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}, originals)

    def test_nonobject_state_is_rejected_as_a_format_error(self):
        run_id = self.start()
        (self.directory(run_id) / "intent.json").write_text("[]", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Unsupported Origami saved state schema_version"):
            self.organ.status(run_id)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
