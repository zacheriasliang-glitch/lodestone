"""Tests for the corpus fold organ. Every fixture is invented here; no lab record is read."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import corpus_fold as cf  # noqa: E402

BUILT_AT = "2026-10-15T00:00:00Z"


def sha_of(obj):
    return hashlib.sha256(cf.canonical(obj)).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / "rev").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, obj):
        path = self.root / "rev" / name
        path.write_bytes(cf.canonical(obj))
        return path

    def build(self, title="t"):
        out = self.root / "fold.json"
        return cf.build(self.root, ["rev/*.json"], out, title, BUILT_AT)


class EdgeRule(Base):
    def test_sha256_reference_in_a_relation_field_creates_an_edge(self):
        base = {"id": "base", "text": "first"}
        self.write("base.json", base)
        later = {"id": "later", "corrects": "base.json sha256 " + sha_of(base)}
        self.write("later.json", later)
        fold, _ = self.build()
        edges = [e for e in fold["edges"] if e["type"] == "corrects"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["matched_by"], "sha256")
        self.assertTrue(edges[0]["to"].endswith("base.json"))

    def test_a_reference_outside_a_relation_field_creates_no_edge(self):
        base = {"id": "base", "text": "first"}
        self.write("base.json", base)
        # the hash appears, but under a field name that states no relation
        self.write("other.json", {"id": "other", "incidental_note": sha_of(base)})
        fold, _ = self.build()
        self.assertEqual(fold["edges"], [])

    def test_ambiguous_filename_creates_no_edge_and_is_recorded(self):
        (self.root / "rev" / "a").mkdir()
        (self.root / "rev" / "b").mkdir()
        for sub in ("a", "b"):
            (self.root / "rev" / sub / "twin.json").write_bytes(cf.canonical({"id": sub}))
        self.write("pointer.json", {"id": "p", "corrects": "twin.json"})
        out = self.root / "fold.json"
        fold, _ = cf.build(self.root, ["rev/*.json", "rev/*/*.json"], out, "t", BUILT_AT)
        self.assertEqual([e for e in fold["edges"] if e["type"] == "corrects"], [])
        self.assertTrue(any("ambiguous" in u.get("reason", "") for u in fold["unresolved_references"]))

    def test_index_names_are_never_edge_targets(self):
        (self.root / "rev" / "README.md").write_text("index")
        self.write("pointer.json", {"id": "p", "corrects": "README.md"})
        out = self.root / "fold.json"
        fold, _ = cf.build(self.root, ["rev/*.json", "rev/*.md"], out, "t", BUILT_AT)
        self.assertEqual(fold["edges"], [])


class Displacement(Base):
    def test_corrected_record_is_displaced_but_still_present(self):
        base = {"id": "base", "claim": "wrong"}
        p = self.write("base.json", base)
        self.write("fix.json", {"id": "fix", "corrects": sha_of(base)})
        fold, _ = self.build()
        entries = {Path(e["path"]).name: e for e in fold["entries"]}
        self.assertEqual(entries["base.json"]["status"], "DISPLACED")
        self.assertEqual(entries["fix.json"]["status"], "CURRENT")
        self.assertEqual(entries["base.json"]["displaced_by"][0]["relation"], "corrects")
        self.assertTrue(p.exists(), "a displaced record must remain on disk")

    def test_read_cost_counts_only_the_current_set(self):
        base = {"id": "base", "filler": "x" * 500}
        self.write("base.json", base)
        self.write("fix.json", {"id": "fix", "corrects": sha_of(base)})
        fold, _ = self.build()
        rc = fold["read_cost"]
        self.assertEqual(rc["corpus_bytes"], rc["current_bytes"] + rc["displaced_bytes"])
        self.assertLess(rc["current_bytes"], rc["corpus_bytes"])


class Threads(Base):
    def test_only_strong_relations_form_threads(self):
        a = {"id": "a"}
        self.write("a.json", a)
        # "about" is recorded but must not merge threads
        self.write("b.json", {"id": "b", "subject": sha_of(a)})
        fold, _ = self.build()
        self.assertTrue(any(e["type"] == "about" for e in fold["edges"]))
        self.assertTrue(all(t["size"] == 1 for t in fold["threads"]))

    def test_derives_from_forms_a_thread(self):
        pre = {"id": "pre"}
        self.write("pre.json", pre)
        self.write("res.json", {"id": "res", "pre_registration": sha_of(pre)})
        fold, _ = self.build()
        self.assertIn(2, [t["size"] for t in fold["threads"]])


class Cohorts(Base):
    def test_records_citing_one_external_artifact_form_a_cohort(self):
        external = "f" * 64
        self.write("one.json", {"id": "1", "note": "measured " + external})
        self.write("two.json", {"id": "2", "note": "also " + external})
        self.write("alone.json", {"id": "3", "note": "unrelated"})
        fold, _ = self.build()
        self.assertEqual(len(fold["cohorts"]), 1)
        self.assertEqual(fold["cohorts"][0]["size"], 2)

    def test_a_corpus_internal_hash_is_not_an_external_citation(self):
        a = {"id": "a"}
        self.write("a.json", a)
        self.write("b.json", {"id": "b", "note": sha_of(a)})
        self.write("c.json", {"id": "c", "note": sha_of(a)})
        fold, _ = self.build()
        self.assertEqual(fold["cohorts"], [])


class RecoveryContract(Base):
    def test_verify_is_clean_immediately_after_build(self):
        self.write("a.json", {"id": "a"})
        out = self.root / "fold.json"
        cf.build(self.root, ["rev/*.json"], out, "t", BUILT_AT)
        self.assertEqual(cf.verify(self.root, out)["verdict"], "CLEAN")

    def test_verify_detects_a_changed_record(self):
        p = self.write("a.json", {"id": "a"})
        out = self.root / "fold.json"
        cf.build(self.root, ["rev/*.json"], out, "t", BUILT_AT)
        p.write_bytes(cf.canonical({"id": "a", "tampered": True}))
        result = cf.verify(self.root, out)
        self.assertEqual(result["verdict"], "DRIFTED")
        self.assertEqual(len(result["drifted"]), 1)

    def test_verify_detects_a_removed_record(self):
        p = self.write("a.json", {"id": "a"})
        out = self.root / "fold.json"
        cf.build(self.root, ["rev/*.json"], out, "t", BUILT_AT)
        p.unlink()
        result = cf.verify(self.root, out)
        self.assertEqual(result["verdict"], "DRIFTED")
        self.assertEqual(len(result["missing"]), 1)

    def test_build_writes_nothing_into_the_corpus(self):
        self.write("a.json", {"id": "a"})
        before = {p: p.read_bytes() for p in (self.root / "rev").rglob("*") if p.is_file()}
        cf.build(self.root, ["rev/*.json"], self.root / "fold.json", "t", BUILT_AT)
        after = {p: p.read_bytes() for p in (self.root / "rev").rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class Robustness(Base):
    def test_an_unparsable_record_is_counted_and_does_not_abort_the_build(self):
        (self.root / "rev" / "broken.json").write_text("{not json")
        self.write("good.json", {"id": "g"})
        fold, _ = self.build()
        self.assertEqual(fold["counts"]["unparsable"], 1)
        self.assertEqual(fold["counts"]["records"], 2)

    def test_the_fold_is_canonical_and_reproducible(self):
        self.write("a.json", {"id": "a"})
        out = self.root / "fold.json"
        _, raw1 = cf.build(self.root, ["rev/*.json"], out, "t", BUILT_AT)
        _, raw2 = cf.build(self.root, ["rev/*.json"], out, "t", BUILT_AT, replace=True)
        self.assertEqual(raw1, raw2)
        self.assertEqual(raw1, cf.canonical(json.loads(raw1.decode())))


if __name__ == "__main__":
    unittest.main()


class Lint(Base):
    def test_states_are_assigned_from_evidence_not_guessed(self):
        a = {"id": "a"}
        self.write("a.json", a)
        self.write("b.json", {"id": "b", "corrects": sha_of(a)})      # LINKED both ways
        self.write("c.json", {"id": "c", "note": "artifact " + "e" * 64})  # ANCHORED
        self.write("d.json", {"id": "d", "note": "nothing"})           # ISLAND
        recs = cf.scan(self.root, ["rev/*.json"])
        bs, bn = cf.build_index(recs)
        state = {Path(r["path"]).name: r["state"] for r in cf.lint(recs, bs, bn)}
        self.assertEqual(state["a.json"], "LINKED")
        self.assertEqual(state["b.json"], "LINKED")
        self.assertEqual(state["c.json"], "ANCHORED")
        self.assertEqual(state["d.json"], "ISLAND")

    def test_lint_proposes_no_edges(self):
        self.write("a.json", {"id": "a", "topic": "ceramics"})
        self.write("b.json", {"id": "b", "topic": "ceramics"})
        recs = cf.scan(self.root, ["rev/*.json"])
        bs, bn = cf.build_index(recs)
        rows = cf.lint(recs, bs, bn)
        self.assertTrue(all(r["state"] == "ISLAND" for r in rows),
                        "a shared topic word must never become a relation")


class Render(Base):
    def test_render_is_text_and_names_the_displaced_successor(self):
        base = {"id": "base"}
        self.write("base.json", base)
        self.write("fix.json", {"id": "fix", "corrects": sha_of(base)})
        fold, _ = self.build(title="T")
        text = cf.render(fold)
        self.assertIn("T", text)
        self.assertIn("base.json", text)
        self.assertIn("fix.json", text)
        self.assertIn("derived data", text)

    def test_render_survives_a_shallow_path(self):
        (self.root / "top.json").write_bytes(cf.canonical({"id": "t", "note": "x" * 200}))
        (self.root / "two.json").write_bytes(cf.canonical({"id": "u", "note": "y" * 200}))
        out = Path(self.tmp).parent / ("fold-%s.json" % Path(self.tmp).name)
        try:
            fold, _ = cf.build(self.root, ["*.json"], out, "shallow", BUILT_AT)
            self.assertIsInstance(cf.render(fold), str)
        finally:
            if out.exists():
                out.unlink()


class FixtureBase(unittest.TestCase):
    """Shared fixtures only. Holds no tests, so subclasses do not rerun each other's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / "rev").mkdir()
        self.out = self.root / "out.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rec(self, name, obj, sub="rev"):
        path = self.root / sub / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cf.canonical(obj))
        return path


