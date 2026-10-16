"""Falsifier suite for the Yggdrasil lifecycle lineage laws.

Each law is falsifiable in one direction that matters: a constructed corpus
that violates it must be rejected. A law that cannot fail on any input is not a
law, so every check below has both a positive control (a well-formed corpus
that passes) and a constructed violation that must be reported.

The final test reads the live repository. It asserts that the instrument runs
and reports, not that the corpus passes: the live verdict is an observation to
be recorded, never a target to tune the instrument against.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from lodestone_machinery.yggdrasil import (MAX_RECEIPT_BYTES, MODES, VERSION,  # noqa: E402
                                           check, load, verify)

REPOSITORY = Path(__file__).resolve().parents[2]


def receipt_name(stamp, slug, suffix):
    return f"{stamp}__{slug}__RCP-{stamp}-{suffix}.md"


def write_receipt(root, stamp, slug, suffix, mode="write", declared=None, form="json", join=None):
    """Write one synthetic receipt in one of the four shapes the corpus uses.

    `join` is `(predecessors, sets)`: a JSON-only join declaration naming two
    predecessors and the two predecessor sets they are the newest members of.
    """
    path = root / "receipts" / receipt_name(stamp, slug, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    if form == "json":
        document = {"receipt_id": f"RCP-{stamp}-{suffix}", "operation_name": slug, "mode": mode}
        if declared is not None:
            document["receipt_directory_path_set_before_creation"] = list(declared)
        if join is not None:
            document["join_predecessors"] = list(join[0])
            document["receipt_directory_path_sets_before_creation"] = [list(group) for group in join[1]]
        path.write_text(f"# Receipt\n\n```json\n{json.dumps(document, indent=2)}\n```\n", encoding="utf-8")
    elif form == "bullet":
        body = [f"# Receipt", "", f"- receipt_id: `RCP-{stamp}-{suffix}`", f"- mode: `{mode}`"]
        if declared is not None:
            body += ["", "## receipt_directory_path_set_before_creation", ""]
            body += [f"- `{entry}`" for entry in declared]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    elif form == "fenced":
        body = [f"# Receipt", "", "## mode", "", f"{mode}. Prose follows the token.", ""]
        if declared is not None:
            body += ["## receipt_directory_path_set_before_creation", "",
                     f"{len(declared)} paths observed before this receipt:", "", "```text"]
            body += list(declared)
            body += ["```", ""]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    elif form == "bare":
        body = [f"receipt_id: RCP-{stamp}-{suffix}", f"operation_name: {slug}", f"mode: {mode}"]
        if declared is not None:
            body += ["", "## receipt_directory_path_set_before_creation", ""]
            body += [f"- `{entry}`" for entry in declared]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    else:
        raise ValueError(f"unknown form {form!r}")
    return f"receipts/{path.name}"


class Corpus:
    """A disposable repository root holding only a receipts directory."""

    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        (self.root / "receipts").mkdir()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self._temporary.cleanup()
        return False

    def add(self, stamp, slug, suffix, **kwargs):
        return write_receipt(self.root, stamp, slug, suffix, **kwargs)

    def verdict(self):
        return check(self.root)


def laws_reported(result):
    return {finding["law"] for finding in result["findings"]}


class PositiveControlTests(unittest.TestCase):
    """A corpus that satisfies every law must pass, or the suite proves nothing."""

    def test_wellformed_chain_passes(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            second = corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first])
            corpus.add("20260103T000000Z", "third", "0123456789ad", declared=[first, second])
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "PASS", result["findings"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["lifecycles"], 3)
        self.assertEqual(result["declaring_custody"], 3)
        self.assertEqual(len(result["heads"]), 1)

    def test_every_policy_mode_is_accepted(self):
        for mode in MODES:
            with self.subTest(mode=mode), Corpus() as corpus:
                corpus.add("20260101T000000Z", "only", "0123456789ab", mode=mode, declared=[])
                self.assertEqual(corpus.verdict()["verdict"], "PASS")


class Y1OneTrunkTests(unittest.TestCase):
    """'Not one Hydra from within': two lifecycles may not share a predecessor set."""

    def test_fork_is_rejected(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            corpus.add("20260102T000000Z", "left", "0123456789ac", declared=[first])
            corpus.add("20260103T000000Z", "right", "0123456789ad", declared=[first])
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "VIOLATION")
        self.assertIn("Y1_ONE_TRUNK", laws_reported(result))
        self.assertEqual(result["counts"]["Y1_ONE_TRUNK"], 1)

    def test_joined_fork_clears_y1_y3_and_y4(self):
        """Host ruling 2026-09-20: a later join naming both heads closes the fork."""
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            left = corpus.add("20260102T000000Z", "left", "0123456789ac", declared=[first])
            right = corpus.add("20260103T000000Z", "right", "0123456789ad", declared=[first])
            join = corpus.add("20260104T000000Z", "join", "0123456789ae",
                              join=([left, right], [[first, left], [first, right]]))
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "PASS", result["findings"])
        self.assertEqual(result["counts"]["Y1_ONE_TRUNK"], 0)
        self.assertEqual(result["counts"]["Y3_CLOSED_PREFIX"], 0)
        self.assertEqual(result["heads"], [join])
        self.assertEqual(result["joins"], [{"receipt": join, "predecessors": [left, right]}])

    def test_join_naming_one_predecessor_is_rejected(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            left = corpus.add("20260102T000000Z", "left", "0123456789ac", declared=[first])
            corpus.add("20260103T000000Z", "right", "0123456789ad", declared=[first])
            for predecessors, sets in (([left, left], [[first, left], [first, left]]),
                                       ([left], [[first, left]]),
                                       ([left, first], [[first, left], [first, left]])):
                with self.subTest(predecessors=predecessors):
                    bad = corpus.add("20260104T000000Z", "join", "0123456789ae", join=(predecessors, sets))
                    result = corpus.verdict()
                    (corpus.root / bad).unlink()
                    self.assertEqual(result["counts"]["Y1_ONE_TRUNK"], 1)
                    self.assertIn("IDENTITY", laws_reported(result))
                    self.assertEqual(result["joins"], [])

    def test_join_over_other_lifecycles_does_not_excuse_the_fork(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            left = corpus.add("20260102T000000Z", "left", "0123456789ac", declared=[first])
            corpus.add("20260103T000000Z", "right", "0123456789ad", declared=[first])
            corpus.add("20260104T000000Z", "join", "0123456789ae",
                       join=([first, left], [[first], [first, left]]))
            result = corpus.verdict()
        self.assertEqual(result["counts"]["Y1_ONE_TRUNK"], 1)

    def test_undeclared_lifecycles_do_not_forge_a_trunk(self):
        """Silence must not be given a derived parent, which would make Y1 vacuous."""
        with Corpus() as corpus:
            corpus.add("20260101T000000Z", "first", "0123456789ab")
            corpus.add("20260102T000000Z", "second", "0123456789ac")
            result = corpus.verdict()
        self.assertEqual(result["declaring_custody"], 0)
        self.assertEqual(result["coverage"]["Y1_ONE_TRUNK"], 0)
        self.assertNotIn("Y1_ONE_TRUNK", laws_reported(result))


class Y2OneOrganTests(unittest.TestCase):
    """One selected organ per lifecycle: exactly one mode, from the policy set."""

    def test_mode_outside_the_policy_set_is_rejected(self):
        with Corpus() as corpus:
            corpus.add("20260101T000000Z", "only", "0123456789ab", mode="BUILDER_EXECUTION", declared=[])
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "VIOLATION")
        self.assertIn("Y2_ONE_ORGAN", laws_reported(result))

    def test_two_declared_modes_are_rejected(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\n- mode: `write`\n- mode: `read-only`\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "VIOLATION")
        self.assertIn("Y2_ONE_ORGAN", laws_reported(result))

    def test_absent_mode_is_coverage_not_violation(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\nNo mode is declared here.\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["coverage"]["Y2_ONE_ORGAN"], 0)
        self.assertNotIn("Y2_ONE_ORGAN", laws_reported(result))

    def test_numeric_file_permission_is_not_read_as_a_mode(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\n- mode: `0600`\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertNotIn("Y2_ONE_ORGAN", laws_reported(result))
        self.assertEqual(result["coverage"]["Y2_ONE_ORGAN"], 0)


class Y3ClosedPrefixTests(unittest.TestCase):
    """Append-only custody: a declared set is exactly the lifecycles preceding it."""

    def test_join_omitting_a_predecessor_prefix_member_is_rejected(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            left = corpus.add("20260102T000000Z", "left", "0123456789ac", declared=[first])
            right = corpus.add("20260103T000000Z", "right", "0123456789ad", declared=[first])
            corpus.add("20260104T000000Z", "join", "0123456789ae",
                       join=([left, right], [[left], [right]]))
            result = corpus.verdict()
        self.assertEqual(result["counts"]["Y1_ONE_TRUNK"], 0)
        self.assertEqual(result["counts"]["Y3_CLOSED_PREFIX"], 1)
        self.assertIn(first, result["findings"][-1]["detail"])

    def test_omitted_predecessor_is_rejected(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first])
            corpus.add("20260103T000000Z", "third", "0123456789ad", declared=[first])
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "VIOLATION")
        self.assertIn("Y3_CLOSED_PREFIX", laws_reported(result))
        self.assertTrue(any("omits the preceding lifecycle" in finding["detail"]
                            for finding in result["findings"]))

    def test_entry_naming_a_later_lifecycle_is_rejected(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            later = receipt_name("20260103T000000Z", "third", "0123456789ad")
            corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first, f"receipts/{later}"])
            corpus.add("20260103T000000Z", "third", "0123456789ad", declared=[first])
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "VIOLATION")
        self.assertTrue(any("does not precede it" in finding["detail"]
                            for finding in result["findings"]))

    def test_entry_naming_a_missing_file_is_reported_as_absent(self):
        """A transcription error names a path that is not in the corpus at all."""
        with Corpus() as corpus:
            ghost = "receipts/" + receipt_name("20251231T000000Z", "ghost", "0123456789ff")
            corpus.add("20260101T000000Z", "only", "0123456789ab", declared=[ghost])
            result = corpus.verdict()
        self.assertEqual(result["absent_declared_entries"], [ghost])
        self.assertNotIn("Y3_CLOSED_PREFIX", laws_reported(result))


class Y4OneHeadTests(unittest.TestCase):
    """'One voxel from outside': the declared subgraph presents exactly one head."""

    def test_broken_chain_reports_two_heads(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            second = corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first])
            gap = corpus.add("20260103T000000Z", "gap", "0123456789ad")
            corpus.add("20260104T000000Z", "fourth", "0123456789ae", declared=[first, second, gap])
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "VIOLATION")
        self.assertIn("Y4_ONE_HEAD", laws_reported(result))
        self.assertEqual(len(result["heads"]), 2)
        self.assertNotIn(gap, result["heads"])

    def test_heads_are_drawn_only_from_declaring_lifecycles(self):
        """Y4's domain is the declared subgraph, not every lifecycle."""
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            second = corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first])
            corpus.add("20260103T000000Z", "silent", "0123456789ad")
            result = corpus.verdict()
        self.assertEqual(result["lifecycles"], 3)
        self.assertEqual(result["declaring_custody"], 2)
        self.assertEqual(result["heads"], [second])
        self.assertNotIn("Y4_ONE_HEAD", laws_reported(result))


