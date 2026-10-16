"""Behavioral checks of evidence choice, persistence, refusal and real consumers.

Provider responses below are scripted test fixtures, not inference telemetry or
independent judgments. No frozen laboratory experiment is used as a fixture.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from lodestone_machinery import Origami
from lodestone_machinery import yggdrasil_runtime as yr
from lodestone_machinery.origami import _lines, VERSION
from lodestone_machinery.origami_cli import CommandProvider
from lodestone_machinery.usage import normalize_usage, usage_totals


def decision(request, kind="UNRESOLVED"):
    return {"request_id": request["request_id"], "decision": kind,
            "claims": [], "relations": [], "alternatives": [], "answer": None,
            "answer_claim_ids": [], "next_check": None,
            "coverage_assumptions": ["Only the explicitly supplied example documents are considered."],
            "unresolved": []}


FOUR_ROLES = {"constructor": "model-a", "consumer": "orchestrator", "checker": "auditor", "provider": "model-a"}
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "origami"


def envelope(response, usage=None):
    return {"provider": "scripted unit-test fixture", "model": "NONE",
            "response_id": None, "usage": usage, "response": response}


def claim(name, passage, value, epoch="current"):
    return {"id": name, "subject": "dispatch.label", "value": value,
            "scope": "example dispatch", "epoch": epoch, "basis": "RECORDED",
            "support": [passage["id"]]}


def answer(request, value="blue"):
    data = decision(request, "ANSWER")
    data["claims"] = [claim("current", request["evidence"][-1], value)]
    data["answer"] = "Use the " + value + " label."
    data["answer_claim_ids"] = ["current"]
    return data


class OrigamiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        self.path = self.root / "dispatch.md"
        self.path.write_bytes(b"Old checkpoint: dispatch uses the red label.\n\nThe current decision supersedes the checkpoint: use the blue label.\n")
        self.workspace = self.base / "workspace"
        self.organ = Origami(self.workspace)

    def start(self, **kwargs):
        # Hand-carried encounters bind to a state only with a declared provider; these tests declare the four chairs.
        kwargs.setdefault("roles", dict(FOUR_ROLES))
        return self.organ.start(self.root, ["dispatch.md"], "Which dispatch label applies?",
                                "example-current", "unit test author: read sources only", **kwargs)["run_id"]

    def read_more(self, request, start=3, end=3):
        data = decision(request, "NEED_EVIDENCE")
        data["next_check"] = {"source": "dispatch.md", "start_line": start, "end_line": end,
                              "purpose": "Distinguish the old checkpoint from its governing decision",
                              "outcomes": ["Explicit supersession changes the current label", "No supersession leaves the answer unresolved"]}
        return data

    def test_selected_read_changes_real_answer_and_relation_artifact(self):
        run_id = self.start(initial_passages=1)
        initial = self.organ.request(run_id)
        self.assertEqual(initial["evidence"][0]["start_line"], 1)
        state = self.organ.advance(run_id, envelope(self.read_more(initial)))
        self.assertEqual(state["status"], "NEEDS_PROVIDER")
        second = self.organ.request(run_id)
        self.assertIn("blue", second["evidence"][-1]["text"])
        reply = answer(second)
        reply["claims"].append(claim("old", initial["evidence"][0], "red", "old"))
        reply["relations"] = [{"id": "precedence", "type": "supersedes", "from": "current", "to": "old",
                                "support": [second["evidence"][-1]["id"]]}]
        done = self.organ.advance(run_id, envelope(reply))
        artifact = Path(done["artifact"]).read_text()
        self.assertEqual(done["status"], "ANSWER")
        self.assertIn("Use the blue label.", artifact)
        self.assertIn("current --supersedes--> old", artifact)
        self.assertEqual(done["observations"][-2]["kind"], "read_passage")
        self.assertIsNone(done["usage"]["counts"]["total_tokens"])

    def test_fabricated_citation_cannot_become_answer(self):
        run_id = self.start()
        reply = answer(self.organ.request(run_id))
        reply["claims"][0]["support"] = ["ev-invented"]
        done = self.organ.advance(run_id, envelope(reply))
        self.assertEqual(done["status"], "INVALID")
        self.assertNotIn("Use the blue label.", Path(done["artifact"]).read_text())

    def test_same_answer_with_changed_relation_preserves_conflict(self):
        run_id = self.start()
        request = self.organ.request(run_id)
        reply = answer(request)
        reply["claims"].append(claim("other", request["evidence"][0], "red"))
        reply["relations"] = [{"id": "conflict", "type": "contradicts", "from": "current", "to": "other",
                                "support": [request["evidence"][0]["id"]]}]
        self.assertEqual(self.organ.advance(run_id, envelope(reply))["status"], "INVALID")
        run2 = self.start()
        request = self.organ.request(run2)
        reply["request_id"] = request["request_id"]
        reply.update(decision="CONFLICTING", answer=None, answer_claim_ids=[])
        done = self.organ.advance(run2, envelope(reply))
        self.assertEqual(done["status"], "CONFLICTING")
        self.assertIn('"red"', Path(done["artifact"]).read_text())
        self.assertIn('"blue"', Path(done["artifact"]).read_text())

    def test_omitting_conflict_label_does_not_hide_incompatible_values(self):
        run_id = self.start()
        request = self.organ.request(run_id)
        reply = answer(request)
        reply["claims"].append(claim("other", request["evidence"][0], "red"))
        self.assertEqual(self.organ.advance(run_id, envelope(reply))["status"], "INVALID")

    def test_stale_unselected_source_blocks_silent_fallback(self):
        run_id = self.start(initial_passages=1)
        request = self.organ.request(run_id)
        self.path.write_text(self.path.read_text().replace("blue", "green"))
        done = self.organ.advance(run_id, envelope(answer(request, "red")))
        self.assertEqual(done["status"], "UNAVAILABLE")
        self.assertIsNone(done["answer"])
        self.assertNotIn("Use the red label.", Path(done["artifact"]).read_text())

    def test_missing_source_is_unavailable(self):
        run_id = self.start()
        request = self.organ.request(run_id)
        self.path.unlink()
        self.assertEqual(self.organ.advance(run_id, envelope(answer(request)))["status"], "UNAVAILABLE")

    def test_scope_escape_and_symlink_escape_are_rejected(self):
        run_id = self.start()
        request = self.organ.request(run_id)
        reply = self.read_more(request)
        reply["next_check"]["source"] = "../private.txt"
        self.assertEqual(self.organ.advance(run_id, envelope(reply))["status"], "INVALID")
        secret = self.base / "private.txt"
        secret.write_text("private")
        self.path.unlink(); self.path.symlink_to(secret)
        with self.assertRaises(ValueError):
            self.start()

    def test_wrong_request_does_not_consume_current_slot(self):
        run_id = self.start()
        reply = answer(self.organ.request(run_id))
        reply["request_id"] = "different:0"
        with self.assertRaises(ValueError):
            self.organ.advance(run_id, envelope(reply))
        self.assertEqual(self.organ.status(run_id)["status"], "NEEDS_PROVIDER")

    def test_call_and_byte_limits_stop_before_additional_read(self):
        for parameters in ({"max_calls": 1}, {"max_read_bytes": 1024}):
            if "max_read_bytes" in parameters:
                self.path.write_text("Old checkpoint: red.\n\n" + "blue " * 300 + "\n")
            run_id = self.start(initial_passages=1, **parameters)
            done = self.organ.advance(run_id, envelope(self.read_more(self.organ.request(run_id))))
            self.assertEqual(done["status"], "BUDGET_EXHAUSTED")
            self.assertFalse(any(x["kind"] == "read_passage" for x in done["observations"]))

    def test_repeated_read_stops_instead_of_spending_calls(self):
        run_id = self.start(initial_passages=1)
        reply = self.read_more(self.organ.request(run_id), 1, 1)
        self.assertEqual(self.organ.advance(run_id, envelope(reply))["status"], "UNRESOLVED")

    def test_live_alternative_and_unknown_support_cannot_resolve(self):
        run_id = self.start()
        reply = answer(self.organ.request(run_id))
        reply["alternatives"] = [{"id": "uncertain", "state": "LIVE", "claim_ids": ["current"],
                                   "description": "A different label may govern", "reason": "No deciding evidence"}]
        self.assertEqual(self.organ.advance(run_id, envelope(reply))["status"], "INVALID")
        run2 = self.start()
        reply = answer(self.organ.request(run2))
        reply["claims"][0]["basis"] = "UNKNOWN"
        self.assertEqual(self.organ.advance(run2, envelope(reply))["status"], "INVALID")

    def test_fresh_process_reentry_flags_new_epoch_and_changed_source(self):
        run_id = self.start()
        done = self.organ.advance(run_id, envelope(answer(self.organ.request(run_id))))
        old_bytes = (self.workspace / "origami" / run_id / "result.json").read_bytes()
        self.path.write_text(self.path.read_text().replace("blue", "green"))
        second = Origami(self.workspace)
        result = second.start(self.root, ["dispatch.md"], "Which label now?", "next-epoch", "test read authority", previous=run_id)
        prior = second.request(result["run_id"])["prior_knowledge"]
        self.assertEqual(prior["status"], "NEEDS_REVIEW")
        self.assertEqual(prior["changed_sources"], ["dispatch.md"])
        self.assertTrue(prior["epoch_changed"])
        self.assertEqual(second.inspect(run_id)["status"], "NEEDS_REVIEW")
        self.assertEqual(old_bytes, (self.workspace / "origami" / run_id / "result.json").read_bytes())

    def test_reentry_binding_drives_an_executed_read_and_new_artifact(self):
        first = self.start(initial_passages=0)
        self.organ.advance(first, envelope(self.read_more(self.organ.request(first))))
        self.organ.advance(first, envelope(answer(self.organ.request(first))))
        second = self.start(initial_passages=0, previous=first)
        chosen = []
        def returning_provider(request):
            if not request["evidence"]:
                prior = request["prior_knowledge"]
                support_id = prior["fold"]["claims"][0]["support"][0]
                binding = next(p for p in prior["bindings"] if p["id"] == support_id)
                chosen.append(binding["start_line"])
                return envelope(self.read_more(request, binding["start_line"], binding["end_line"]))
            return envelope(answer(request))
        done = Origami(self.workspace).run(second, returning_provider)
        self.assertEqual(chosen, [3])
        self.assertEqual(done["status"], "ANSWER")
        self.assertIn("Use the blue label.", Path(done["artifact"]).read_text())
        self.assertEqual(done["evidence"][0]["start_line"], 3)

    def test_artifact_corruption_is_exposed_without_repair(self):
        run_id = self.start()
        done = self.organ.advance(run_id, envelope(answer(self.organ.request(run_id))))
        Path(done["artifact"]).write_text("Incorrect replacement")
        inspected = self.organ.inspect(run_id)
        self.assertEqual(inspected["status"], "NEEDS_REVIEW")
        self.assertFalse(inspected["inspection"]["artifact_intact"])
        self.assertEqual(Path(done["artifact"]).read_text(), "Incorrect replacement")

    def test_three_edge_precedence_cycle_cannot_resolve(self):
        run_id = self.start()
        request = self.organ.request(run_id)
        reply = answer(request)
        p = request["evidence"][0]
        reply["claims"] = [claim(i, p, i) for i in ("a", "b", "c")]
        reply["relations"] = [{"id": a + b, "type": "supersedes", "from": a, "to": b,
                                "support": [p["id"]]} for a, b in (("a", "b"), ("b", "c"), ("c", "a"))]
        reply["answer_claim_ids"] = ["a"]
        self.assertEqual(self.organ.advance(run_id, envelope(reply))["status"], "INVALID")

    def test_lf_byte_handles_preserve_cr_and_unicode_separators(self):
        body = "alpha\r\nbeta\u2028gamma\rdelta\n"
        self.path.write_bytes(body.encode())
        run_id = self.start()
        request = self.organ.request(run_id)
        p = request["evidence"][0]
        self.assertEqual(p["text"], body)
        self.assertEqual(p["end_line"], 2)
        self.assertEqual(p["end_byte"], len(body.encode()))
        self.assertEqual(self.organ.advance(run_id, envelope(answer(request)))["status"], "ANSWER")

    def test_provider_loop_uses_actual_return_and_persists_usage(self):
        run_id = self.start(initial_passages=1)
        seen = []
        def provider(request):
            seen.append(request["step"])
            response = self.read_more(request) if request["step"] == 0 else answer(request)
            return envelope(response, {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                                       "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 0},
                                       "output_tokens_details": {"reasoning_tokens": 3}})
        done = self.organ.run(run_id, provider)
        self.assertEqual(seen, [0, 1])
        self.assertEqual(done["status"], "ANSWER")
        self.assertEqual(done["usage"]["counts"]["total_tokens"], 60)
        self.assertEqual(done["usage"]["counts"]["cached_input_tokens"], 10)
        self.assertEqual(done["usage"]["counts"]["reasoning_tokens"], 6)
        self.assertEqual(done["usage"]["counts"]["output_tokens"], 20)

    def test_provider_failure_does_not_expose_exception_or_replay(self):
        run_id = self.start()
        def broken(request):
            raise RuntimeError("SECRET_EXAMPLE_DO_NOT_PERSIST")
        done = self.organ.run(run_id, broken)
        self.assertEqual(done["status"], "PROVIDER_FAILED")
        self.assertNotIn("SECRET_EXAMPLE_DO_NOT_PERSIST", json.dumps(done))
        self.assertIsNone(done["usage"]["counts"]["input_tokens"])
        self.assertEqual(self.organ.run(run_id, lambda r: self.fail("must not replay"))["status"], "PROVIDER_FAILED")

    def test_interrupted_call_is_not_replayed(self):
        run_id = self.start()
        directory = self.workspace / "origami" / run_id
        (directory / "started-000.json").write_text("{}")
        self.assertEqual(self.organ.run(run_id, lambda r: self.fail("must not replay"))["status"], "UNCERTAIN")

    def test_sources_and_completed_runs_are_not_overwritten(self):
        before = self.path.read_bytes()
        run_id = self.start()
        request = self.organ.request(run_id)
        self.organ.advance(run_id, envelope(answer(request)))
        with self.assertRaises(ValueError):
            self.organ.advance(run_id, envelope(answer(request)))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            Origami(self.root / "outputs").start(self.root, ["dispatch.md"], "Q", "E", "test")

    def test_malformed_usage_is_unknown_with_raw_response_retained(self):
        run_id = self.start()
        done = self.organ.advance(run_id, envelope(answer(self.organ.request(run_id)), {"input_tokens": -1}))
        self.assertEqual(done["status"], "ANSWER")
        self.assertIsNotNone(done["calls"][0]["usage_error"])
        raw = json.loads((self.workspace / "origami" / run_id / "response-000.json").read_text())
        self.assertEqual(raw["usage"]["input_tokens"], -1)
        self.assertIsNone(done["usage"]["counts"]["input_tokens"])

    def test_cli_and_command_provider_are_live_consumers(self):
        run_id = self.start()
        runner = Path(__file__).resolve().parents[2] / "tools/machinery/run"
        request = self.organ.request(run_id)
        reply_path = self.base / "response.json"
        reply_path.write_text(json.dumps(envelope(answer(request))))
        result = subprocess.run([sys.executable, "-B", str(runner), "origami", "advance",
                                 "--workspace", str(self.workspace), run_id, "--response", str(reply_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "ANSWER")
        self.assertTrue(Path(output["artifact"]).is_file())
        script = "import json,sys; r=json.load(sys.stdin); print(json.dumps({'echo':r['request_id']}))"
        self.assertEqual(CommandProvider([sys.executable, "-c", script])(request), {"echo": request["request_id"]})


PROBE = """
import json, os, socket, subprocess, sys
request = json.load(sys.stdin)
marks = ["cwd=" + os.getcwd(), "env=" + ",".join(sorted(os.environ))]
attempts = (("root_write", lambda: open(os.path.join(%(root)r, "leash-probe.txt"), "w")),
            ("outside_write", lambda: open(os.path.join(%(outside)r, "leash-probe.txt"), "w")),
            ("network", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1)),
            ("subprocess", lambda: subprocess.run(["/bin/echo", "x"], capture_output=True, check=True)))