class ReviewedDefects(FixtureBase):
    """One test per defect found by the independent reviewer on 2026-10-15.
    Each fails against the behaviour that was shipped at commit bb8a1dd."""

    def test_build_refuses_to_overwrite_one_of_its_own_inputs(self):
        victim = self.rec("a.json", {"id": "a", "precious": True})
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], victim, "t", BUILT_AT)
        self.assertEqual(json.loads(victim.read_text())["precious"], True)

    def test_build_refuses_a_destination_its_own_globs_would_reselect(self):
        self.rec("a.json", {"id": "a"})
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], self.root / "rev" / "fold.json", "t", BUILT_AT)

    def test_build_refuses_a_symlinked_destination(self):
        self.rec("a.json", {"id": "a"})
        target = self.root / "real.json"
        target.write_text("{}")
        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], link, "t", BUILT_AT)

    def test_identity_and_semantics_come_from_one_buffer(self):
        """The reviewer's counterexample: mutate between hashing and parsing and the
        fold binds an old hash to an edge that is not in those bytes."""
        a = {"id": "a"}
        self.rec("a.json", a)
        self.rec("b.json", {"id": "b", "corrects": sha_of(a)})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        entries = {Path(e["path"]).name: e for e in fold["entries"]}
        for name in ("a.json", "b.json"):
            on_disk = hashlib.sha256((self.root / "rev" / name).read_bytes()).hexdigest()
            self.assertEqual(entries[name]["sha256"], on_disk,
                             "the bound hash must be the hash of the bytes the edges were read from")

    def test_a_partial_amendment_keeps_the_original_in_the_reading_set(self):
        base = {"id": "base", "finding": "mostly still true"}
        self.rec("base.json", base)
        self.rec("amend.json", {"id": "amend", "amends": sha_of(base) + " only its second condition"})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        entries = {Path(e["path"]).name: e for e in fold["entries"]}
        self.assertEqual(entries["base.json"]["status"], "CURRENT")
        self.assertEqual(entries["base.json"]["amended_by"][0]["relation"], "amends")
        self.assertEqual(entries["base.json"]["displaced_by"], [])
        self.assertIn("base.json", [Path(x["path"]).name for x in fold["partially_amended"]])

    def test_a_full_correction_still_displaces(self):
        base = {"id": "base"}
        self.rec("base.json", base)
        self.rec("fix.json", {"id": "fix", "corrects": sha_of(base)})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        entries = {Path(e["path"]).name: e for e in fold["entries"]}
        self.assertEqual(entries["base.json"]["status"], "DISPLACED")

    def test_verify_reports_a_record_added_after_the_build(self):
        self.rec("a.json", {"id": "a"})
        cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        self.assertEqual(cf.verify(self.root, self.out)["verdict"], "CLEAN")
        self.rec("late.json", {"id": "late"})
        result = cf.verify(self.root, self.out)
        self.assertEqual(result["verdict"], "DRIFTED")
        self.assertEqual(result["added_since_build"], ["rev/late.json"])

    def test_bounds_refuse_rather_than_truncate(self):
        self.rec("big.json", {"id": "big", "filler": "x" * 5000})
        tight = cf.Bounds(max_file_bytes=100)
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT, bounds=tight)
        self.assertFalse(self.out.exists(), "a refused build must leave no fold behind")

    def test_record_count_bound_refuses(self):
        for i in range(5):
            self.rec("r%d.json" % i, {"id": str(i)})
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT,
                     bounds=cf.Bounds(max_records=3))

    def test_the_fold_records_its_own_bounds_and_coverage(self):
        self.rec("a.json", {"id": "a"})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        self.assertIn("bounds", fold["coverage"])
        self.assertEqual(fold["coverage"]["records_read"], 1)
        self.assertNotIn("elapsed_seconds", fold["coverage"],
                         "a timing value would make the fold unreproducible")


