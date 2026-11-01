"""Pando custody, attribution, organ composition, and loopback API checks.

All data is a disposable fixture. These tests do not read host memories,
call a provider, run a scientific trial, or restore a Saṃsāra archive.
"""
import base64
import contextlib
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile
import threading
import unittest

from lodestone_pando.__main__ import main
from lodestone_pando.server import make_server
from lodestone_pando.store import Board


class PandoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pando checks ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "board"
        self.board = Board(self.home)
        self.raw = b"Probe score = effort + gain.\r\nFunction travels; identity does not.\n"
        self.source = self.board.ingest(self.raw, title="Fixture", uri="fixture://probe/v1",
                                        author="host", kind="host")

    def test_user_participants_are_persistent_and_do_not_grant_authority(self):
        people = [{"id": "host", "name": "Owner", "role": "Approver"},
                  {"id": "local-author", "name": "Writer", "role": "Contributor"}]
        path = self.base / "custom-board"
        board = Board(path, participants=people)
        self.assertEqual(Board(path).state()["participants"], people)
        sid = board.ingest(b"Local source", title="Local", uri="fixture://custom", author="local-author")
        self.assertEqual(board.source(sid)["author"], "local-author")
        with self.assertRaisesRegex(ValueError, "Unknown participant"):
            board.post(author="operator", shelf="commons", title="No", body="Undeclared")
        with self.assertRaisesRegex(ValueError, "differs"):
            Board(path, participants=people + [{"id": "other", "name": "Other", "role": "Reader"}])
        self.assertEqual(Board(path).state()["participants"], people)
        for invalid in ([], people[1:], people *2, [{"id": "host", "name": "", "role": "x"}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Board(self.base / "invalid-board", participants=invalid)
        self.assertFalse((self.base / "invalid-board").exists())

    def request(self):
        return self.board.eat([self.source], author="operator",
                              question="Extract the declared score sign.", title="Score request")

    def proposal(self, request):
        return {
            "schema": "kernel-digestion/v1", "id": "score-parent", "actor": "operator",
            "request_handle": self.board.artifact(request)["handle"],
            "operators": {"probe_gain_sign": {"value": 1, "source": self.source + ".txt",
                "quote": "Probe score = effort + gain.", "rationale": "The written score adds gain."}},
            "instructions": [], "rejected": [], "uncertainties": ["Fixture only; utility is unmeasured."],
        }

    def digest(self):
        request = self.request()
        return self.board.digest(request, self.proposal(request), author="operator", title="Score candidate")

    def mutagen(self):
        return {"schema": "kernel-mutagen/v1", "id": "score-sign", "new_id": "score-child",
                "operator": "probe_gain_sign", "expected": 1, "replacement": -1,
                "rationale": "An explicit candidate change, not a measured improvement."}

    def view_candidate(self, operators=None):
        operators = operators if operators is not None else {"board_name": "Fixture Pando", "enabled_shelves": ["commons"]}
        raw = json.dumps(operators, sort_keys=True).encode()
        source = self.board.ingest(raw, title="View charter", uri="fixture://view/" + hashlib.sha256(raw).hexdigest(),
                                  author="host", kind="host")
        request = self.board.eat([source], author="host", question="Copy the declared view fields.", title="View request")
        proposal = {"schema": "kernel-digestion/v1", "id": "view-candidate", "actor": "host",
                    "request_handle": self.board.artifact(request)["handle"],
                    "operators": {name: {"value": value, "source": source + ".txt", "quote": raw.decode(),
                                           "rationale": "Explicit disposable view fixture."}
                                  for name, value in operators.items()},
                    "instructions": [], "rejected": [], "uncertainties": []}
        return self.board.digest(request, proposal, author="host", title="View candidate")

    def test_duplicate_import_is_idempotent_but_revision_retains_both_sources(self):
        again = self.board.ingest(self.raw, title="Repeated fetch", uri="fixture://probe/v1",
                                  author="operator", kind="notion")
        revised = self.board.ingest(self.raw + b"Revision two.\n", title="Revision",
                                    uri="fixture://probe/v1", author="host", kind="host")
        self.assertEqual(again, self.source)
        self.assertNotEqual(revised, self.source)
        self.assertEqual(self.board.source(self.source)["text"].encode(), self.raw)
        self.assertEqual(self.board.source(self.source)["sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(self.board.state()["stats"]["sources"], 2)
        self.assertEqual(self.board.source(self.source)["author"], "host")

    def test_source_hash_drift_fails_on_read_and_verify(self):
        with self.board.connection() as db:
            db.execute("UPDATE sources SET body=? WHERE id=?", (b"changed", self.source))
        for operation in (lambda: self.board.source(self.source), self.board.verify):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                operation()

    def test_import_rejects_non_utf8_and_undeclared_actor(self):
        for raw, actor in ((b"\xff", "host"), (b"body", "unregistered-model")):
            with self.assertRaises(ValueError):
                self.board.ingest(raw, title="Rejected", uri="fixture://bad", author=actor)
        self.assertEqual(self.board.state()["stats"]["sources"], 1)

    def test_mailbox_hash_size_encoding_and_message_kind_checked(self):
        envelope = {"schema": "lodestone-mailbox/v1", "kind": "file", "to": "cloud",
                    "size": len(self.raw), "sha256": hashlib.sha256(self.raw).hexdigest(),
                    "payload_b64": base64.b64encode(self.raw).decode()}
        imported = self.board.import_mailbox(envelope, title="Mail fixture", uri="mailbox://fixture",
                                             author="reviewer")
        self.assertEqual(self.board.source(imported)["text"].encode(), self.raw)
        for key, value in (("sha256", "0" * 64), ("size", len(self.raw) + 1),
                           ("size", True), ("kind", "job"), ("payload_b64", "%%%")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.board.import_mailbox({**envelope, key: value}, title="Bad mail",
                                          uri="mailbox://bad", author="reviewer")
        self.assertEqual(envelope["kind"], "file")
        self.assertEqual(self.board.state()["stats"]["sources"], 2)

    def test_posts_keep_source_references_and_same_shelf_reply_lineage(self):
        parent = self.board.post(author="host", shelf="commons", title="Question", body="Discuss this.",
                                  source_ids=[self.source])
        reply = self.board.post(author="reviewer", shelf="commons", title="Re: Question",
                                 body="An attributed reply.", parent_id=parent, source_ids=[self.source])
        rows = {p["id"]: p for p in self.board.state()["posts"]}
        self.assertEqual(rows[reply]["parent_id"], parent)
        self.assertEqual(rows[reply]["source_ids"], [self.source])
        for kwargs in ({"shelf": "research", "parent_id": parent}, {"source_ids": ["missing"]},
                       {"source_ids": [self.source, self.source]}, {"parent_id": "missing"}):
            args = {"author": "host", "shelf": "commons", "title": "Bad reply", "body": "No publish."}
            args.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.board.post(**args)
        self.assertEqual(self.board.state()["stats"]["posts"], 2)

    def test_eat_digest_mutate_preserves_bytes_attribution_and_parent_records(self):
        request = self.request()
        request_item = self.board.artifact(request)
        ingestion = self.board.plumber.records.read(request_item["record"]["payload"]["ingest_handle"])
        self.assertEqual(ingestion["payload"]["sources"][0]["text"].encode(), self.raw)
        candidate = self.board.digest(request, self.proposal(request), author="operator", title="Candidate")
        parent = self.board.artifact(candidate)
        parent_bytes = self.board.plumber.records.path(parent["handle"]).read_bytes()
        child_id = self.board.mutate(candidate, self.mutagen(), author="host", title="Variant")
        child = self.board.artifact(child_id)
        self.assertEqual(parent["parent_id"], request)
        self.assertEqual(child["parent_id"], candidate)
        self.assertEqual(child["source_ids"], [self.source])
        self.assertEqual(child["record"]["payload"]["profile"]["operators"], {"probe_gain_sign": -1})
        self.assertEqual(child["record"]["payload"]["profile"]["lineage"]["parent_record"], parent["handle"])
        self.assertEqual(parent["record"]["payload"]["profile"]["lineage"]["actor"], "operator")
        self.assertEqual(self.board.plumber.records.path(parent["handle"]).read_bytes(), parent_bytes)
        self.assertEqual(self.board.source(self.source)["text"].encode(), self.raw)
        self.assertEqual(child["record"]["payload"]["fitness"], "UNMEASURED")
        self.assertEqual(child["record"]["payload"]["activation"], "NONE")
        self.assertFalse(child["record"]["payload"]["source_instructions_executed"])
        self.assertEqual(self.board.verify()["status"], "INTACT")

    def test_digest_rejects_wrong_actor_fabricated_quote_and_wrong_request(self):
        request = self.request()
        proposal = self.proposal(request)
        cases = []
        for key, value in (("actor", "reviewer"), ("request_handle", "eat:" + "0" * 64)):
            wrong = copy.deepcopy(proposal); wrong[key] = value; cases.append(wrong)
        wrong = copy.deepcopy(proposal)
        wrong["operators"]["probe_gain_sign"]["quote"] = "This was never in the source."
        cases.append(wrong)
        for wrong in cases:
            with self.subTest(proposal=wrong), self.assertRaises(ValueError):
                self.board.digest(request, wrong, author="operator", title="Rejected")
        self.assertEqual(self.board.state()["stats"]["artifacts"], 1)

    def test_mutate_rejects_bool_as_int_wrong_expected_noop_and_wrong_stage(self):
        candidate = self.digest()
        for key, value in (("expected", True), ("expected", 9), ("replacement", 1), ("operator", "unknown")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.board.mutate(candidate, {**self.mutagen(), key: value}, author="host", title="Rejected")
        with self.assertRaisesRegex(ValueError, "requires a digested"):
            self.board.mutate(self.request(), self.mutagen(), author="host", title="Rejected")
        self.assertNotIn("MUTATE", [a["stage"] for a in self.board.state()["artifacts"]])

    def test_changed_capture_cannot_be_silently_reused(self):
        self.request()
        path = self.home / "captures" / (self.source + ".txt")
        path.chmod(0o600); path.write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "capture drift"):
            self.request()

    def test_explicit_view_application_consumes_only_named_fields_and_keeps_candidate(self):
        candidate = self.view_candidate()
        mutagen = {"schema": "kernel-mutagen/v1", "id": "add-shelf", "new_id": "view-child",
                   "operator": "enabled_shelves", "expected": ["commons"], "replacement": ["commons", "memory"],
                   "rationale": "Include the memory shelf for this disposable board."}
        child_id = self.board.mutate(candidate, mutagen, author="host", title="View with memory")
        child = self.board.artifact(child_id)
        before = self.board.plumber.records.path(child["handle"]).read_bytes()
        application = self.board.apply_profile(child_id, author="host")
        self.assertEqual(application["status"], "APPLIED_VIEW")
        self.assertEqual(self.board.state()["name"], "Fixture Pando")
        self.assertEqual(self.board.state()["shelves"], ["commons", "memory"])
        self.assertEqual(self.board.state()["view"]["artifact_id"], child_id)
        self.assertEqual(self.board.state()["view"]["author"], "host")
        record = self.board.plumber.records.read(application["handle"])
        self.assertIn(child["handle"], record["links"])
        self.assertIsNone(record["payload"]["previous"])
        self.assertIsNone(record["payload"]["previous_application"])
        second = self.board.apply_profile(candidate, author="operator")
        second_record = self.board.plumber.records.read(second["handle"])
        self.assertEqual(second_record["payload"]["previous"], application["view"])
        self.assertEqual(second_record["payload"]["previous_application"], application["handle"])
        self.assertIn(application["handle"], second_record["links"])
        self.assertEqual(self.board.plumber.records.path(child["handle"]).read_bytes(), before)
        self.assertEqual(child["record"]["payload"]["activation"], "NONE")
        self.assertEqual(self.board.verify()["status"], "INTACT")

    def test_rejected_view_profile_cannot_change_existing_view_or_enable_new_operations(self):
        self.board.apply_profile(self.view_candidate(), author="host")
        before = self.board.settings()
        defaults = {"board_name": "Do not apply", "enabled_shelves": ["commons"]}
        invalid = [{**defaults, "execute": "shell"},
                   {**defaults, "enabled_shelves": ["unknown"]},
                   {**defaults, "enabled_shelves": ["commons", "commons"]},
                   {**defaults, "enabled_shelves": []},
                   {**defaults, "enabled_shelves": "commons"},
                   {**defaults, "board_name": 4}]
        for operators in invalid:
            with self.subTest(operators=operators):
                candidate = self.view_candidate(operators)
                with self.assertRaises(ValueError):
                    self.board.apply_profile(candidate, author="host")
                self.assertEqual(self.board.settings(), before)
        with self.assertRaises(ValueError):
            self.board.apply_profile(self.request(), author="host")
        self.assertEqual(self.board.settings(), before)
        self.assertEqual(self.board.verify()["status"], "INTACT")

    def test_export_contains_database_sources_and_all_reachable_organ_bytes(self):
        candidate = self.digest()
        child = self.board.mutate(candidate, self.mutagen(), author="host", title="Child")
        self.board.post(author="host", shelf="memory", title="Keep lineage", body="A fixture.", source_ids=[self.source])
        output = self.base / "departure.tar.gz"
        with contextlib.redirect_stdout(io.StringIO()) as capture:
            result = main(["--home", str(self.home), "export", "--output", str(output)])
        self.assertEqual(result, 0)
        receipt = json.loads(capture.getvalue())
        self.assertEqual(receipt["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        with tarfile.open(output, "r:gz") as archive:
            database = archive.extractfile("pando/pando.sqlite3").read()
            temporary_database = self.base / "snapshot.sqlite3"
            temporary_database.write_bytes(database)
            with sqlite3.connect(temporary_database) as db:
                self.assertEqual(db.execute("SELECT body FROM sources WHERE id=?", (self.source,)).fetchone()[0], self.raw)
                self.assertEqual(db.execute("SELECT count(*) FROM posts").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 3)
            checked, pending = set(), [self.board.artifact(child)["handle"]]
            while pending:
                handle = pending.pop()
                if handle in checked:
                    continue
                checked.add(handle)
                path = self.board.plumber.records.path(handle)
                member = "pando/kernel/" + path.relative_to(self.board.home / "kernel").as_posix()
                archived = archive.extractfile(member).read()
                self.assertEqual(archived, path.read_bytes())
                pending.extend(json.loads(archived)["links"])
            self.assertEqual(len(checked), 4)
            self.assertEqual(archive.extractfile("pando/captures/" + self.source + ".txt").read(), self.raw)
        before = output.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "export", "--output", str(output)]), 2)
        self.assertEqual(output.read_bytes(), before)


class PandoServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pando http ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "board"
        self.server = make_server(self.home, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        status, raw, _ = self.call("GET", "/")
        self.assertEqual(status, 200)
        match = re.search(rb'<meta name="pando-token" content="([a-f0-9]{64})">', raw)
        self.assertIsNotNone(match, "UI must expose its local write token to its own client")
        self.token = match.group(1).decode()

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def call(self, method, path, value=None, headers=None):
        body = None if value is None else json.dumps(value).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_state_and_sources_are_available_locally_without_starting_sessions(self):
        board = Board(self.home)
        identity = board.ingest(b"text", title="HTTP fixture", uri="fixture://http", author="host")
        status, raw, headers = self.call("GET", "/api/state")
        state = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(state["name"], "Lodestone Pando")
        self.assertEqual({p["id"] for p in state["participants"]},
                         {"host", "operator", "reviewer"})
        self.assertEqual(state["stats"]["sources"], 1)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.call("GET", "/api/sources/" + identity)[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers={"Host": "foreign.example"})[0], 403)

    def test_cross_origin_and_wrong_tokens_refuse_writes_then_valid_post_succeeds(self):
        value = {"author": "host", "shelf": "commons", "title": "Hello", "body": "Recorded locally."}
        for headers in ({}, {"X-Pando-Token": "bad"}, {"X-Pando-Token": "caf\u00e9"},
                        {"X-Pando-Token": self.token, "Origin": "https://foreign.example"},
                        {"X-Pando-Token": self.token, "Origin": "null"},
                        {"X-Pando-Token": self.token, "Host": "foreign.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("POST", "/api/posts", value, headers)[0], 403)
        self.assertEqual(Board(self.home).state()["stats"]["posts"], 0)
        headers = {"Origin": self.origin, "X-Pando-Token": self.token, "Content-Type": "application/json"}
        status, raw, _ = self.call("POST", "/api/posts", value, headers)
        self.assertEqual(status, 201, raw)
        parent = json.loads(raw)["id"]
        status, raw, _ = self.call("POST", "/api/posts", {**value, "title": "Re: Hello", "parent_id": parent}, headers)
        self.assertEqual(status, 201, raw)
        self.assertEqual(Board(self.home).state()["stats"]["posts"], 2)

    def test_malformed_json_object_and_unknown_source_do_not_publish(self):
        headers = {"X-Pando-Token": self.token, "Origin": self.origin}
        self.assertEqual(self.call("POST", "/api/posts", ["not-an-object"], headers)[0], 400)
        value = {"author": "host", "shelf": "commons", "title": "Bad", "body": "Unbound", "source_ids": ["missing"]}
        self.assertEqual(self.call("POST", "/api/posts", value, headers)[0], 400)
        self.assertEqual(Board(self.home).state()["stats"]["posts"], 0)

    def test_malformed_reply_parent_and_database_failure_return_json_without_writing(self):
        from unittest.mock import patch
        headers = {"X-Pando-Token": self.token, "Origin": self.origin}
        value = {"author": "host", "shelf": "commons", "title": "Invalid reply", "body": "Do not write"}
        for parent in ({}, ["x"], 5):
            with self.subTest(parent=parent):
                status, raw, _ = self.call("POST", "/api/posts", {**value, "parent_id": parent}, headers)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(raw))
        with patch.object(Board, "post", side_effect=sqlite3.OperationalError("fixture unavailable")):
            status, raw, _ = self.call("POST", "/api/posts", value, headers)
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(raw))
        with patch.object(Board, "state", side_effect=sqlite3.DatabaseError("fixture unavailable")):
            status, raw, _ = self.call("GET", "/api/state")
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(raw))
        self.assertEqual(Board(self.home).state()["stats"]["posts"], 0)


if __name__ == "__main__":
    unittest.main()
