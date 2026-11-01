"""Synthetic engineering checks for Nurarihyon profile plans.

All inputs are disposable literals. These tests open no files except this
module's own source for the import check, start no service or provider, and
read no research data. sqlite3 runs only in memory to compare queue ordering.
"""
import ast
import copy
import hashlib
import inspect
import json
import sqlite3
import unittest

from lodestone_nurarihyon import plans
from lodestone_nurarihyon.plans import (PlanError, lesson_for, make_profile_plan, parse_profile_source,
                                        priority_key, validate_profile_plan)
from lodestone_machinery.organs._kernel import digest, text as kernel_text, typed_equal
from lodestone_pando.store import PARTICIPANTS, SHELVES

VIEW = {"name": "Lodestone Pando", "shelves": list(SHELVES), "artifact_id": None, "author": None}
ARTIFACT = "a" * 64


def source_value(**changes):
    value = {"schema": "nurarihyon-profile-source/v1",
             "baseline": {"board_name": "Lodestone Pando", "enabled_shelves": list(SHELVES)},
             "candidate": {"board_name": "Lodestone Pando", "enabled_shelves": ["commons", "memory"]},
             "rationale": "Fixture: show two shelves on a disposable board."}
    value.update(changes)
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def with_token(token, **changes):
    """Replace the rationale value with a literal JSON token."""
    return encode(source_value(rationale="@@", **changes)).replace(b'"@@"', token)


class Checks(unittest.TestCase):
    def assertCode(self, code, function, *args):
        with self.assertRaises(PlanError) as context:
            function(*args)
        self.assertIsInstance(context.exception, ValueError)
        self.assertEqual(context.exception.code, code)
        return context.exception


class SourceBindingTests(Checks):
    def test_mirrors_match_existing_pando_source(self):
        self.assertEqual(plans.SHELVES, SHELVES)
        self.assertEqual(plans.PARTICIPANT_IDS, tuple(p["id"] for p in PARTICIPANTS))

    def test_source_bound_fits_existing_kernel_quote_bound(self):
        kernel_text("x" * plans.MAX_SOURCE_BYTES, "quote")
        with self.assertRaises(ValueError):
            kernel_text("x" * (plans.MAX_SOURCE_BYTES + 1), "quote")

    def test_typed_equality_matches_existing_kernel(self):
        pairs = [(1, True), ([1], [True]), ({"a": 1}, {"a": 1.0}), ("a", "a"), (["x"], ("x",)),
                 (None, None), ({"a": ["x"]}, {"a": ["x"]}), ({"a": 1}, {"b": 1})]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertEqual(plans._typed_equal(a, b), typed_equal(a, b))

    def test_plan_identity_is_existing_canonical_digest(self):
        plan = make_profile_plan(encode(source_value()), VIEW)
        raw = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest(plan))

    def test_dispatch_has_only_declared_adapters_without_io_or_dynamic_execution(self):
        tree = ast.parse(inspect.getsource(plans))
        imported, adapters = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.assertEqual(node.level, 1)
                    adapters.add(node.module)
                else:
                    imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"hashlib", "json", "math", "re"})
        self.assertEqual(adapters, {"documents", "reconciliation", "complete_capture", "historical_observation"})
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertFalse(names & {"open", "print", "input", "eval", "exec", "compile", "__import__", "globals"})


