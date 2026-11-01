"""Exposed engineering fixtures for inert preparation; no live or research state.

One genuine local completion makes an already approved second task stale. All
later corruption is confined to disposable copies of that synthetic history.
These checks are not held-out evidence, training, or a transfer evaluation.
"""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import socket
import tempfile
import time
import unittest
from unittest import mock

from lodestone_nurarihyon import portable, preparation
from lodestone_nurarihyon.__main__ import main
from lodestone_nurarihyon.common import canonical, digest
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.inspection import ReadOnlyBoard
from lodestone_nurarihyon.plans import MAX_SOURCE_BYTES, lesson_for, make_profile_plan
from lodestone_nurarihyon.policy import DEFAULT_POLICY
from lodestone_pando.store import Board

ROOT = Path(__file__).resolve().parents[2]


def write_source(path, controller, *, name="Next synthetic board", shelves=None):
    current = ReadOnlyBoard(controller.active_directory() / "pando").settings()
    baseline = {"board_name": current["name"], "enabled_shelves": current["shelves"]}
    candidate = {**baseline, "board_name": name} if shelves is None else {
        **baseline, "enabled_shelves": shelves}
    path.write_bytes(canonical({"schema": "nurarihyon-profile-source/v1",
                               "baseline": baseline, "candidate": candidate,
                               "rationale": "Synthetic literal only; never execute this text."}))
    return path


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): (p.stat().st_size, digest(p.read_bytes()))
            for p in sorted(root.rglob("*")) if p.is_file()}