class IdentityTests(unittest.TestCase):
    """Receipt identity is the policy's, not an invented one."""

    def test_noncanonical_receipt_id_is_reported(self):
        with Corpus() as corpus:
            corpus.add("20260101T000000Z", "only", "bootstrap-p08", declared=[])
            result = corpus.verdict()
        self.assertIn("IDENTITY", laws_reported(result))

    def test_declared_id_must_match_the_filename(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text('# Receipt\n\n```json\n{"receipt_id": "RCP-20260101T000000Z-ffffffffffff",'
                            ' "mode": "write"}\n```\n', encoding="utf-8")
            result = corpus.verdict()
        self.assertTrue(any("does not match the filename" in finding["detail"]
                            for finding in result["findings"]))

    def test_non_receipt_file_is_not_a_lifecycle(self):
        with Corpus() as corpus:
            corpus.add("20260101T000000Z", "only", "0123456789ab", declared=[])
            handshake = corpus.root / "receipts" / "blink-handshake"
            handshake.mkdir()
            (handshake / "claude.md").write_text("not a receipt\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["lifecycles"], 1)
        self.assertEqual(result["non_lifecycle_files"], ["receipts/blink-handshake/claude.md"])
        self.assertEqual(result["verdict"], "PASS")


class ParsingTests(unittest.TestCase):
    """Every declaration shape the corpus actually uses must read identically."""

    def test_all_four_shapes_yield_the_same_lineage(self):
        for form in ("json", "bullet", "fenced", "bare"):
            with self.subTest(form=form), Corpus() as corpus:
                first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[], form=form)
                corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first], form=form)
                result = corpus.verdict()
                self.assertEqual(result["verdict"], "PASS", (form, result["findings"]))
                self.assertEqual(result["declaring_custody"], 2, form)
                self.assertEqual(result["coverage"]["Y2_ONE_ORGAN"], 2, form)

    def test_receipt_paths_cited_outside_the_declaration_are_ignored(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            path = corpus.root / "receipts" / receipt_name("20260102T000000Z", "second", "0123456789ac")
            path.write_text("# Receipt\n\n- mode: `write`\n\n## objective\n\n"
                            f"Correct the transcription in `{first}` without editing it.\n",
                            encoding="utf-8")
            lifecycles, _ = load(corpus.root)
        self.assertIsNone(lifecycles[1].declared)

    def test_malformed_json_block_is_reported_not_raised(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\n```json\n{not valid json}\n```\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertTrue(any("json block does not parse" in finding["detail"]
                            for finding in result["findings"]))


class DeclarationScopeTests(unittest.TestCase):
    """A declaration must be read from its declaration, and from nowhere else.

    These guard the parsing layer rather than a law body. Each one failed, or
    would have passed against a reverted correction, before it was written.
    """

    def test_unrelated_json_block_does_not_suppress_a_fork(self):
        """A stray JSON block must not switch Y1, Y3 and Y4 off for a receipt.

        A receipt carries JSON for many reasons -- hashes, an inventory, a
        quoted prior record. If any of them were taken as the receipt's own
        document, a receipt declaring custody in Markdown would read as
        declaring nothing, and a lifecycle that declares nothing is excluded
        from three laws. The fork below must be reported either way.
        """
        for stray in (False, True):
            with self.subTest(stray_json=stray), Corpus() as corpus:
                first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[], form="bullet")
                for stamp, slug, suffix in (("20260102T000000Z", "left", "0123456789ac"),
                                            ("20260103T000000Z", "right", "0123456789ad")):
                    path = corpus.root / "receipts" / receipt_name(stamp, slug, suffix)
                    body = ["# Receipt", "", "- mode: `write`", "",
                            "## receipt_directory_path_set_before_creation", "", f"- `{first}`"]
                    if stray:
                        body += ["", "## hashes_after", "", "```json",
                                 json.dumps({"hashes_after": {"a.md": {"sha256": "0" * 64}}}), "```"]
                    path.write_text("\n".join(body) + "\n", encoding="utf-8")
                result = corpus.verdict()
                self.assertEqual(result["declaring_custody"], 3, stray)
                self.assertIn("Y1_ONE_TRUNK", laws_reported(result), stray)

    def test_the_receipts_own_json_document_is_chosen_by_content(self):
        """A quoted prior record must not supply this receipt's identity or mode."""
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            quoted = json.dumps({"receipt_id": "RCP-20251231T000000Z-ffffffffffff", "mode": "BOGUS"})
            mine = json.dumps({"receipt_id": "RCP-20260101T000000Z-0123456789ab", "mode": "write",
                               "receipt_directory_path_set_before_creation": []})
            path.write_text("# Receipt\n\n## observed_inputs\n\nThe prior record read:\n\n```json\n"
                            + quoted + "\n```\n\n## this receipt\n\n```json\n" + mine + "\n```\n",
                            encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["verdict"], "PASS", result["findings"])

    def test_a_declared_set_does_not_absorb_paths_cited_after_it(self):
        """The N1 case: a real declaration plus an unrelated citation below it."""
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            later = "receipts/" + receipt_name("20260109T000000Z", "later", "0123456789af")
            path = corpus.root / "receipts" / receipt_name("20260102T000000Z", "second", "0123456789ac")
            path.write_text("# Receipt\n\n- mode: `write`\n"
                            f"- receipt_directory_path_set_before_creation:\n  - `{first}`\n"
                            f"\nSee also the unrelated note about `{later}` further down.\n",
                            encoding="utf-8")
            lifecycles, _ = load(corpus.root)
            result = corpus.verdict()
        self.assertEqual(lifecycles[1].declared, [first])
        self.assertEqual(result["verdict"], "PASS", result["findings"])

    def test_markdown_link_paths_do_not_capture_the_closing_bracket(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            path = corpus.root / "receipts" / receipt_name("20260102T000000Z", "second", "0123456789ac")
            path.write_text("# Receipt\n\n- mode: `write`\n\n"
                            "## receipt_directory_path_set_before_creation\n\n"
                            f"- [first](../{first})\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["absent_declared_entries"], [])
        self.assertEqual(result["verdict"], "PASS", result["findings"])

    def test_a_mode_quoted_in_a_later_section_is_not_a_second_declaration(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\n- mode: `write`\n\n"
                            "## receipt_directory_path_set_before_creation\n\n"
                            "## rollback_procedure\n\n" + "\n".join(["Filler line."] * 45)
                            + "\nA rollback operation is filed with `- mode: `rollback``.\n",
                            encoding="utf-8")
            result = corpus.verdict()
        self.assertNotIn("Y2_ONE_ORGAN", laws_reported(result))

    def test_a_trailing_parenthetical_does_not_hide_a_mode(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\n- MODE: READ_ONLY (plus the two authorized writes below)\n",
                            encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["coverage"]["Y2_ONE_ORGAN"], 1)
        self.assertTrue(any("READ_ONLY" in finding["detail"] for finding in result["findings"]))

    def test_policy_membership_is_exact_but_distinctness_is_not(self):
        """`READ_ONLY` is not `read-only`; `write` and `Write` are one declaration."""
        with Corpus() as corpus:
            corpus.add("20260101T000000Z", "cased", "0123456789ab", mode="READ_ONLY", declared=[])
            result = corpus.verdict()
        self.assertIn("Y2_ONE_ORGAN", laws_reported(result))
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "only", "0123456789ab")
            path.write_text("# Receipt\n\n- mode: `write`\n- Mode: Write\n", encoding="utf-8")
            result = corpus.verdict()
        self.assertNotIn("Y2_ONE_ORGAN", laws_reported(result))


class ReadBoundTests(unittest.TestCase):
    """A receipt too large to read is reported, and claims no coverage."""

    def test_oversized_receipt_is_unread_and_uncovered(self):
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "huge", "0123456789ab")
            path.write_text("# Receipt\n\n- mode: `NOT_A_MODE`\n" + "x" * (MAX_RECEIPT_BYTES + 1),
                            encoding="utf-8")
            result = corpus.verdict()
        self.assertEqual(result["unread"], [f"receipts/{path.name}"])
        self.assertIn("UNREAD", laws_reported(result))
        self.assertEqual(result["coverage"]["Y2_ONE_ORGAN"], 0)
        self.assertNotIn("Y2_ONE_ORGAN", laws_reported(result))

    def test_an_ordinary_large_receipt_is_still_read(self):
        """The bound is pinned from below: real receipts run to tens of kilobytes.

        Without this, narrowing the bound would silently exempt ordinary
        receipts from every law while the oversized fixture above still passed.
        """
        with Corpus() as corpus:
            path = corpus.root / "receipts" / receipt_name("20260101T000000Z", "large", "0123456789ab")
            padding = "\n".join(["Ordinary evidence prose."] * 4000)
            path.write_text("# Receipt\n\n- mode: `BUILDER_EXECUTION`\n\n" + padding + "\n",
                            encoding="utf-8")
            self.assertGreater(path.stat().st_size, 64 * 1024)
            result = corpus.verdict()
        self.assertEqual(result["unread"], [])
        self.assertEqual(result["coverage"]["Y2_ONE_ORGAN"], 1)
        self.assertIn("Y2_ONE_ORGAN", laws_reported(result))


class DeterminismTests(unittest.TestCase):
    def test_verdict_is_stable_and_serialisable(self):
        with Corpus() as corpus:
            first = corpus.add("20260101T000000Z", "first", "0123456789ab", declared=[])
            corpus.add("20260102T000000Z", "second", "0123456789ac", declared=[first])
            lifecycles, others = load(corpus.root)
            once = json.dumps(verify(lifecycles, others), sort_keys=True)
            twice = json.dumps(verify(*load(corpus.root)), sort_keys=True)
        self.assertEqual(once, twice)

    def test_missing_receipts_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ValueError):
                load(empty)