class ParseTests(Checks):
    def test_valid_source_returns_fresh_exact_fields(self):
        result = parse_profile_source(encode(source_value()))
        self.assertEqual(result, source_value())
        result["baseline"]["enabled_shelves"].append("extra")
        self.assertEqual(parse_profile_source(encode(source_value())), source_value())

    def test_key_order_and_whitespace_do_not_change_parsed_fields(self):
        value = source_value()
        compact = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        reordered = json.dumps(dict(reversed(list(value.items()))), indent=3).encode()
        self.assertEqual(parse_profile_source(compact), parse_profile_source(reordered))

    def test_byte_bound_is_exact(self):
        raw = encode(source_value())
        parse_profile_source(raw + b" " * (plans.MAX_SOURCE_BYTES - len(raw)))
        self.assertCode("REJECTED_VALIDATION", parse_profile_source,
                        raw + b" " * (plans.MAX_SOURCE_BYTES + 1 - len(raw)))

    def test_rejects_non_bytes_inputs(self):
        raw = encode(source_value())
        for value in (raw.decode(), bytearray(raw), memoryview(raw), None, source_value()):
            with self.subTest(kind=type(value).__name__):
                self.assertCode("REJECTED_VALIDATION", parse_profile_source, value)

    def test_rejects_invalid_encoding_syntax_and_nesting(self):
        raw = encode(source_value())
        for bad in (b"\xff", b"\xef\xbb\xbf" + raw, raw[:-1], b"", b"[]", b'"text"',
                    b"[" * 2000 + b"]" * 2000, b'{"a":' * 600 + b"1" + b"}" * 600):
            with self.subTest(bad=bad[:20]):
                self.assertCode("REJECTED_VALIDATION", parse_profile_source, bad)

    def test_rejects_duplicate_keys_at_any_depth(self):
        raw = encode(source_value())
        top = raw[:-1] + b', "rationale": "again"}'
        nested = raw.replace(b'"board_name": "Lodestone Pando"',
                             b'"board_name": "Other", "board_name": "Lodestone Pando"', 1)
        for bad in (top, nested):
            with self.subTest(bad=bad):
                error = self.assertCode("REJECTED_VALIDATION", parse_profile_source, bad)
                self.assertIn("duplicate", str(error))

    def test_rejects_nonfinite_numbers(self):
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e400", b"-1e400"):
            with self.subTest(token=token):
                error = self.assertCode("REJECTED_VALIDATION", parse_profile_source, with_token(token))
                self.assertIn("nonfinite", str(error))

    def test_rejects_unknown_missing_and_instruction_fields(self):
        base = source_value()
        cases = [{**base, "permissions": ["write"]}, {**base, "instructions": ["run"]},
                 {k: v for k, v in base.items() if k != "rationale"},
                 {**base, "baseline": {**base["baseline"], "execute": "shell"}},
                 {**base, "candidate": {"board_name": "Only a name"}}]
        for value in cases:
            with self.subTest(value=value):
                self.assertCode("REJECTED_VALIDATION", parse_profile_source, encode(value))

    def test_rejects_wrong_schema_and_exact_type_confusion(self):
        base = source_value()
        candidate = base["candidate"]
        cases = [{**base, "schema": "nurarihyon-profile-source/v2"}, {**base, "schema": ["nurarihyon-profile-source/v1"]}]
        for name in (4, True, None, "", "   ", ["Pando"]):
            cases.append({**base, "candidate": {**candidate, "board_name": name}})
        for shelves in ("commons", [], ["unknown"], ["commons", "commons"], [1], [True], ["Commons"],
                        [["commons"]], list(SHELVES) + ["commons"]):
            cases.append({**base, "candidate": {**candidate, "enabled_shelves": shelves}})
        for rationale in (1, 1.5, True, None, "", " \n", ["text"]):
            cases.append({**base, "rationale": rationale})
        for value in cases:
            with self.subTest(value=value):
                self.assertCode("REJECTED_VALIDATION", parse_profile_source, encode(value))

    def test_utf8_byte_bounds_and_unencodable_text(self):
        base = source_value()
        renamed = {"enabled_shelves": list(SHELVES)}  # Only board_name changes.
        parse_profile_source(encode({**base, "rationale": "é" * 512}))
        parse_profile_source(encode({**base, "candidate": {**renamed, "board_name": "é" * 60}}))
        for value in ({**base, "rationale": "é" * 512 + "x"},
                      {**base, "candidate": {**renamed, "board_name": "é" * 60 + "x"}}):
            with self.subTest(value=value):
                self.assertCode("REJECTED_VALIDATION", parse_profile_source, encode(value))
        surrogate = encode(source_value(rationale="@@")).replace(b'"@@"', b'"\\ud800"')
        self.assertCode("REJECTED_VALIDATION", parse_profile_source, surrogate)

    def test_exactly_one_operator_must_change(self):
        base = source_value()
        same = {**base, "candidate": copy.deepcopy(base["baseline"])}
        both = {**base, "candidate": {"board_name": "Renamed", "enabled_shelves": ["commons"]}}
        for value in (same, both):
            with self.subTest(value=value):
                self.assertCode("REJECTED_VALIDATION", parse_profile_source, encode(value))
        reordered = {**base, "candidate": {"board_name": "Lodestone Pando",
                                           "enabled_shelves": list(reversed(SHELVES))}}
        self.assertEqual(make_profile_plan(encode(reordered), VIEW)["operator"], "enabled_shelves")

    def test_errors_do_not_echo_source_content(self):
        error = self.assertCode("REJECTED_VALIDATION", parse_profile_source,
                                encode(source_value(rationale="SECRET-MARKER " * 100)))
        self.assertNotIn("SECRET-MARKER", str(error))