for name, action in attempts:
    try:
        action(); marks.append(name + "=allowed")
    except Exception as exc:
        marks.append(name + "=" + type(exc).__name__)
response = {"request_id": request["request_id"], "decision": "UNRESOLVED", "claims": [], "relations": [],
            "alternatives": [], "answer": None, "answer_claim_ids": [], "next_check": None,
            "coverage_assumptions": ["probe only"], "unresolved": marks}
print(json.dumps({"provider": "leash probe", "model": "NONE", "response_id": None, "usage": None, "response": response}))
"""

ENDPOINT_PROBE = """
import json, os, socket, sys
request = json.load(sys.stdin)
marks = []
proxy = os.environ.get("HTTPS_PROXY", "")
host, port = proxy.rsplit("//", 1)[-1].rsplit(":", 1)
def via_proxy(target):
    s = socket.create_connection((host, int(port)), timeout=3)
    s.sendall(("CONNECT %%s HTTP/1.1\\r\\nHost: %%s\\r\\n\\r\\n" %% (target, target)).encode())
    status = s.recv(200).decode("utf-8", "replace").split("\\r\\n")[0].split(" ", 1)[1]
    s.close()
    return status
for name, target in (("proxy_allowed", "127.0.0.1:%(allowed)d"), ("proxy_other", "127.0.0.1:%(other)d")):
    try:
        marks.append(name + "=" + via_proxy(target))
    except Exception as exc:
        marks.append(name + "=" + type(exc).__name__)