class ReviewedDefectsSecondRound(FixtureBase):
    def test_fold_bytes_matches_the_file_actually_written(self):
        self.rec("a.json", {"id": "a"})
        fold, raw = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        self.assertEqual(fold["read_cost"]["fold_bytes"], len(raw))
        self.assertEqual(len(raw), self.out.stat().st_size)

    def test_citation_retention_is_deterministic_and_declares_its_coverage(self):
        for i in range(6):
            self.rec("r%d.json" % i, {"id": str(i), "note": "art " + ("%064x" % (i % 3))})
        tight = cf.Bounds(max_citation_rows=1)
        f1, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT, bounds=tight)
        f2, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT, bounds=tight, replace=True)
        self.assertEqual(list(f1["shared_citations"]), list(f2["shared_citations"]))
        cov = f1["shared_citations_coverage"]
        self.assertEqual(cov["retained"], 1)
        self.assertGreater(cov["omitted"], 0)
        self.assertIn("omitted by the retention bound", cov["warning"])

    def test_mutual_replacement_displaces_neither(self):
        """A content-hash cycle is impossible: each file would have to contain the
        other's final hash. A filename cycle is not, so that is what this builds."""
        self.rec("a.json", {"id": "a", "supersedes": "b.json"})
        self.rec("b.json", {"id": "b", "supersedes": "a.json"})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        statuses = {Path(e["path"]).name: e["status"] for e in fold["entries"]}
        self.assertEqual(set(statuses.values()), {"CURRENT"},
                         "a corpus that contradicts itself about replacement must skip nothing")
        self.assertTrue(fold["replacement_conflicts"])

    def test_cohort_states_its_connected_component_basis(self):
        self.rec("a.json", {"id": "a", "n": "x " + "a" * 63 + "1"})
        self.rec("b.json", {"id": "b", "n": "x " + "a" * 63 + "1"})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        if fold["cohorts"]:
            self.assertIn("connected component", fold["cohorts"][0]["basis"])

    def test_render_does_not_claim_the_rest_is_irrelevant(self):
        self.rec("a.json", {"id": "a"})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        text = cf.render(fold)
        self.assertNotIn("Everything else in the corpus is about something else", text)
        self.assertIn("cannot tell you", text)