class PlanTests(Checks):
    def test_plan_has_exact_fields_and_inert_authority(self):
        raw = encode(source_value())
        self.assertEqual(make_profile_plan(raw, VIEW), {
            "schema": "nurarihyon-plan/v1", "toolkit": "pando.profile/v1",
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "baseline": {"board_name": "Lodestone Pando", "enabled_shelves": list(SHELVES)},
            "candidate": {"board_name": "Lodestone Pando", "enabled_shelves": ["commons", "memory"]},
            "operator": "enabled_shelves",
            "rationale": "Fixture: show two shelves on a disposable board.",
            "authority": "PROPOSAL_ONLY"})

    def test_name_change_selects_board_name_operator(self):
        value = source_value(candidate={"board_name": "Renamed Pando", "enabled_shelves": list(SHELVES)})
        plan = make_profile_plan(encode(value), VIEW)
        self.assertEqual((plan["operator"], plan["candidate"]["board_name"]), ("board_name", "Renamed Pando"))

    def test_plan_is_deterministic_and_never_modifies_or_aliases_inputs(self):
        raw, view = encode(source_value()), copy.deepcopy(VIEW)
        first = make_profile_plan(raw, view)
        first["baseline"]["enabled_shelves"].clear()
        second = make_profile_plan(raw, view)
        self.assertEqual(view, VIEW)
        self.assertEqual(digest(second), digest(make_profile_plan(raw, view)))
        self.assertEqual(second["baseline"]["enabled_shelves"], list(SHELVES))

    def test_stale_baseline_preserves_order_and_name(self):
        raw = encode(source_value())
        for view in ({**VIEW, "name": "Other"}, {**VIEW, "shelves": list(reversed(SHELVES))},
                     {**VIEW, "shelves": ["commons"]}):
            with self.subTest(view=view):
                self.assertCode("STALE_BASELINE", make_profile_plan, raw, view)

    def test_current_view_metadata_rules(self):
        raw = encode(source_value())
        accepted = ({"name": VIEW["name"], "shelves": VIEW["shelves"]},
                    {**VIEW, "artifact_id": ARTIFACT, "author": "host"},
                    {**VIEW, "artifact_id": ARTIFACT, "author": "reviewer"})
        for view in accepted:
            with self.subTest(view=view):
                make_profile_plan(raw, view)
        rejected = [{**VIEW, "extra": 1}, {"name": VIEW["name"], "artifact_id": None},
                    {**VIEW, "artifact_id": ARTIFACT}, {**VIEW, "author": "host"},
                    {**VIEW, "artifact_id": ARTIFACT, "author": "invalid attribution"},
                    {**VIEW, "artifact_id": "A" * 64, "author": "host"},
                    {**VIEW, "artifact_id": "eat:" + ARTIFACT, "author": "host"},
                    {**VIEW, "artifact_id": True, "author": "host"},
                    {**VIEW, "name": 4}, {**VIEW, "name": "x" * 121},
                    {**VIEW, "shelves": ["unknown"]}, {**VIEW, "shelves": tuple(SHELVES)},
                    [VIEW["name"], VIEW["shelves"]], None]
        for view in rejected:
            with self.subTest(view=view):
                self.assertCode("REJECTED_VALIDATION", make_profile_plan, raw, view)

    def test_validation_accepts_only_exact_recomputation(self):
        raw = encode(source_value())
        plan = make_profile_plan(raw, VIEW)
        before = copy.deepcopy(plan)
        self.assertIsNone(validate_profile_plan(plan, raw, VIEW))
        self.assertEqual(plan, before)
        tampered = []
        for key, value in (("authority", "AUTHORIZED"), ("operator", "board_name"),
                           ("rationale", "Changed after approval."), ("toolkit", "pando.profile/v2"),
                           ("candidate", {"board_name": "Lodestone Pando", "enabled_shelves": ["commons"]}),
                           ("baseline", {"board_name": "Lodestone Pando", "enabled_shelves": tuple(SHELVES)}),
                           ("source_sha256", "not-a-hash"), ("source_sha256", None)):
            tampered.append({**plan, key: value})
        tampered += [{**plan, "approved": True}, {k: v for k, v in plan.items() if k != "rationale"}]
        for bad in tampered:
            with self.subTest(bad=bad):
                self.assertCode("REJECTED_VALIDATION", validate_profile_plan, bad, raw, VIEW)
        for bad in ([plan], None, json.dumps(plan)):
            with self.subTest(bad=bad):
                self.assertCode("REJECTED_VALIDATION", validate_profile_plan, bad, raw, VIEW)
        self.assertCode("REJECTED_VALIDATION", validate_profile_plan, plan, raw.decode(), VIEW)

    def test_validation_enforces_source_bound_before_hashing(self):
        raw = encode(source_value())
        padded = raw + b" " * (plans.MAX_SOURCE_BYTES - len(raw))
        self.assertIsNone(validate_profile_plan(make_profile_plan(padded, VIEW), padded, VIEW))
        oversized = padded + b" "
        # Even a plan whose stored hash names the oversized bytes is rejected by size, not drift.
        plan = {**make_profile_plan(raw, VIEW), "source_sha256": hashlib.sha256(oversized).hexdigest()}
        error = self.assertCode("REJECTED_VALIDATION", validate_profile_plan, plan, oversized, VIEW)
        self.assertIn("exceeds", str(error))
        self.assertCode("REJECTED_VALIDATION", validate_profile_plan, plan, b" " * (1024 * 1024), VIEW)

    def test_validation_classifies_source_drift_and_stale_view(self):
        raw = encode(source_value())
        plan = make_profile_plan(raw, VIEW)
        drifted = encode(source_value(rationale="A different approved reason."))
        self.assertCode("SOURCE_DRIFT", validate_profile_plan, plan, drifted, VIEW)
        self.assertCode("SOURCE_DRIFT", validate_profile_plan, plan, raw + b" ", VIEW)
        self.assertCode("SOURCE_DRIFT", validate_profile_plan, {**plan, "source_sha256": "0" * 64}, raw, VIEW)
        self.assertCode("STALE_BASELINE", validate_profile_plan, plan, raw, {**VIEW, "name": "Changed"})