for name, target in (("direct_allowed", %(allowed)d), ("direct_other", %(other)d)):
    try:
        socket.create_connection(("127.0.0.1", target), timeout=2).close(); marks.append(name + "=connected")
    except Exception as exc:
        marks.append(name + "=" + type(exc).__name__)
response = {"request_id": request["request_id"], "decision": "UNRESOLVED", "claims": [], "relations": [],
            "alternatives": [], "answer": None, "answer_claim_ids": [], "next_check": None,
            "coverage_assumptions": ["probe only"], "unresolved": marks}
print(json.dumps({"provider": "endpoint probe", "model": "NONE", "response_id": None, "usage": None, "response": response}))
"""

READ_PROBE = """
import json, os, sys
request = json.load(sys.stdin)
marks = []
for name, path in (("auth_read", %(auth)r + "/token.txt"), ("sibling_read", %(sibling)r + "/other.txt"), ("source_read", %(root)r + "/dispatch.md")):
    try:
        open(path).read(1); marks.append(name + "=read")
    except Exception as exc:
        marks.append(name + "=" + type(exc).__name__)
try:
    open(%(auth)r + "/probe-write.txt", "w"); marks.append("auth_write=allowed")
except Exception as exc:
    marks.append("auth_write=" + type(exc).__name__)