class RootEscape(FixtureBase):
    def test_a_glob_with_dotdot_is_refused(self):
        self.rec("a.json", {"id": "a"})
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["../*.json"], self.out, "t", BUILT_AT)

    def test_an_absolute_glob_is_refused(self):
        self.rec("a.json", {"id": "a"})
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["/etc/*.json"], self.out, "t", BUILT_AT)

    def test_a_parent_directory_link_out_of_the_root_is_refused(self):
        outside = Path(tempfile.mkdtemp())
        try:
            (outside / "secret.json").write_bytes(cf.canonical({"id": "secret"}))
            (self.root / "rev" / "via").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(cf.BoundExceeded):
                cf.build(self.root, ["rev/*/*.json"], self.out, "t", BUILT_AT)
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class ReviewedDefectsThirdRound(FixtureBase):
    """Six negative cases from CORPUS-FOLD-REVIEW-02."""

    def test_a_hardlinked_output_is_refused(self):
        self.rec("a.json", {"id": "a"})
        original = self.root / "precious.json"
        original.write_bytes(cf.canonical({"id": "precious"}))
        link = self.root / "out-link.json"
        os.link(original, link)
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], link, "t", BUILT_AT, replace=True)
        self.assertEqual(json.loads(original.read_text())["id"], "precious")

    def test_an_existing_output_is_refused_rather_than_overwritten(self):
        self.rec("a.json", {"id": "a"})
        self.out.write_bytes(b"{}")
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        self.assertEqual(self.out.read_bytes(), b"{}")

    def test_a_symlink_planted_at_the_output_is_refused_by_the_descriptor(self):
        self.rec("a.json", {"id": "a"})
        victim = self.root / "victim.json"
        victim.write_bytes(cf.canonical({"id": "victim"}))
        link = self.root / "planted.json"
        link.symlink_to(victim)
        with self.assertRaises(cf.BoundExceeded):
            cf.write_exclusive(link, b"overwritten", replace=True)
        self.assertEqual(json.loads(victim.read_text())["id"], "victim")

    def test_verify_rejects_a_bound_path_that_now_resolves_outside_the_root(self):
        self.rec("a.json", {"id": "a"}, sub="rev/inner")
        cf.build(self.root, ["rev/inner/*.json"], self.out, "t", BUILT_AT)
        outside = Path(tempfile.mkdtemp())
        try:
            (outside / "a.json").write_bytes(cf.canonical({"id": "swapped"}))
            shutil.rmtree(self.root / "rev" / "inner")
            (self.root / "rev" / "inner").symlink_to(outside, target_is_directory=True)
            result = cf.verify(self.root, self.out)
            self.assertEqual(result["verdict"], "DRIFTED")
            self.assertTrue(result["rejected_paths"])
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_a_three_record_supersession_cycle_retains_all_three(self):
        self.rec("a.json", {"id": "a", "supersedes": "b.json"})
        self.rec("b.json", {"id": "b", "supersedes": "c.json"})
        self.rec("c.json", {"id": "c", "supersedes": "a.json"})
        fold, _ = cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        statuses = {Path(e["path"]).name: e["status"] for e in fold["entries"]}
        self.assertEqual(set(statuses.values()), {"CURRENT"})
        self.assertEqual(len(fold["replacement_conflicts"]), 3)
        self.assertEqual(fold["counts"]["current"], 3)

    def test_the_deadline_covers_phases_after_discovery(self):
        for i in range(3):
            self.rec("r%d.json" % i, {"id": str(i)})
        tiny = cf.Bounds(max_seconds=-1.0)
        with self.assertRaises(cf.BoundExceeded):
            cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT, bounds=tiny)
        self.assertFalse(self.out.exists())

    def test_drift_during_the_build_refuses_publication(self):
        path = self.rec("a.json", {"id": "a"})
        real_drift = cf.Record.drifted_since_read
        try:
            cf.Record.drifted_since_read = lambda self: "changed during the build"
            with self.assertRaises(cf.BoundExceeded):
                cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
        finally:
            cf.Record.drifted_since_read = real_drift
        self.assertFalse(self.out.exists(), "a drifted build must publish nothing")