class PriorityTests(Checks):
    TASKS = [{"id": "b", "priority": 5, "sequence": 3}, {"id": "a", "priority": 5, "sequence": 3},
             {"id": "Z", "priority": 5, "sequence": 3}, {"id": "0", "priority": 5, "sequence": 3},
             {"id": "c", "priority": 9, "sequence": 7}, {"id": "d", "priority": 0, "sequence": 0},
             {"id": "e", "priority": 5, "sequence": 1}, {"id": "x.y:z-1_", "priority": 5, "sequence": 3},
             {"id": "A-", "priority": 5, "sequence": 3}, {"id": "f", "priority": 0, "sequence": 2 ** 63 - 1}]

    def test_order_is_priority_descending_then_sequence_then_id(self):
        order = [t["id"] for t in sorted(self.TASKS, key=priority_key)]
        self.assertEqual(order, ["c", "e", "0", "A-", "Z", "a", "b", "x.y:z-1_", "d", "f"])
        self.assertEqual(priority_key({"id": "q", "priority": 7, "sequence": 4, "state": "READY"}), (-7, 4, "q"))

    def test_order_matches_sqlite_order_by(self):
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, priority INTEGER NOT NULL, sequence INTEGER NOT NULL)")
            db.executemany("INSERT INTO tasks VALUES (?,?,?)",
                           [(t["id"], t["priority"], t["sequence"]) for t in reversed(self.TASKS)])
            sql = [row[0] for row in db.execute("SELECT id FROM tasks ORDER BY priority DESC, sequence ASC, id ASC")]
        finally:
            db.close()
        self.assertEqual(sql, [t["id"] for t in sorted(self.TASKS, key=priority_key)])

    def test_boundaries_and_invalid_values(self):
        task = {"id": "t", "priority": 0, "sequence": 0}
        for good in ({**task, "priority": 9}, {**task, "sequence": 2 ** 63 - 1}, {**task, "id": "a" * 128}):
            priority_key(good)
        bad = [None, [], {"id": "t", "priority": 1}]
        bad += [{**task, "priority": v} for v in (True, False, 10, -1, 1.0, "1", None)]
        bad += [{**task, "sequence": v} for v in (-1, True, 2 ** 63, 1.5, "0", None)]
        bad += [{**task, "id": v} for v in ("", " a", "a b", "é", "-a", "a" * 129, 5, None, "a\n")]
        for value in bad:
            with self.subTest(value=value):
                self.assertCode("INVALID_TASK", priority_key, value)
        before = copy.deepcopy(task)
        priority_key(task)
        self.assertEqual(task, before)