@unittest.skipUnless((REPOSITORY / "receipts").is_dir(),
                     "no receipts corpus here; the portable harness ships without one")
class LiveCorpusTests(unittest.TestCase):
    """Observe the real corpus. The verdict is recorded, never asserted.

    These two observe a corpus that exists only in the laboratory repository.
    The clean harness is a portable source package with no receipts directory,
    so there is nothing to observe there. Every law is exercised against
    synthetic corpora by the tests above, which run in either environment.
    """

    def test_live_corpus_is_readable_and_reports_coverage(self):
        result = check(REPOSITORY)
        self.assertEqual(result["version"], VERSION)
        self.assertIn(result["verdict"], ("PASS", "VIOLATION"))
        self.assertGreater(result["lifecycles"], 0)
        self.assertLessEqual(result["coverage"]["Y1_ONE_TRUNK"], result["lifecycles"])
        self.assertEqual(json.loads(json.dumps(result))["findings"], result["findings"])

    def test_live_corpus_has_no_fork(self):
        """Y1 holds on the live record: no unjoined fork. A joined fork is not a fork.

        Host ruling 2026-09-20: two lifecycles that share a predecessor set are
        excused when a later join names both. Every join the instrument reports
        must name two existing lifecycles; a future unjoined fork fails this test.
        """
        result = check(REPOSITORY)
        self.assertEqual(result["counts"]["Y1_ONE_TRUNK"], 0, result["findings"])
        paths = {entry.path for entry in load(REPOSITORY)[0]}
        for join in result["joins"]:
            self.assertEqual(len(set(join["predecessors"])), 2)
            self.assertLessEqual(set(join["predecessors"]), paths)


if __name__ == "__main__":
    unittest.main()