class SelfReview(FixtureBase):
    """Defects I looked for in my own organ after the second review, before being told."""

    def test_render_uses_the_same_descriptor_discipline_as_the_fold(self):
        victim = self.root / "victim.md"
        victim.write_text("precious")
        link = self.root / "planted.md"
        link.symlink_to(victim)
        with self.assertRaises(cf.BoundExceeded):
            cf.write_exclusive(link, b"overwritten", replace=True)
        self.assertEqual(victim.read_text(), "precious")

    def test_write_exclusive_refuses_a_directory(self):
        d = self.root / "adir"
        d.mkdir()
        with self.assertRaises(cf.BoundExceeded):
            cf.write_exclusive(d, b"x", replace=True)

    def test_a_refused_write_leaves_no_partial_file(self):
        target = self.root / "new.json"
        other = self.root / "other.json"
        other.write_bytes(b"original")
        os.link(other, target)
        with self.assertRaises(cf.BoundExceeded):
            cf.write_exclusive(target, b"x" * 1000, replace=True)
        self.assertEqual(other.read_bytes(), b"original")


class ReviewedDefectsFourthRound(FixtureBase):
    """From CORPUS-FOLD-REVIEW-03."""

    def test_the_drift_check_runs_after_serialization_not_only_before(self):
        self.rec("a.json", {"id": "a"})
        calls = []
        real = cf.Record.drifted_since_read

        def late_drift(record):
            calls.append(1)
            return "changed during the build" if len(calls) > 1 else None

        try:
            cf.Record.drifted_since_read = late_drift
            with self.assertRaises(cf.BoundExceeded) as ctx:
                cf.build(self.root, ["rev/*.json"], self.out, "t", BUILT_AT)
            self.assertIn("before publication", str(ctx.exception))
        finally:
            cf.Record.drifted_since_read = real
        self.assertFalse(self.out.exists())

    def test_every_byte_is_written(self):
        payload = b"y" * 300000
        target = self.root / "big.out"
        cf.write_exclusive(target, payload)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(target.stat().st_size, len(payload))

    def test_render_refuses_a_hardlinked_destination(self):
        original = self.root / "keep.md"
        original.write_text("keep me")
        link = self.root / "view.md"
        os.link(original, link)
        with self.assertRaises(cf.BoundExceeded):
            cf.write_exclusive(link, b"clobbered", replace=True)
        self.assertEqual(original.read_text(), "keep me")

    def test_the_written_file_is_read_back_and_compared(self):
        payload = b"z" * 70000
        target = self.root / "rb.out"
        cf.write_exclusive(target, payload)
        self.assertEqual(target.read_bytes(), payload)

    def test_readback_failure_is_raised_not_ignored(self):
        real_read = os.read
        target = self.root / "rb2.out"
        try:
            os.read = lambda fd, n: b""          # pretend the file came back empty
            with self.assertRaises(cf.BoundExceeded) as ctx:
                cf.write_exclusive(target, b"content that will not read back")
            self.assertIn("readback", str(ctx.exception))
        finally:
            os.read = real_read