class LessonTests(Checks):
    SHA = hashlib.sha256(b"fixture").hexdigest()

    def test_supported_outcomes_are_inert_deterministic_records(self):
        for code in ("SOURCE_DRIFT", "STALE_BASELINE", "RESOURCE_LIMIT", "REJECTED_VALIDATION"):
            with self.subTest(code=code):
                lesson = lesson_for("pando.profile/v1", code, self.SHA)
                self.assertEqual(set(lesson), {"schema", "toolkit", "outcome", "source_sha256",
                                               "condition", "next_step", "retry", "authority"})
                self.assertEqual((lesson["schema"], lesson["toolkit"], lesson["outcome"], lesson["source_sha256"]),
                                 ("nurarihyon-lesson/v1", "pando.profile/v1", code, self.SHA))
                self.assertEqual((lesson["retry"], lesson["authority"]), ("NONE", "NONE"))
                self.assertTrue(all(len(lesson[k]) <= 300 for k in ("condition", "next_step")))
                self.assertEqual(digest(lesson), digest(lesson_for("pando.profile/v1", code, self.SHA)))

    def test_rejects_controller_outcomes_and_invalid_arguments(self):
        cases = [("pando.profile/v1", code, self.SHA) for code in
                 ("STOPPED", "INTERRUPTED", "source_drift", "VALIDATION_REJECTED", "REJECTED-VALIDATION", "", None)]
        cases += [(toolkit, "SOURCE_DRIFT", self.SHA) for toolkit in ("pando.profile/v2", "", None, ["pando.profile/v1"])]
        cases += [("pando.profile/v1", "SOURCE_DRIFT", sha) for sha in
                  (self.SHA.upper(), self.SHA[:63], self.SHA + "0", None, b"0" * 64)]
        for args in cases:
            with self.subTest(args=args):
                self.assertCode("INVALID_LESSON", lesson_for, *args)


if __name__ == "__main__":
    unittest.main()
