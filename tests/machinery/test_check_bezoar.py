#!/usr/bin/env python3
"""Tests for the node registry gate.

One test per law, both kinds, and the three reconciliation directions: a
finding no node holds, a seed claiming to be cleared while its finding is
still emitted, and a pearl holding a live matter.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from io import StringIO

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "machinery"))

import check_bezoar  # noqa: E402

GOOD = textwrap.dedent(
    """\
    # Bezoar

    ## BZ-0001

        kind: SEED
        law: Y2_ONE_ORGAN
        subject: RCP-aaaa, RCP-bbbb
        first_seen: d584ca7
        state: HELD
        clears_when: each receipt declares exactly one organ, or a later
            receipt supersedes both

    Two receipts name two organs each. One cause: the writer treated the
    organ field as a list of everything touched.

    ## BZ-0002

        kind: SEED
        law: Y4_ONE_HEAD
        subject: RCP-cccc
        first_seen: d584ca7
        state: WAIVED
        waived_by: HOST
        clears_when: NONE POSSIBLE

    A historical gap. Receipts are immutable, so nothing can close it.

    ## BZ-0003

        kind: NACRE
        law: BRIEF_FROM_SUMMARY
        subject: sf01-consumer-brief
        first_seen: 66cd43b
        state: LAYERED
        adage: asking for the file costs a relay; writing without it costs the run

        - 2026-09-20  first brief: wrong epoch, two forbidden reads, no custody
        - 2026-09-20  rerun brief: custody path wrong again, same cause

    Written from a summary of the preregistration rather than the file, twice
    in one hour. The second layer is what named the mechanism.
    """
)

FINDINGS = (
    "Y2_ONE_ORGAN\tRCP-aaaa\n"
    "Y2_ONE_ORGAN\tRCP-bbbb\n"
    "Y4_ONE_HEAD\tRCP-cccc\n"
)


class BezoarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="bezoar-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, text: str, name: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def gate(self, registry_text: str, findings_text: str | None = FINDINGS):
        registry = self.write(registry_text, "bezoar.md")
        argv = [registry]
        if findings_text is not None:
            argv += ["--findings", self.write(findings_text, "findings.tsv")]
        buffer, saved = StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            code = check_bezoar.main(argv)
        finally:
            sys.stdout = saved
        return code, buffer.getvalue()

    # -- shape -----------------------------------------------------------

    def test_well_formed_registry_passes(self) -> None:
        code, out = self.gate(GOOD)
        self.assertEqual(code, 0, out)
        self.assertIn("held by a seed", out)

    def test_one_seed_covers_several_subjects(self) -> None:
        code, out = self.gate(GOOD)
        self.assertEqual(code, 0, out)
        self.assertIn("SEED:      2", out)
        self.assertIn("NACRE:     1   layers 2", out)

    def test_n1_duplicate_id(self) -> None:
        code, out = self.gate(GOOD + GOOD.split("# Bezoar\n", 1)[1])
        self.assertEqual(code, 2)
        self.assertIn("N1_ID_UNIQUE", out)

    def test_n2_missing_field(self) -> None:
        code, out = self.gate(GOOD.replace("    first_seen: d584ca7\n", "", 1))
        self.assertEqual(code, 2)
        self.assertIn("first_seen", out)

    def test_n2_unknown_kind(self) -> None:
        code, out = self.gate(GOOD.replace("kind: SEED", "kind: STONE", 1))
        self.assertEqual(code, 2)
        self.assertIn("is not one of SEED / NACRE", out)

    def test_n2_node_with_no_prose(self) -> None:
        text = GOOD.replace(
            "Two receipts name two organs each. One cause: the writer treated the\n"
            "organ field as a list of everything touched.\n",
            "",
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("a label, not an object", out)

    def test_n2_seed_without_clearing_condition(self) -> None:
        text = GOOD.replace(
            "    clears_when: each receipt declares exactly one organ, or a later\n"
            "        receipt supersedes both\n",
            "",
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("a SEED needs 'clears_when:'", out)

    # -- states ----------------------------------------------------------

    def test_n3_illegal_seed_state(self) -> None:
        code, out = self.gate(GOOD.replace("state: HELD", "state: PENDING"))
        self.assertEqual(code, 2)
        self.assertIn("N3_STATE_LEGAL", out)

    def test_n3_cleared_without_citation(self) -> None:
        code, out = self.gate(GOOD.replace("state: HELD", "state: CLEARED"))
        self.assertEqual(code, 2)
        self.assertIn("does not clear by say-so", out)

    def test_n3_waived_without_owner(self) -> None:
        code, out = self.gate(GOOD.replace("    waived_by: HOST\n", ""))
        self.assertEqual(code, 2)
        self.assertIn("a waiver needs an owner", out)

    def test_n3_nacre_cannot_be_cleared(self) -> None:
        code, out = self.gate(GOOD.replace("state: LAYERED", "state: CLEARED"))
        self.assertEqual(code, 2)
        self.assertIn("a pearl is not cleared or waived", out)

    def test_n4_none_possible_cannot_be_cleared(self) -> None:
        text = GOOD.replace("state: WAIVED", "state: CLEARED").replace(
            "    waived_by: HOST\n", "    cleared_by: commit abc1234\n"
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("NONE POSSIBLE but the node is CLEARED", out)

    def test_n4_nacre_may_not_have_a_clearing_condition(self) -> None:
        text = GOOD.replace(
            "    adage: asking for the file costs a relay; writing without it costs the run\n",
            "    adage: a lesson\n    clears_when: never happening again\n",
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("the mistake already happened", out)

    def test_n6_single_layer_is_not_a_pearl(self) -> None:
        text = GOOD.replace(
            "    - 2026-09-20  rerun brief: custody path wrong again, same cause\n", ""
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("one instance is an accident", out)

    def test_n6_nacre_without_adage(self) -> None:
        text = GOOD.replace(
            "    adage: asking for the file costs a relay; writing without it costs the run\n",
            "",
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("the lesson is the reason it is kept", out)

    # -- reconciliation --------------------------------------------------

    def test_n5_finding_held_by_no_node(self) -> None:
        code, out = self.gate(GOOD, FINDINGS + "Y3_CLOSED_PREFIX\tRCP-dddd\n")
        self.assertEqual(code, 2)
        self.assertIn("held by no node", out)

    def test_n5_cleared_seed_whose_finding_still_fires(self) -> None:
        text = GOOD.replace(
            "    state: HELD\n",
            "    state: CLEARED\n    cleared_by: commit abc1234\n",
            1,
        )
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("still emitted", out)

    def test_n5_pearl_may_not_hold_a_live_finding(self) -> None:
        code, out = self.gate(
            GOOD, FINDINGS + "BRIEF_FROM_SUMMARY\tsf01-consumer-brief\n"
        )
        self.assertEqual(code, 2)
        self.assertIn("wants a crystal, not an archive", out)

    def test_partial_subject_coverage_fails(self) -> None:
        text = GOOD.replace("subject: RCP-aaaa, RCP-bbbb", "subject: RCP-aaaa")
        code, out = self.gate(text)
        self.assertEqual(code, 2)
        self.assertIn("RCP-bbbb", out)

    def test_no_findings_file_leaves_n5_unchecked(self) -> None:
        code, out = self.gate(GOOD, None)
        self.assertEqual(code, 0, out)
        self.assertIn("N5 unchecked", out)

    def test_fenced_block_is_not_read_as_structure(self) -> None:
        code, out = self.gate(GOOD + "\n```\n## BZ-9999\n    kind: FAKE\n```\n")
        self.assertEqual(code, 0, out)

    def test_double_space_findings_format(self) -> None:
        code, out = self.gate(
            GOOD,
            "Y2_ONE_ORGAN  RCP-aaaa\nY2_ONE_ORGAN  RCP-bbbb\nY4_ONE_HEAD  RCP-cccc\n",
        )
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