response = {"request_id": request["request_id"], "decision": "UNRESOLVED", "claims": [], "relations": [],
            "alternatives": [], "answer": None, "answer_claim_ids": [], "next_check": None,
            "coverage_assumptions": ["probe only"], "unresolved": marks}
print(json.dumps({"provider": "read probe", "model": "NONE", "response_id": None, "usage": None, "response": response}))
"""

STATUSES = {"ENFORCED", "DECLARED", "ABSENT"}
CONSTRAINT_NAMES = ("read_only_source_root", "workspace_only_writes", "no_network", "no_lab_credentials",
                    "provider_auth_path", "no_further_subprocesses", "budget_stop")


class LeashTests(unittest.TestCase):
    """The provider program is launched with only what the Host scope allows, and the result says what held."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "sources"
        self.root.mkdir()
        (self.root / "dispatch.md").write_bytes(b"Old checkpoint: red.\n\nCurrent decision: blue.\n")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.workspace = self.base / "workspace"
        self.organ = Origami(self.workspace)
        self.script = self.base / "probe.py"
        self.script.write_text(PROBE % {"root": str(self.root), "outside": str(self.outside)})
        self.argv = [sys.executable, "-B", str(self.script)]

    def start(self, **kwargs):
        return self.organ.start(self.root, ["dispatch.md"], "Which label?", "epoch", "test read authority", **kwargs)["run_id"]

    def probe(self, provider):
        done = self.organ.run(self.start(), provider)
        self.assertEqual(done["status"], "UNRESOLVED", done.get("reason"))
        return done, dict(mark.split("=", 1) for mark in done["fold"]["unresolved"])

    def test_launched_command_runs_in_workspace_with_scrubbed_environment(self):
        os.environ["ORIGAMI_LEASH_SECRET"] = "must-not-cross"
        self.addCleanup(os.environ.pop, "ORIGAMI_LEASH_SECRET", None)
        done, marks = self.probe(self.argv)
        self.assertEqual(Path(marks["cwd"]).resolve(), self.workspace.resolve())
        passed = set(marks["env"].split(","))
        self.assertNotIn("ORIGAMI_LEASH_SECRET", passed)
        # macOS libSystem injects __CF_USER_TEXT_ENCODING into every new process; it is not passed by the runtime.
        self.assertLessEqual(passed - {"__CF_USER_TEXT_ENCODING"}, {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE"}, passed)
        self.assertEqual(done["provider_constraints"]["environment_allowlist"], "ENFORCED")
        expected = "ENFORCED" if done["provider_constraints"]["launchers"] == ["sandbox-exec"] else "DECLARED"
        self.assertEqual(done["provider_constraints"]["no_lab_credentials"], expected)

    def test_cli_command_provider_object_is_launched_under_the_same_leash(self):
        os.environ["ORIGAMI_LEASH_SECRET"] = "must-not-cross"
        self.addCleanup(os.environ.pop, "ORIGAMI_LEASH_SECRET", None)
        done, marks = self.probe(CommandProvider(self.argv))
        self.assertNotIn("ORIGAMI_LEASH_SECRET", marks["env"].split(","))
        self.assertEqual(Path(marks["cwd"]).resolve(), self.workspace.resolve())
        self.assertIn(done["provider_constraints"]["launchers"][0], ("sandbox-exec", "subprocess"))

    def test_provider_constraints_field_is_present_and_truthful(self):
        done, marks = self.probe(self.argv)
        constraints = done["provider_constraints"]
        for name in CONSTRAINT_NAMES:
            self.assertIn(constraints[name], STATUSES | {"ENFORCED_ENDPOINT_ONLY"}, name)
        self.assertEqual(constraints["budget_stop"], "ENFORCED")
        self.assertEqual(constraints["environment_allowlist"], "ENFORCED")
        self.assertIn("Provider constraints:", Path(done["artifact"]).read_text())
        kernel = ("read_only_source_root", "workspace_only_writes", "no_network", "no_further_subprocesses", "provider_auth_path", "no_lab_credentials")
        if constraints["launchers"] == ["sandbox-exec"]:
            for name in kernel:
                self.assertEqual(constraints[name], "ENFORCED", name)
            for probe in ("root_write", "outside_write", "network", "subprocess"):
                self.assertEqual(marks[probe], "PermissionError", probe)
            self.assertFalse((self.root / "leash-probe.txt").exists())
            self.assertFalse((self.outside / "leash-probe.txt").exists())
        else:
            self.assertEqual(constraints["launchers"], ["subprocess"])
            for name in kernel:
                self.assertEqual(constraints[name], "DECLARED", name)
        # An in-process callable and an external exchange are not launched at all; the record says so.
        run2 = self.start()
        done2 = self.organ.run(run2, lambda request: envelope(decision(request)))
        self.assertEqual(done2["provider_constraints"]["launchers"], ["callable"])
        self.assertEqual(done2["provider_constraints"]["no_lab_credentials"], "ABSENT")
        self.assertEqual(done2["provider_constraints"]["provider_auth_path"], "ABSENT")
        self.assertEqual(done2["provider_constraints"]["no_network"], "DECLARED")
        run3 = self.start()
        done3 = self.organ.advance(run3, envelope(decision(self.organ.request(run3))))
        self.assertEqual(done3["provider_constraints"]["launchers"], ["external_exchange"])
        self.assertEqual(done3["provider_constraints"]["budget_stop"], "ENFORCED")

    def listener(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        self.addCleanup(server.close)
        def serve():
            while True:
                try:
                    client, _ = server.accept()
                except OSError:
                    return
                client.sendall(b"HELLO\n")
                client.close()
        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()[1]

    def test_declared_endpoint_is_reachable_and_other_hosts_are_blocked(self):
        allowed, other = self.listener(), self.listener()
        script = self.base / "endpoint_probe.py"
        script.write_text(ENDPOINT_PROBE % {"allowed": allowed, "other": other})
        done = self.organ.run(self.start(), [sys.executable, "-B", str(script)], allowed_endpoints=[f"127.0.0.1:{allowed}"])
        self.assertEqual(done["status"], "UNRESOLVED", done.get("reason"))
        marks = dict(mark.split("=", 1) for mark in done["fold"]["unresolved"])
        constraints = done["provider_constraints"]
        self.assertEqual(constraints["allowed_endpoints"], [f"127.0.0.1:{allowed}"])
        self.assertTrue(marks["proxy_allowed"].startswith("200"), marks)
        self.assertTrue(marks["proxy_other"].startswith("403"), marks)
        if constraints["launchers"] == ["sandbox-exec"]:
            self.assertEqual(constraints["no_network"], "ENFORCED_ENDPOINT_ONLY")
            self.assertEqual(marks["direct_allowed"], "PermissionError")
            self.assertEqual(marks["direct_other"], "PermissionError")
        else:
            self.assertEqual(constraints["no_network"], "DECLARED")
        with self.assertRaises(ValueError):
            self.organ.run(self.start(), self.argv, allowed_endpoints=["*:443"])

    def test_provider_auth_path_is_readable_and_its_sibling_is_blocked(self):
        auth = self.base / "auth"; auth.mkdir(); (auth / "token.txt").write_text("secret-token")
        sibling = self.base / "sibling"; sibling.mkdir(); (sibling / "other.txt").write_text("other")
        script = self.base / "read_probe.py"
        script.write_text(READ_PROBE % {"auth": str(auth), "sibling": str(sibling), "root": str(self.root)})
        done = self.organ.run(self.start(), [sys.executable, "-B", str(script)], provider_auth_path=str(auth))
        self.assertEqual(done["status"], "UNRESOLVED", done.get("reason"))
        marks = dict(mark.split("=", 1) for mark in done["fold"]["unresolved"])
        constraints = done["provider_constraints"]
        self.assertEqual(constraints["provider_auth_path_value"], [str(auth.resolve())])
        self.assertEqual(marks["auth_read"], "read")
        self.assertEqual(marks["source_read"], "read")
        if constraints["launchers"] == ["sandbox-exec"]:
            self.assertEqual(constraints["provider_auth_path"], "ENFORCED")
            self.assertEqual(marks["sibling_read"], "PermissionError")
            self.assertEqual(marks["auth_write"], "PermissionError")
        else:
            self.assertEqual(constraints["provider_auth_path"], "DECLARED")
        with self.assertRaises(ValueError):
            self.organ.run(self.start(), self.argv, provider_auth_path=str(self.base / "missing"))

    def test_roles_are_recorded_and_two_equal_bodies_are_rejected(self):
        with self.assertRaises(ValueError):
            self.start(roles={"constructor": "model-a", "consumer": "orchestrator", "checker": "model-a"})
        with self.assertRaises(ValueError):
            self.start(roles={"constructor": "model-a", "consumer": "orchestrator"})
        with self.assertRaises(ValueError):
            self.start(roles={"constructor": "", "consumer": "orchestrator", "checker": "auditor"})
        run_id = self.start(roles=dict(FOUR_ROLES))
        done = self.organ.advance(run_id, envelope(decision(self.organ.request(run_id))))
        self.assertEqual(done["roles"], {**FOUR_ROLES, "separation": "DISTINCT"})
        self.assertIn('"checker": "auditor"', Path(done["artifact"]).read_text())
        bare = self.start()
        self.assertEqual(self.organ.advance(bare, envelope(decision(self.organ.request(bare))))["roles"]["separation"], "UNDECLARED")

    def test_cli_start_passes_roles_through_and_rejects_two_equal_bodies(self):
        runner = Path(__file__).resolve().parents[2] / "tools/machinery/run"
        base = [sys.executable, "-B", str(runner), "origami", "start", "--workspace", str(self.workspace),
                "--root", str(self.root), "--source", "dispatch.md", "--question", "Which label?",
                "--epoch", "epoch", "--authority-ref", "test read authority"]
        rejected = subprocess.run(base + ["--constructor", "model-a", "--consumer", "orchestrator", "--checker", "model-a",
                                          "--provider", "model-a"], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("three distinct bodies", rejected.stderr)
        three = subprocess.run(base + ["--constructor", "model-a", "--consumer", "orchestrator", "--checker", "model-a"],
                               capture_output=True, text=True)
        self.assertEqual(three.returncode, 2)
        self.assertEqual(list((self.workspace / "origami").iterdir()) if (self.workspace / "origami").exists() else [], [])
        partial = subprocess.run(base + ["--constructor", "model-a"], capture_output=True, text=True)
        self.assertEqual(partial.returncode, 2)
        started = subprocess.run(base + ["--constructor", "model-a", "--consumer", "orchestrator", "--checker", "auditor",
                                         "--provider", "model-a"], capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        done = self.organ.advance(run_id, envelope(decision(self.organ.request(run_id))))
        self.assertEqual(done["roles"], {**FOUR_ROLES, "separation": "DISTINCT"})


class FourthChairTests(unittest.TestCase):
    """The provider gets a name and a state: who answered, how the answer arrived, and BLOCKED when neither is on record."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "sources"
        self.root.mkdir()
        (self.root / "dispatch.md").write_bytes(b"Old checkpoint: red.\n\nCurrent decision: blue.\n")
        self.workspace = self.base / "workspace"
        self.organ = Origami(self.workspace)

    def start(self, **kwargs):
        return self.organ.start(self.root, ["dispatch.md"], "Which label?", "epoch", "test read authority", **kwargs)["run_id"]

    def runs(self):
        return list((self.workspace / "origami").iterdir()) if (self.workspace / "origami").exists() else []

    def test_four_roles_accepted_and_three_still_rejected_as_all_or_none(self):
        run_id = self.start(roles=dict(FOUR_ROLES))
        self.assertEqual(self.organ.status(run_id)["status"], "NEEDS_PROVIDER")
        with self.assertRaises(ValueError):
            self.start(roles={"constructor": "model-a", "consumer": "orchestrator", "checker": "auditor"})
        with self.assertRaises(ValueError):
            self.start(roles={"provider": "model-a"})
        self.assertEqual(len(self.runs()), 1)

    def test_provider_equal_to_checker_is_rejected_before_a_run_directory_exists(self):
        with self.assertRaises(ValueError) as refused:
            self.start(roles={**FOUR_ROLES, "provider": "auditor"})
        self.assertIn("checker", str(refused.exception))
        self.assertEqual(self.runs(), [])

    def test_provider_equal_to_constructor_is_accepted(self):
        run_id = self.start(roles={**FOUR_ROLES, "constructor": "model-a", "provider": "model-a"})
        done = self.organ.advance(run_id, envelope(decision(self.organ.request(run_id))))
        self.assertEqual(done["roles"]["provider"], done["roles"]["constructor"])
        self.assertEqual(done["roles"]["separation"], "DISTINCT")

    def test_cli_provider_flag_is_all_or_none_with_the_existing_exit_code(self):
        runner = Path(__file__).resolve().parents[2] / "tools/machinery/run"
        base = [sys.executable, "-B", str(runner), "origami", "start", "--workspace", str(self.workspace),
                "--root", str(self.root), "--source", "dispatch.md", "--question", "Which label?",
                "--epoch", "epoch", "--authority-ref", "test read authority"]
        three = subprocess.run(base + ["--constructor", "model-a", "--consumer", "orchestrator", "--checker", "auditor"],
                               capture_output=True, text=True)
        self.assertEqual(three.returncode, 2, three.stdout)
        alone = subprocess.run(base + ["--provider", "model-a"], capture_output=True, text=True)
        self.assertEqual(alone.returncode, 2, alone.stdout)
        grader = subprocess.run(base + ["--constructor", "model-a", "--consumer", "orchestrator",
                                        "--checker", "auditor", "--provider", "auditor"], capture_output=True, text=True)
        self.assertEqual(grader.returncode, 2, grader.stdout)
        self.assertEqual(self.runs(), [])
        four = subprocess.run(base + ["--constructor", "model-a", "--consumer", "orchestrator",
                                      "--checker", "auditor", "--provider", "model-a"], capture_output=True, text=True)
        self.assertEqual(four.returncode, 0, four.stderr)
        run_id = json.loads(four.stdout)["run_id"]
        self.assertEqual(self.organ.status(run_id)["status"], "NEEDS_PROVIDER")

    def test_program_encounter_records_transport_program(self):
        done = self.organ.run(self.start(), lambda request: envelope(decision(request)))
        self.assertEqual(done["transport"], "PROGRAM")
        self.assertEqual(done["status"], "UNRESOLVED")
        self.assertIsNone(done["provider_identity"])
        self.assertIn("Transport: \"PROGRAM\"", Path(done["artifact"]).read_text())

    def test_hand_carried_encounter_with_declared_provider_terminates_normally(self):
        run_id = self.start(roles=dict(FOUR_ROLES))
        done = self.organ.advance(run_id, envelope(decision(self.organ.request(run_id))))
        self.assertEqual(done["transport"], "HAND_CARRIED")
        self.assertEqual(done["status"], "UNRESOLVED")
        self.assertEqual(done["provider_identity"], "model-a")
        self.assertEqual(done["schema_version"], VERSION)

    def test_hand_carried_encounter_without_declared_provider_is_blocked_not_rejected(self):
        run_id = self.start()
        done = self.organ.advance(run_id, envelope(decision(self.organ.request(run_id))))
        self.assertEqual((done["status"], done["reason"]), ("BLOCKED", "PROVIDER_UNDECLARED"))
        self.assertNotEqual(done["status"], "REJECTED")
        self.assertEqual(done["transport"], "HAND_CARRIED")
        self.assertIsNone(done["provider_identity"])
        self.assertIsNone(done["answer"])
        self.assertEqual(done["fold"]["decision"], "UNRESOLVED")
        self.assertEqual(self.organ.status(run_id)["status"], "BLOCKED")
        with self.assertRaises(ValueError):
            self.organ.advance(run_id, envelope(decision(self.organ.request(run_id))))
        # A read request from an undeclared provider is not performed: the encounter ends here.
        second = self.start()
        request = self.organ.request(second)
        asked = decision(request, "NEED_EVIDENCE")
        asked["next_check"] = {"source": "dispatch.md", "start_line": 3, "end_line": 3, "purpose": "distinguish",
                               "outcomes": ["one consequence", "another consequence"]}
        blocked = self.organ.advance(second, envelope(asked))
        self.assertEqual((blocked["status"], blocked["reason"]), ("BLOCKED", "PROVIDER_UNDECLARED"))
        self.assertFalse((self.workspace / "origami" / second / "request-001.json").exists())

    def test_committed_three_role_result_without_transport_still_validates(self):
        # A real saved result from SF-01 (run 08b44cc0, branch exp/origami-selffold-sf01-run, blob ac29b925), byte-identical.
        if not (FIXTURES / "sf01-consumer-result-0.1.0.json").is_file():
            self.skipTest("SF-01 saved result is lab custody; it travels with the private tree, not the clean export")
        saved = json.loads((FIXTURES / "sf01-consumer-result-0.1.0.json").read_text())
        self.assertEqual(saved["schema_version"], "0.1.0")
        self.assertNotEqual(saved["schema_version"], VERSION)
        self.assertEqual(set(saved["roles"]), {"constructor", "consumer", "checker", "separation"})
        self.assertNotIn("transport", saved)
        self.assertNotIn("provider_identity", saved)
        directory = self.workspace / "origami" / saved["run_id"]
        directory.mkdir(parents=True)
        for name in ("intent", "result"):
            (directory / (name + ".json")).write_bytes((FIXTURES / f"sf01-consumer-{name}-0.1.0.json").read_bytes())
        self.assertEqual(self.organ.status(saved["run_id"]), saved)
        inspected = self.organ.inspect(saved["run_id"])
        self.assertEqual(inspected["inspection"]["recorded_status"], "CONFLICTING")
        prior = self.organ.request(self.start(previous=saved["run_id"], roles=dict(FOUR_ROLES)))["prior_knowledge"]
        self.assertEqual(prior["previous_status"], "CONFLICTING")
        self.assertEqual(prior["fold"], saved["fold"])
        from test_macro_voxel import fixture as canon_fixture
        canon = self.base / "canon"
        canon.mkdir()
        canon_fixture(canon)
        voxel = yr.snapshot(canon, origami_results=[directory / "result.json"])
        self.assertTrue(any(n["role"] == "origami_result" for n in mv_decode(voxel)["nodes"]))


def mv_decode(voxel):
    from lodestone_machinery import macro_voxel
    return macro_voxel.decode(voxel)


class UsageTests(unittest.TestCase):
    def test_missing_is_not_zero_and_totals_do_not_double_count(self):
        one = normalize_usage({"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                               "input_tokens_details": {"cached_tokens": 80},
                               "output_tokens_details": {"reasoning_tokens": 20}})
        total = usage_totals([{"usage": one}, {"usage": normalize_usage(None)}])
        self.assertIsNone(total["counts"]["total_tokens"])
        self.assertEqual(total["known_subtotals"]["total_tokens"], 130)
        self.assertEqual(total["missing_calls"]["total_tokens"], 1)
        self.assertEqual(one["counts"]["output_tokens"], 30)

    def test_invalid_and_inconsistent_counters_are_rejected(self):
        for raw in ({"input_tokens": True}, {"input_tokens": -1},
                    {"input_tokens": 4, "cached_input_tokens": 5},
                    {"output_tokens": 4, "reasoning_tokens": 5},
                    {"input_tokens": 5, "output_tokens": 5, "total_tokens": 12},
                    {"cached_input_tokens": 4, "input_tokens_details": {"cached_tokens": 3}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_usage(raw)


if __name__ == "__main__":
    unittest.main()
