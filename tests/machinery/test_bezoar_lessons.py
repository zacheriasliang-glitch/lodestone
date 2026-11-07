"""Scripted review fixtures; no model judgment or useful-learning claim."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from lodestone_machinery.bezoar import Plumber
from lodestone_machinery.bezoar_cli import main
from lodestone_machinery.bezoar_lessons import REVIEW_SCHEMA
from lodestone_machinery.origami import _snapshot, _passage


class BezoarLessonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bezoar source lessons ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "capture"
        self.root.mkdir()
        self.file = self.root / "source.txt"
        self.original = b"The gate closes at six.\n"
        self.file.write_bytes(self.original)
        self.plumber = Plumber(self.base / "workspace")
        self.store = self.plumber.records
        self.binding = self.bind()
        self.tick = 0

    def bind(self, name="source.txt", run="scripted-source"):
        sources, manifest, _ = _snapshot(self.root, [name])
        return self.store.publish("origami", "source_bindings", {
            "run_id": run, "source_root": str(self.root),
            "scope": {"sources": manifest, "coverage": "Scripted fixture source only"},
            "evidence": [_passage(sources[name], 1, 1)], "authority": "NONE"})

    def payload(self, handle):
        return self.store.read(handle)["payload"]

    def observe(self, binding=None, roots=True):
        binding = self.binding if binding is None else binding
        self.tick += 1
        # Mock dates exist only in this deterministic test fixture.
        stamp = datetime(2026, 1, 3, 0, 0, self.tick, tzinfo=timezone.utc)
        with patch("lodestone_machinery.organs.watcher.datetime") as clock:
            clock.now.return_value = stamp
            return self.plumber.watch([binding], capture_roots={binding: self.root} if roots else None)

    def holds(self):
        self.file.write_bytes(b"The gate closes at seven.\n")
        return [self.plumber.hold(self.observe()), self.plumber.hold(self.observe())]

    def request(self, held=None, **kwargs):
        held = self.holds() if held is None else held
        return self.plumber.lesson_request(held, lesson="Reopen exact sources before reusing a reviewed answer.",
            producer=kwargs.get("producer", "scripted-proposer"),
            checker=kwargs.get("checker", "scripted-checker"), finding_key=kwargs.get("finding_key"))

    def review(self, request, verdict="PASS"):
        payload = self.payload(request)
        body = payload["request"]
        return {"schema": REVIEW_SCHEMA, "request_handle": request,
                "request_sha256": payload["request_sha256"], "reviewer": body["checker"],
                "verdict": verdict, "layer_verdicts": [
                    {"observation": layer["observation"], "verdict": "SUPPORTED",
                     "reason": "Scripted fixture confirms retained repeated observation."}
                    for layer in body["layers"]],
                "applicability": "SAME_SOURCE_AND_FINDING", "reason": "Scripted fixture review only."}

    def test_watch_requires_explicit_root_and_never_follows_saved_attribution(self):
        with patch("lodestone_machinery.organs.watcher.read_bound_sources",
                   side_effect=AssertionError("missing root must not read attributed path")):
            report = self.payload(self.observe(roots=False))
        self.assertEqual(report["status"], "ATTENTION")
        self.assertEqual(report["issues"][0]["code"], "SOURCE_UNAVAILABLE")

    def test_watch_reopens_current_and_relocated_captures(self):
        report = self.payload(self.observe())
        self.assertEqual((report["status"], report["sources_fresh"]), ("INTACT", 1))
        moved = self.base / "relocated"
        moved.mkdir()
        (moved / "source.txt").write_bytes(self.original)
        self.file.unlink()
        handle = self.plumber.watch([self.binding], capture_roots={self.binding: moved})
        self.assertEqual(self.payload(handle)["status"], "INTACT")

    def test_watch_detects_drift_missing_and_symlink(self):
        self.file.write_bytes(b"Changed.\n")
        report = self.payload(self.observe())
        self.assertEqual(report["issues"][0]["code"], "SOURCE_DRIFT")
        self.assertEqual(report["issues"][0]["source"], "source.txt")
        self.file.unlink()
        self.assertEqual(self.payload(self.observe())["issues"][0]["code"], "SOURCE_UNAVAILABLE")
        outside = self.base / "outside.txt"
        outside.write_bytes(self.original)
        self.file.symlink_to(outside)
        self.assertEqual(self.payload(self.observe())["issues"][0]["code"], "CAPTURE_INVALID")

    def test_bad_root_mapping_and_unreachable_mapping_refuse(self):
        with self.assertRaises(ValueError):
            self.plumber.watch([self.binding], capture_roots={"bad": self.root})
        unrelated = self.store.publish("origami", "unrelated", {})
        with self.assertRaises(ValueError):
            self.plumber.watch([self.binding], capture_roots={unrelated: self.root})

    def test_watcher_keeps_shared_source_read_budget(self):
        # Six individually valid 2 MiB bindings cannot exceed the shared 8 MiB read ceiling.
        handles = []
        for index in range(6):
            name = f"large-{index}.txt"
            (self.root / name).write_bytes(b"x" * (2 * 1024 * 1024 - 1) + b"\n")
            handles.append(self.bind(name, f"large-{index}"))
        report = self.payload(self.plumber.watch(handles, capture_roots={h: self.root for h in handles}))
        self.assertEqual(report["status"], "ATTENTION")
        self.assertTrue(any(x["code"] == "COVERAGE_LIMIT" for x in report["issues"]))
        self.assertLessEqual(report["source_read_bytes_charged"], 8 * 1024 * 1024)

    def test_nacre_preserves_dated_distinct_layers_and_live_seeds(self):
        held = self.holds()
        originals = {h: self.store.path(h).read_bytes() for h in held}
        request = self.request(held)
        lesson = self.plumber.lesson(request, self.review(request))
        payload = self.plumber.read_lesson(lesson)
        self.assertEqual((payload["kind"], payload["state"]), ("NACRE", "LAYERED"))
        self.assertEqual({x["date"] for x in payload["layers"]}, {"2026-01-03"})
        self.assertEqual(len({x["observation"] for x in payload["layers"]}), 2)
        self.assertIn("one continuing incident", payload["layer_scope"])
        self.assertEqual(payload["activation"], "NONE")
        for h in held:
            self.assertEqual(self.store.path(h).read_bytes(), originals[h])
            self.assertEqual(self.payload(h)["status"], "HELD")

    def test_watch_and_hold_never_automatically_make_nacre(self):
        self.holds()
        self.assertNotIn("nacre", {x["kind"] for x in self.store.catalog()["entries"]})

    def test_duplicate_or_single_observation_refuses(self):
        held = self.holds()
        for selected in ([held[0]], [held[0], held[0]]):
            with self.assertRaises(ValueError):
                self.request(selected)

    def test_layers_must_share_exact_source_and_finding(self):
        held = self.holds()
        (self.root / "other.txt").write_bytes(b"Other original.\n")
        other = self.bind("other.txt", "other")
        (self.root / "other.txt").write_bytes(b"Changed too.\n")
        unrelated = self.plumber.hold(self.observe(other))
        with self.assertRaises(ValueError):
            self.request([held[0], unrelated])
        with self.assertRaises(ValueError):
            self.request(held, finding_key="0" * 64)

    def test_same_reviewer_and_producer_refuse(self):
        with self.assertRaises(ValueError):
            self.request(producer="same", checker=" same ")

    def test_wrong_request_hash_reviewer_and_unrelated_pass_refuse(self):
        request = self.request()
        for key, value in (("request_handle", "bezoar:" + "0" * 64),
                           ("request_sha256", "0" * 64), ("reviewer", "someone-else"),
                           ("applicability", "UNRELATED")):
            envelope = self.review(request)
            envelope[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.plumber.lesson(request, envelope)

    def test_review_requires_complete_nonduplicate_layer_support(self):
        request = self.request()
        for mode in ("missing", "duplicate", "unknown", "unsupported"):
            envelope = self.review(request)
            rows = envelope["layer_verdicts"]
            if mode == "missing":
                rows.pop()
            elif mode == "duplicate":
                rows[1] = deepcopy(rows[0])
            else:
                rows[0]["verdict"] = mode.upper()
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.plumber.lesson(request, envelope)

    def test_failed_and_unknown_reviews_are_retained_without_nacre(self):
        request = self.request()
        for verdict in ("FAIL", "UNKNOWN"):
            envelope = self.review(request, verdict)
            envelope["layer_verdicts"][0]["verdict"] = "UNKNOWN"
            handle = self.plumber.lesson(request, envelope)
            self.assertEqual(self.store.read(handle)["kind"], "nacre-review")
            self.assertEqual(self.payload(handle)["status"], "REVIEWED_" + verdict)
        self.assertNotIn("nacre", {x["kind"] for x in self.store.catalog()["entries"]})

    def test_repeated_publication_is_idempotent(self):
        held = self.holds()
        request = self.request(held)
        self.assertEqual(request, self.request(held))
        first = self.plumber.lesson(request, self.review(request))
        before = self.store.catalog()
        self.assertEqual(first, self.plumber.lesson(request, self.review(request)))
        self.assertEqual(self.store.catalog(), before)

    def test_new_clean_observation_does_not_clear_seed_or_rewrite_lesson(self):
        held = self.holds()
        request = self.request(held)
        lesson = self.plumber.lesson(request, self.review(request))
        before = self.store.path(lesson).read_bytes()
        self.file.write_bytes(self.original)
        clean = self.plumber.hold(self.observe())
        self.assertEqual(self.payload(clean)["status"], "OBSERVED")
        self.assertTrue(all(self.payload(h)["status"] == "HELD" for h in held))
        self.assertEqual(self.store.path(lesson).read_bytes(), before)
        self.assertTrue(self.plumber.read_lesson(lesson)["seeds_remain_held"])

    def test_tampered_evidence_and_rehashed_false_state_refuse(self):
        held = self.holds()
        record = self.store.read(held[0])
        forged = deepcopy(record["payload"])
        forged["held_findings"][0]["state"] = "CLEARED"
        bad = self.store.publish("bezoar", "routing", forged, links=record["links"])
        with self.assertRaises(ValueError):
            self.request([bad, held[1]])
        request = self.request(held)
        path = self.store.path(held[0])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.plumber.lesson(request, self.review(request))

    def test_rehashed_lesson_change_is_not_accepted_by_reader(self):
        request = self.request()
        lesson = self.plumber.lesson(request, self.review(request))
        record = self.store.read(lesson)
        for field, value in (("lesson", "Different unreviewed claim."), ("seeds_remain_held", 1)):
            changed = deepcopy(record["payload"])
            changed[field] = value
            forged = self.store.publish("bezoar", "nacre", changed, links=record["links"])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.plumber.read_lesson(forged)

    def test_cli_duplicate_root_and_malformed_review_are_structured_refusals(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            args = ["observe", "--workspace", str(self.store.workspace), "--handle", self.binding]
            args += ["--capture-root", self.binding + "=" + str(self.root)] * 2
            self.assertEqual(main(args, watcher_only=True), 2)
            request = self.request()
            path = self.base / "review.json"
            path.write_text('{"verdict":"PASS","verdict":"FAIL"}', encoding="utf-8")
            self.assertEqual(main(["lesson", "--workspace", str(self.store.workspace),
                                   "--request", request, "--review", str(path)]), 2)


if __name__ == "__main__":
    unittest.main()
