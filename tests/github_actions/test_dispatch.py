"""Offline transport fixtures: no GitHub mutation, workflow, model or credentials."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[2] / "tools/github_actions/dispatch.py"
SPEC = importlib.util.spec_from_file_location("workflow_dispatch_adapter", MODULE)
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)
SHA, BLOB = "a" * 40, "b" * 40


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.sha, self.blob, self.private = SHA, BLOB, False
        self.full_name = "example/project"
        self.tag, self.annotated, self.branch = False, False, True
        self.post = dispatch.Response(200, {"workflow_run_id": 81,
            "run_url": "https://api.github.com/repos/example/project/actions/runs/81",
            "html_url": "https://github.com/example/project/actions/runs/81"})
        self.run = {"id": 81, "repository": {"id": 7}, "workflow_id": 9,
            "path": ".github/workflows/verify.yml@main",
            "event": "workflow_dispatch", "head_sha": SHA, "run_attempt": 1,
            "status": "queued", "conclusion": None}
        self.lookup_error = False

    def request(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, body))
        if method == "POST":
            if isinstance(self.post, BaseException):
                raise self.post
            return self.post
        if endpoint == "/repos/example/project":
            value = {"id": 7, "full_name": self.full_name, "private": self.private, "default_branch": "main"}
        elif "/git/ref/" in endpoint:
            kind = endpoint.split("/git/ref/")[1].split("/")[0]
            if not (self.branch if kind == "heads" else self.tag):
                return dispatch.Response(404)
            value = {"ref": "refs/" + kind + "/main", "object": {
                "type": "tag" if kind == "tags" and self.annotated else "commit",
                "sha": "c" * 40 if kind == "tags" and self.annotated else self.sha}}
        elif "/git/tags/" in endpoint:
            value = {"object": {"type": "commit", "sha": self.sha}}
        elif "/actions/workflows/" in endpoint:
            value = {"id": 9, "state": "active", "path": ".github/workflows/verify.yml"}
        elif "/contents/" in endpoint:
            value = {"type": "file", "path": ".github/workflows/verify.yml", "sha": self.blob}
        elif "/actions/runs/" in endpoint:
            if self.lookup_error:
                raise dispatch.TransportError("fixture lookup interrupted")
            value = self.run
        else:
            raise AssertionError(endpoint)
        return dispatch.Response(200, copy.deepcopy(value))

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "plan.json"
        self.transport = FakeTransport()
        self.request = {"repository": "example/project", "workflow": "verify.yml",
                        "ref": "main", "expected_sha": SHA, "inputs": {"checked": True}}

    def plan(self):
        result = dispatch.create_plan(self.request, self.root / "state", self.path, self.transport)
        return result["plan_sha256"]

    def execute(self, sha):
        return dispatch.execute_plan(self.path, sha, self.transport)

    def test_plan_is_read_only_and_does_not_initialize_intent(self):
        self.plan()
        self.assertFalse(self.transport.posts())
        self.assertFalse((self.root / "state").exists())

    def test_no_argument_cli_does_not_contact_github(self):
        with patch.object(dispatch.subprocess, "run") as run, patch("sys.stdout", io.StringIO()):
            self.assertEqual(dispatch.main([]), 0)
        run.assert_not_called()

    def test_plan_bytes_must_match_explicit_digest(self):
        sha = self.plan()
        self.path.write_bytes(self.path.read_bytes() + b" ")
        with self.assertRaises(ValueError): self.execute(sha)
        self.assertFalse(self.transport.posts())

    def test_plan_will_not_overwrite_an_existing_plan(self):
        self.plan()
        with self.assertRaises(FileExistsError): self.plan()
        self.assertFalse(self.transport.posts())

    def test_ref_or_workflow_change_stops_before_post(self):
        sha = self.plan()
        for field in ("sha", "blob"):
            with self.subTest(field=field):
                old = getattr(self.transport, field)
                setattr(self.transport, field, "d" * 40)
                with self.assertRaises(ValueError): self.execute(sha)
                setattr(self.transport, field, old)
        self.assertFalse(self.transport.posts())

    def test_private_repository_and_ambiguous_ref_are_rejected(self):
        self.transport.private = True
        with self.assertRaises(ValueError): self.plan()
        self.transport.private, self.transport.tag = False, True
        with self.assertRaises(ValueError): self.plan()
        self.assertFalse(self.transport.posts())

    def test_annotated_tag_resolves_to_commit(self):
        self.transport.branch, self.transport.tag, self.transport.annotated = False, True, True
        self.plan()
        self.assertEqual(dispatch.read(self.path)[1]["binding"]["ref_kind"], "tags")

    def test_duplicate_intent_never_posts_twice(self):
        sha = self.plan()
        result = self.execute(sha)
        self.assertEqual(result["dispatch"]["state"], "ACCEPTED_WITH_RUN_ID")
        self.assertEqual(result["observation"]["state"], "RUN_PENDING")
        self.assertFalse(result["observation"]["completed"])
        with self.assertRaises(FileExistsError): self.execute(sha)
        self.assertEqual(len(self.transport.posts()), 1)

    def test_ambiguous_post_preserves_intent_and_forbids_retry(self):
        sha = self.plan()
        self.transport.post = dispatch.TransportError("fixture connection lost after send")
        result = self.execute(sha)
        self.assertEqual(result["dispatch"]["state"], "POST_AMBIGUOUS")
        with self.assertRaises(FileExistsError): self.execute(sha)
        self.assertEqual(len(self.transport.posts()), 1)
        self.assertEqual(dispatch.inspect_intent(result["intent_directory"], self.transport)["state"], "POST_AMBIGUOUS")

    def test_crash_after_reservation_does_not_erase_intent(self):
        sha = self.plan()
        self.transport.post = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.execute(sha)
        directory = next((self.root / "state/intents").iterdir())
        self.assertEqual(dispatch.inspect_intent(directory, self.transport)["state"], "RESERVED_OUTCOME_UNKNOWN")
        with self.assertRaises(FileExistsError): self.execute(sha)
        self.assertEqual(len(self.transport.posts()), 1)

    def test_legacy204_is_accepted_unbound_without_recent_run_guess(self):
        sha = self.plan()
        self.transport.post = dispatch.Response(204)
        result = self.execute(sha)
        self.assertEqual(result["dispatch"]["state"], "ACCEPTED_UNBOUND")
        self.assertFalse(any("/actions/runs/" in c[1] for c in self.transport.calls))

    def test_wrong_response_url_cannot_choose_a_run(self):
        sha = self.plan()
        self.transport.post.body["run_url"] = "https://example.invalid/steal"
        result = self.execute(sha)
        self.assertEqual(result["dispatch"]["state"], "ACCEPTED_UNBOUND")

    def test_canonical_repository_casing_keeps_returned_run_identity(self):
        self.transport.full_name = "Example/Project"
        for field in ("run_url", "html_url"):
            self.transport.post.body[field] = self.transport.post.body[field].replace("example/project", "Example/Project")
        result = self.execute(self.plan())
        self.assertEqual(result["dispatch"]["state"], "ACCEPTED_WITH_RUN_ID")
        self.assertEqual(result["observation"]["state"], "RUN_PENDING")

    def test_rejection_and_server_ambiguity_are_distinct(self):
        for status, expected in ((403, "REJECTED"), (429, "REJECTED"), (500, "POST_AMBIGUOUS"), (408, "POST_AMBIGUOUS")):
            with self.subTest(status=status):
                self.path = self.root / (str(status) + ".json")
                sha = self.plan()
                self.transport.post = dispatch.Response(status)
                self.assertEqual(self.execute(sha)["dispatch"]["state"], expected)

    def test_commit_workflow_event_repository_id_and_attempt_all_bind(self):
        sha = self.plan()
        for field, value in (("head_sha", "f" * 40), ("workflow_id", 8), ("event", "push"),
                             ("path", ".github/workflows/deploy.yml"),
                             ("repository", {"id": 6}), ("repository", None),
                             ("id", 82), ("run_attempt", 2), ("run_attempt", True)):
            with self.subTest(field=field):
                old = self.transport.run[field]
                self.transport.run[field] = value
                observed = dispatch.observe(dispatch.load_plan(self.path, sha), 81, self.transport)
                self.assertEqual(observed["state"], "RUN_BINDING_MISMATCH")
                self.assertFalse(observed["completed"])
                self.transport.run[field] = old

    def test_concurrent_execute_has_only_one_post(self):
        from concurrent.futures import ThreadPoolExecutor
        sha = self.plan()
        def attempt():
            try:
                return self.execute(sha)["dispatch"]["state"]
            except FileExistsError:
                return "DUPLICATE_BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(outcomes, ["ACCEPTED_WITH_RUN_ID", "DUPLICATE_BLOCKED"])
        self.assertEqual(len(self.transport.posts()), 1)

    def test_status_is_read_only_and_success_requires_completed_success(self):
        result = self.execute(self.plan())
        self.transport.run.update(status="completed", conclusion="failure")
        observation = dispatch.inspect_intent(result["intent_directory"], self.transport)
        self.assertTrue(observation["completed"])
        self.assertFalse(observation["successful"])
        self.transport.run["conclusion"] = "success"
        self.assertTrue(dispatch.inspect_intent(result["intent_directory"], self.transport)["successful"])
        self.assertEqual(len(self.transport.posts()), 1)

    def test_read_failure_after_acceptance_keeps_exact_id_for_later_status(self):
        sha = self.plan()
        self.transport.lookup_error = True
        result = self.execute(sha)
        self.assertEqual(result["observation"]["state"], "RUN_LOOKUP_PENDING")
        self.transport.lookup_error = False
        self.assertEqual(dispatch.inspect_intent(result["intent_directory"], self.transport)["state"], "RUN_PENDING")
        self.assertEqual(len(self.transport.posts()), 1)

    def test_transient_status_lookup_records_pending_without_resubmission(self):
        result = self.execute(self.plan())
        directory = Path(result["intent_directory"])
        before = set(directory.glob("observation-*.json"))
        self.transport.lookup_error = True
        observation = dispatch.inspect_intent(directory, self.transport)
        self.assertEqual(observation, {"state": "RUN_LOOKUP_PENDING", "completed": False})
        self.assertEqual(len(set(directory.glob("observation-*.json")) - before), 1)
        self.assertEqual(dispatch.read(directory / "dispatch.json")[1]["state"], "ACCEPTED_WITH_RUN_ID")
        self.assertEqual(len(self.transport.posts()), 1)

    def test_input_validation_blocks_paths_structures_and_nonfinite_values(self):
        for field, value in (("repository", "../.."), ("workflow", "../../workflow.yml"),
                             ("ref", "main;echo"), ("expected_sha", "main"),
                             ("inputs", {"x": [1]}), ("inputs", {"x": float("nan")})):
            with self.subTest(field=field, value=value):
                request = {**self.request, field: value}
                with self.assertRaises(ValueError): dispatch.validate_request(request)

    def test_gh_uses_argv_stdin_existing_auth_and_no_debug_output(self):
        response = b'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"workflow_run_id":81}'
        fixture = SimpleNamespace(stdout=response, stderr=b"private diagnostic", returncode=0)
        with patch.dict(os.environ, {"GH_DEBUG": "api", "GH_TOKEN": "fixture-only-token"}), patch.object(dispatch.subprocess, "run", return_value=fixture) as run:
            actual = dispatch.GhTransport().request("POST", "/repos/example/project/actions/workflows/9/dispatches", {"ref": "main"})
        args, options = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", options)
        self.assertNotIn("GH_DEBUG", options["env"])
        self.assertNotIn("fixture-only-token", " ".join(args[0]))
        self.assertEqual(options["env"]["GH_TOKEN"], "fixture-only-token")
        self.assertEqual(json.loads(options["input"]), {"ref": "main"})
        self.assertEqual(actual.status, 200)

    def test_cli_failure_after_success_headers_is_ambiguous(self):
        fixture = SimpleNamespace(stdout=b'HTTP/2.0 200 OK\n\n{}', stderr=b"", returncode=1)
        with patch.object(dispatch.subprocess, "run", return_value=fixture), self.assertRaises(dispatch.TransportError):
            dispatch.GhTransport().request("POST", "/repos/example/project/actions/workflows/9/dispatches", {})


if __name__ == "__main__":
    unittest.main()