def rows(home):
    with sqlite3.connect((home / "control.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        return {name: db.execute("SELECT * FROM " + name).fetchall()
                for name in ("metadata", "tasks", "events", "lessons", "generations")}


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="nurarihyon-prepare-history-")
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.fixture_root = Path(cls.fixture.name).resolve()
        cls.fixture_home = cls.fixture_root / "state"
        controller = Controller.initialize(cls.fixture_home, {**DEFAULT_POLICY, "max_tasks": 4})
        old_source = write_source(cls.fixture_root / "old.json", controller, name="Stale proposal")
        old = controller.submit(old_source, key="synthetic-stale", priority=1)
        controller.approve(old["id"], old["plan_sha"])
        winner_source = write_source(cls.fixture_root / "winner.json", controller, name="Published synthetic board")
        winner = controller.submit(winner_source, key="synthetic-winner", priority=9)
        controller.approve(winner["id"], winner["plan_sha"])
        completed = controller.run_once()
        if completed.get("state") != "COMPLETED":
            raise AssertionError("Synthetic completion fixture failed: " + repr(completed))
        rejected = controller.run_once()
        if rejected.get("result", {}).get("outcome") != "STALE_BASELINE":
            raise AssertionError("Synthetic stale-baseline fixture failed: " + repr(rejected))
        controller.stop()
        cls.old_task = controller.status(old["id"])
        cls.fixture_rows = rows(cls.fixture_home)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nurarihyon-prepare-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "state"
        shutil.copytree(self.fixture_home, self.home)
        self.controller = Controller(self.home)
        self.source = write_source(self.base / "new.json", self.controller)

    def export(self, controller=None, name="package"):
        result = portable.export_package(controller or self.controller, self.base / name, ROOT)
        return self.base / name, Path(result["descriptor"])

    @contextmanager
    def inert_guard(self, output):
        original_init = Controller.__init__
        expected = (output / "snapshot/state").resolve()

        def only_snapshot(instance, home):
            self.assertEqual(Path(home).resolve(), expected, "Preparation opened non-snapshot controller state")
            original_init(instance, home)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(Controller, "__init__", only_snapshot))
            for name in ("submit", "approve", "run_once", "serve", "stop", "clear_stop", "retry", "recover"):
                stack.enter_context(mock.patch.object(Controller, name, side_effect=AssertionError("Forbidden action: " + name)))
            stack.enter_context(mock.patch.object(Board, "__init__", side_effect=AssertionError("Mutable Board constructor")))
            stack.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("Network socket")))
            stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=AssertionError("Network connection")))
            stack.enter_context(mock.patch("subprocess.Popen", side_effect=AssertionError("Process execution")))
            yield

    def prepare(self, package, pin, *, source=None, output="prepared", **options):
        destination = self.base / output
        with self.inert_guard(destination):
            return preparation.prepare(package, pin, source or self.source, destination, **options)

    def assert_failed_receipt(self, output="prepared"):
        destination = self.base / output
        receipt = json.loads((destination / "PREPARATION-FAILED.json").read_bytes())
        self.assertEqual(receipt["status"], "PREPARATION_FAILED")
        self.assertEqual(receipt["authority"], "NONE")
        self.assertFalse(receipt["task_created"])
        self.assertFalse(receipt["automatic_retry"])
        self.assertTrue((destination / "snapshot").is_dir())
        self.assertFalse((destination / "PREPARATION.json").exists())

    def copy_case(self, name):
        home = self.base / name
        shutil.copytree(self.fixture_home, home)
        return Controller(home)

    def select(self, plan, tasks, lessons, events, *, limit=4):
        # Selection alone does not assert that this historical plan is currently
        # admissible. Its source/plan evidence remains the real retained input.
        with self.inert_guard(self.base / "selection-only"):
            return preparation._select_lessons(
                self.controller, plan, tasks, lessons, events, limit,
                time.monotonic() + portable.MAX_SECONDS)

    def test_cold_empty_history_cli_prepares_exact_inert_plan(self):
        empty = Controller.initialize(self.base / "empty")
        source = write_source(self.base / "empty-source.json", empty)
        package, pin = self.export(empty)
        output = self.base / "prepared"
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.inert_guard(output), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["prepare", "--package", str(package), "--descriptor", str(pin),
                         "--source", str(source), "--output", str(output)])
        self.assertEqual(code, 0, stderr.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["lesson_context"], [])
        self.assertEqual(report["retrieval"]["lessons_checked"], 0)
        self.assertEqual(report["plan"]["authority"], "PROPOSAL_ONLY")
        self.assertEqual(report["authority"], "NONE")
        self.assertFalse(report["task_created"])
        self.assertFalse(report["execution_allowed"])
        self.assertIs(report["admission_checked"], False)
        self.assertEqual(report["costs"]["model_calls"], 0)
        self.assertEqual(report["costs"]["training_updates"], 0)
        self.assertEqual(report["plan_sha256"], digest(canonical(report["plan"])))

    def test_genuine_history_reused_for_new_source_without_mutation(self):
        package, pin = self.export()
        original = (tree_bytes(self.home), rows(self.home), tree_bytes(package), pin.read_bytes(), self.source.read_bytes())
        report = self.prepare(package, pin)
        group, = report["lesson_context"]
        self.assertEqual(group["match"], "same_toolkit_and_operator")
        self.assertNotEqual(group["lesson"]["source_sha256"], report["source_sha256"])
        self.assertEqual(group["lesson"]["outcome"], "STALE_BASELINE")
        self.assertEqual(group["occurrence_count"], 1)
        self.assertEqual(group["provenance"][0]["task_id"], self.old_task["id"])
        self.assertTrue(group["provenance"][0]["supporting_events"])
        self.assertIs(group["provenance"][0]["old_baseline_equals_new_baseline"], False)
        self.assertIs(group["provenance"][0]["old_candidate_equals_new_candidate"], False)
        self.assertEqual(group["applicability"], "NOT_ESTABLISHED")
        self.assertIs(report["admission_checked"], False)
        self.assertTrue(report["snapshot_stopped"])
        self.assertEqual(original, (tree_bytes(self.home), rows(self.home), tree_bytes(package), pin.read_bytes(), self.source.read_bytes()))
        snapshot = self.base / "prepared/snapshot"
        manifest = json.loads((snapshot / "metadata/package.json").read_bytes())
        for name, expected in manifest["files"].items():
            raw = (snapshot / name).read_bytes()
            self.assertEqual({"bytes": len(raw), "sha256": digest(raw)}, expected)
        self.assertEqual((snapshot / "state/STOP").read_bytes(), (self.home / "STOP").read_bytes())
        self.assertEqual(rows(snapshot / "state"), rows(self.home))
        view = ReadOnlyBoard(self.controller.active_directory() / "pando").settings()
        self.assertEqual(report["plan"], make_profile_plan(self.source.read_bytes(), view))
        submitted = self.controller.submit(self.source, key="separate-post-check-submission")
        self.assertEqual(submitted["plan_sha"], report["plan_sha256"])
        self.assertEqual(submitted["state"], "PLANNED")

    def test_other_operator_is_checked_but_not_retrieved(self):
        write_source(self.source, self.controller, shelves=["commons", "memory"])
        package, pin = self.export()
        report = self.prepare(package, pin)
        self.assertEqual(report["plan"]["operator"], "enabled_shelves")
        self.assertEqual(report["lesson_context"], [])
        self.assertEqual(report["retrieval"]["lessons_checked"], 1)
        self.assertEqual(report["retrieval"]["matching_groups"], 0)

    def test_duplicate_lessons_collapse_without_erasing_occurrences(self):
        with self.controller.connection() as db:
            db.execute("INSERT INTO lessons(task_id,body) SELECT task_id,body FROM lessons")
        package, pin = self.export()
        report = self.prepare(package, pin)
        group, = report["lesson_context"]
        self.assertEqual(group["occurrence_count"], 2)
        self.assertEqual(len({p["lesson_id"] for p in group["provenance"]}), 2)
        self.assertEqual({p["task_id"] for p in group["provenance"]}, {self.old_task["id"]})
        self.assertIn("not a prediction or an independently replicated result", group["interpretation"])
        self.assertTrue(all(p["supporting_events"] for p in group["provenance"]))

    def test_same_source_selection_reports_factual_equality_only(self):
        tasks, lessons, events, _, _ = preparation._bounded_evidence(self.controller)
        old_plan = json.loads(tasks[self.old_task["id"]]["plan"])
        groups, counts = self.select(old_plan, tasks, lessons, events)
        group, = groups
        occurrence, = group["provenance"]
        self.assertEqual(group["match"], "same_source")
        self.assertEqual(counts["selected_groups"], 1)
        self.assertIs(occurrence["old_baseline_equals_new_baseline"], True)
        self.assertIs(occurrence["old_candidate_equals_new_candidate"], True)
        self.assertEqual(group["applicability"], "NOT_ESTABLISHED")

        # The same candidate can also come from a different current baseline.
        write_source(self.source, self.controller, name=old_plan["candidate"]["board_name"])
        current = ReadOnlyBoard(self.controller.active_directory() / "pando").settings()
        plan = make_profile_plan(self.source.read_bytes(), current)
        groups, _ = self.select(plan, tasks, lessons, events)
        occurrence, = groups[0]["provenance"]
        self.assertEqual(groups[0]["match"], "same_toolkit_and_operator")
        self.assertIs(occurrence["old_baseline_equals_new_baseline"], False)
        self.assertIs(occurrence["old_candidate_equals_new_candidate"], True)

    def test_multiple_groups_rank_exact_source_then_recency_and_truncate(self):
        tasks, lessons, events, _, _ = preparation._bounded_evidence(self.controller)
        old_task = tasks[self.old_task["id"]]
        old_plan = json.loads(old_task["plan"])
        old_raw = json.loads((self.home / "inputs" / (old_task["source_sha"] + ".json")).read_bytes())
        source_hashes = [old_task["source_sha"]]
        # Two explicitly synthetic record variations exercise the selector;
        # they are not additional observed failures or independent evidence.
        for number in (1, 2):
            raw = canonical({**old_raw, "candidate": {
                **old_raw["candidate"], "board_name": "Synthetic alternate " + str(number)}})
            source_sha = digest(raw)
            (self.home / "inputs" / (source_sha + ".json")).write_bytes(raw)
            plan = make_profile_plan(raw, {"name": old_plan["baseline"]["board_name"],
                                          "shelves": old_plan["baseline"]["enabled_shelves"]})
            identity = str(number) * 32
            tasks[identity] = {**old_task, "id": identity, "source_sha": source_sha,
                               "plan": canonical(plan).decode(), "plan_sha": digest(canonical(plan))}
            lessons.append({"id": lessons[0]["id"] + number, "task_id": identity,
                            "body": canonical(lesson_for(plan["toolkit"], "STALE_BASELINE", source_sha)).decode()})
            events.append({**events[0], "sequence": events[0]["sequence"] + number, "task_id": identity})
            source_hashes.append(source_sha)
        current = ReadOnlyBoard(self.controller.active_directory() / "pando").settings()
        new_plan = make_profile_plan(self.source.read_bytes(), current)
        groups, counts = self.select(new_plan, tasks, lessons, events, limit=2)
        self.assertEqual([g["lesson"]["source_sha256"] for g in groups], list(reversed(source_hashes[1:])))
        self.assertEqual(counts, {"lessons_checked": 3, "matching_groups": 3,
                                  "selected_groups": 2, "unselected_groups": 1})
        exact_groups, exact_counts = self.select(old_plan, tasks, lessons, events, limit=2)
        self.assertEqual([g["lesson"]["source_sha256"] for g in exact_groups], [source_hashes[0], source_hashes[2]])
        self.assertEqual(exact_groups[0]["match"], "same_source")
        self.assertEqual(exact_counts, counts)

    def test_provenance_and_event_caps_retain_latest_refs_and_full_digests(self):
        tasks, retained, observed, _, _ = preparation._bounded_evidence(self.controller)
        task = tasks[self.old_task["id"]]
        # Bounded synthetic duplicates retain their shared task attribution;
        # no attempt-to-event pairing or replicated success is inferred.
        lessons = [{**retained[0], "id": retained[0]["id"] + n} for n in range(6)]
        events = [{**observed[0], "sequence": observed[0]["sequence"] + n,
                   "payload": canonical({"outcome": "STALE_BASELINE", "fixture_ordinal": n}).decode()}
                  for n in range(7)]
        current = ReadOnlyBoard(self.controller.active_directory() / "pando").settings()
        plan = make_profile_plan(self.source.read_bytes(), current)
        groups, counts = self.select(plan, tasks, lessons, events)
        group, = groups
        self.assertEqual(preparation.MAX_PROVENANCE_PER_GROUP, 4)
        self.assertEqual(preparation.MAX_EVENT_REFS, 4)
        self.assertEqual(group["occurrence_count"], 6)
        self.assertEqual(group["provenance_omitted"], 2)
        self.assertEqual([p["lesson_id"] for p in group["provenance"]],
                         [p["id"] for p in reversed(lessons[-4:])])
        full_events = [{"sequence": e["sequence"], "kind": e["kind"],
                        "payload_sha256": digest(e["payload"].encode())} for e in events]
        full_occurrences = [{"lesson_id": p["id"], "task_id": task["id"],
                             "source_sha256": task["source_sha"], "plan_sha256": task["plan_sha"],
                             "code_sha256": task["code_sha"], "base_generation": task["base_generation"],
                             "old_baseline_equals_new_baseline": False,
                             "old_candidate_equals_new_candidate": False,
                             "supporting_events": full_events,
                             "event_attribution": "Supporting task/outcome events; no unique attempt pairing inferred"}
                            for p in lessons]
        self.assertEqual(group["provenance_sha256"], digest(canonical(full_occurrences)))
        for occurrence in group["provenance"]:
            self.assertEqual(occurrence["supporting_events"], list(reversed(full_events[-4:])))
            self.assertEqual(occurrence["supporting_events_count"], 7)
            self.assertEqual(occurrence["supporting_events_omitted"], 3)
            self.assertEqual(occurrence["supporting_events_sha256"], digest(canonical(full_events)))
        self.assertEqual(self.select(plan, tasks, lessons, events), (groups, counts))
        # Alter an omitted witness: the visible event refs stay identical,
        # while both digests must still commit to the omitted evidence.
        changed_events = [dict(e) for e in events]
        changed_events[0]["payload"] = canonical({"outcome": "STALE_BASELINE", "fixture_ordinal": "changed"}).decode()
        changed, _ = self.select(plan, tasks, lessons, changed_events)
        self.assertNotEqual(changed[0]["provenance_sha256"], group["provenance_sha256"])
        self.assertNotEqual(changed[0]["provenance"][0]["supporting_events_sha256"],
                            group["provenance"][0]["supporting_events_sha256"])
        self.assertEqual(changed[0]["provenance"][0]["supporting_events"],
                         group["provenance"][0]["supporting_events"])

    def test_final_snapshot_census_rejects_added_and_excluded_files(self):
        package, pin = self.export()
        original = (tree_bytes(self.home), tree_bytes(package), pin.read_bytes(), self.source.read_bytes())
        original_select = preparation._select_lessons
        for number, name in enumerate(("state/unexpected.txt", "runtime/.env")):
            with self.subTest(path=name):
                output = "census-" + str(number)
                added = self.base / output / "snapshot" / name

                def add_after_selection(*args, **kwargs):
                    result = original_select(*args, **kwargs)
                    added.write_text("Synthetic census test; contains no credential.\n")
                    return result

                with mock.patch.object(preparation, "_select_lessons", side_effect=add_after_selection):
                    with self.assertRaisesRegex(ValueError, "snapshot changed|snapshot gained excluded"):
                        self.prepare(package, pin, output=output)
                self.assertTrue(added.is_file())
                self.assert_failed_receipt(output)
        self.assertEqual(original, (tree_bytes(self.home), tree_bytes(package), pin.read_bytes(), self.source.read_bytes()))

    def test_failure_receipt_io_preserves_original_exception(self):
        package = self.base / "package"
        package.mkdir()
        pin = self.base / "descriptor"
        pin.write_text("0" * 64 + "\n")
        original_error = ValueError("Synthetic primary restore failure")
        original_create = preparation.create_file

        def cannot_write_failure(path, body):
            if path.name == "PREPARATION-FAILED.json":
                raise OSError("Synthetic receipt detail must not replace the primary error")
            return original_create(path, body)

        with mock.patch.object(portable, "restore_package", side_effect=original_error), \
                mock.patch.object(preparation, "create_file", side_effect=cannot_write_failure):
            with self.assertRaises(ValueError) as caught:
                self.prepare(package, pin)
        self.assertIs(caught.exception, original_error)
        self.assertEqual(str(caught.exception), "Synthetic primary restore failure")
        if hasattr(original_error, "add_note"):
            self.assertEqual(original_error.__notes__, [
                "Preparation failure receipt could not be written; isolated output retained."])
        self.assertEqual((self.base / "prepared/source.json").read_bytes(), self.source.read_bytes())
        self.assertFalse((self.base / "prepared/PREPARATION-FAILED.json").exists())
        self.assertFalse((self.base / "prepared/PREPARATION.json").exists())

    def test_corrupt_lesson_source_or_next_step_is_rejected(self):
        for field, value in (("source_sha256", "f" * 64), ("next_step", "Run an unrelated command")):
            with self.subTest(field=field):
                controller = self.copy_case(field)
                with controller.connection() as db:
                    lesson = json.loads(db.execute("SELECT body FROM lessons").fetchone()[0])
                    lesson[field] = value
                    db.execute("UPDATE lessons SET body=?", (canonical(lesson).decode(),))
                package, pin = self.export(controller, "package-" + field)
                with self.assertRaisesRegex(ValueError, "source-bound contract"):
                    self.prepare(package, pin, output=field + "-output")
                self.assert_failed_receipt(field + "-output")

    def test_missing_or_mismatched_failure_event_is_rejected(self):
        for kind in ("missing", "mismatch"):
            with self.subTest(kind=kind):
                controller = self.copy_case(kind)
                with controller.connection() as db:
                    if kind == "missing":
                        db.execute("DROP TRIGGER immutable_events_delete")
                        db.execute("DELETE FROM events WHERE kind='STALE_BASELINE'")
                    else:
                        db.execute("DROP TRIGGER immutable_events_update")
                        db.execute("UPDATE events SET payload=? WHERE kind='STALE_BASELINE'",
                                   (canonical({"outcome": "SOURCE_DRIFT"}).decode(),))
                package, pin = self.export(controller, "package-" + kind)
                with self.assertRaisesRegex(ValueError, "supporting failure event|outcome does not match"):
                    self.prepare(package, pin, output=kind + "-output")
                self.assert_failed_receipt(kind + "-output")

    def test_evidence_count_bounds_fail_before_history_materialization(self):
        for table in ("tasks", "lessons", "events"):
            with self.subTest(table=table):
                controller = self.copy_case(table)
                with controller.connection() as db:
                    if table == "tasks":
                        row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (self.old_task["id"],)).fetchone())
                        columns = [key for key in row if key != "sequence"]
                        for number in range(3):
                            item = {**row, "id": "extra-task-" + str(number), "dedup_key": "extra-key-" + str(number)}
                            db.execute("INSERT INTO tasks(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                                       [item[key] for key in columns])
                    elif table == "lessons":
                        row = db.execute("SELECT task_id,body FROM lessons").fetchone()
                        db.executemany("INSERT INTO lessons(task_id,body) VALUES(?,?)", [tuple(row)] * 12)
                    else:
                        db.executemany("INSERT INTO events(task_id,time,kind,payload) VALUES(NULL,'fixture','UNRELATED','{}')",
                                       [()] * (preparation.MAX_EVENTS + 1))
                package, pin = self.export(controller, "package-" + table)
                with mock.patch.object(preparation._SnapshotController, "verify", side_effect=AssertionError("Count gate must run first")):
                    with self.assertRaisesRegex(ValueError, "count exceeds bound"):
                        self.prepare(package, pin, output=table + "-output")
                self.assert_failed_receipt(table + "-output")

    def test_oversized_lesson_and_event_payload_are_rejected(self):
        for table, column, bound in (("lessons", "body", preparation.MAX_LESSON_BYTES),
                                     ("events", "payload", preparation.MAX_CONTEXT_BYTES)):
            with self.subTest(table=table):
                controller = self.copy_case(table)
                with controller.connection() as db:
                    if table == "events":
                        db.execute("DROP TRIGGER immutable_events_update")
                    db.execute("UPDATE " + table + " SET " + column + "=?", ("x" * (bound + 1),))
                package, pin = self.export(controller, "package-" + table)
                with self.assertRaisesRegex(ValueError, "text exceeds bound"):
                    self.prepare(package, pin, output=table + "-output")
                self.assert_failed_receipt(table + "-output")

        package, pin = self.export(name="context-package")
        with mock.patch.object(preparation, "MAX_CONTEXT_BYTES", 2048):
            with self.assertRaisesRegex(ValueError, "context exceeds byte bound"):
                self.prepare(package, pin)
        self.assert_failed_receipt()

    def test_nonlesson_task_evidence_refuses_before_general_verifier(self):
        package, pin = self.export()
        for kind, message in (("source", "Invalid retained task pin"),
                              ("result", "text exceeds bound"),
                              ("column", "schema differs")):
            with self.subTest(kind=kind):
                restored = self.base / (kind + "-restored")
                portable.restore_package(package, pin, restored)
                database = restored / "state/control.sqlite3"
                with sqlite3.connect(database) as db:
                    task = db.execute("SELECT id FROM tasks WHERE state='COMPLETED'").fetchone()[0]
                    self.assertEqual(db.execute("SELECT count(*) FROM lessons WHERE task_id=?", (task,)).fetchone()[0], 0)
                    if kind == "source":
                        db.execute("DROP TRIGGER immutable_intent")
                        db.execute("UPDATE tasks SET source_sha='../outside-snapshot' WHERE id=?", (task,))
                    elif kind == "result":
                        db.execute("UPDATE tasks SET result=? WHERE id=?", ("x" * (preparation.MAX_CONTEXT_BYTES + 1), task))
                    else:
                        db.execute("ALTER TABLE tasks ADD COLUMN undeclared TEXT")
                manifest_path = restored / "metadata/package.json"
                manifest = json.loads(manifest_path.read_bytes())
                raw = database.read_bytes()
                manifest["files"]["state/control.sqlite3"] = {"bytes": len(raw), "sha256": digest(raw)}
                manifest_path.write_bytes(canonical(manifest))
                bad_package = self.base / (kind + "-package")
                packed = portable.archive_pack.pack(
                    {name: restored / name for name in ("runtime", "state", "metadata")},
                    bad_package, epoch="synthetic", authority_ref="Synthetic evidence-corruption fixture")
                bad_pin = self.base / (kind + "-pin")
                bad_pin.write_text(packed["descriptor_sha256"] + "\n")
                with mock.patch.object(preparation._SnapshotController, "verify", side_effect=AssertionError("Evidence gate must run first")):
                    with self.assertRaisesRegex(ValueError, message):
                        self.prepare(bad_package, bad_pin, output=kind + "-output")
                self.assert_failed_receipt(kind + "-output")

    def test_strict_source_stale_source_and_invalid_limits_refuse(self):
        package, pin = self.export()
        valid = self.source.read_bytes()
        stale = (self.home / "inputs" / (self.old_task["source_sha"] + ".json")).read_bytes()
        cases = (valid[:-1] + b',"rationale":"duplicate"}', stale, valid + b" " * MAX_SOURCE_BYTES)
        for number, raw in enumerate(cases):
            with self.subTest(source=number):
                self.source.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.prepare(package, pin, output="invalid-source-" + str(number))
        self.source.write_bytes(valid)
        for limit in (0, 9, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.prepare(package, pin, output="invalid-limit-" + str(limit), limit=limit)
        with self.assertRaisesRegex(ValueError, "Destination must be new"):
            self.prepare(package, pin, output="state")
        wrong = self.base / "wrong-pin"
        wrong.write_text("0" * 64 + "\n")
        with self.assertRaises(ValueError):
            self.prepare(package, wrong, output="bad-pin")
        self.assertFalse((self.base / "bad-pin/PREPARATION.json").exists())

    def test_restart_and_explicit_recovery_preserve_plan_and_context(self):
        package, pin = self.export()
        first = self.prepare(package, pin, output="first")
        restarted = Controller(self.home)
        before = rows(self.home)
        recovery = restarted.recover()
        self.assertEqual(recovery["reconciled_tasks"], [])
        self.assertFalse(recovery["work_restarted"])
        self.assertEqual(rows(self.home), before)
        second_package, second_pin = self.export(restarted, "second-package")
        second = self.prepare(second_package, second_pin, output="second")
        for key in ("plan", "plan_sha256", "source_sha256", "lesson_context", "retrieval", "snapshot_active_generation", "snapshot_stopped"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(json.loads((self.base / "first/PREPARATION.json").read_bytes()), first)

    def test_cli_home_is_rejected_before_opening_any_controller(self):
        stderr = io.StringIO()
        with mock.patch.object(Controller, "__init__", side_effect=AssertionError("Live home opened")), \
                mock.patch.object(preparation, "prepare", side_effect=AssertionError("Preparation entered")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("Network socket")), redirect_stderr(stderr):
            result = main(["--home", str(self.home), "prepare", "--package", "not-opened",
                           "--descriptor", "not-opened", "--source", "not-opened", "--output", "not-opened"])
        self.assertEqual(result, 2)
        self.assertIn("not --home", json.loads(stderr.getvalue())["message"])


if __name__ == "__main__":
    unittest.main()
