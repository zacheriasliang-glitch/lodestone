#!/usr/bin/env python3
"""Tests for the brief gate.

One test per law, each shaped on a defect that actually happened: a brief
written from a summary rather than the source (B2), a clause asserted with
nothing behind it (B3), a permitted read that the preregistration forbade
and the brief never declared (B4), a source that moved under the brief (B1),
and a brief that lived only in /tmp (B5).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "machinery"))

import check_brief  # noqa: E402

PREREG = textwrap.dedent(
    """\
    # PREREG-FIXTURE v1.0.0

    ## (d) Consumption

    The consumer reads only the constructor's fold from custody, runs one
    encounter with the same epoch as the construction, and records the
    question before opening the fixture.

    ## (i) Custody

    All artifacts live under runs/<run-id>/workspace/. /tmp is not custody.
    """
)

QUOTE_D = (
    "The consumer reads only the constructor's fold from custody, runs one\n"
    "encounter with the same epoch as the construction, and records the\n"
    "question before opening the fixture."
)


def quoted(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


class GateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="briefgate-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.prereg = os.path.join(self.dir, "prereg.md")
        with open(self.prereg, "w", encoding="utf-8") as handle:
            handle.write(PREREG)
        self.digest = hashlib.sha256(PREREG.encode("utf-8")).hexdigest()

    # -- helpers ---------------------------------------------------------

    def write_brief(self, body: str, digest: str | None = None, name: str = "brief.md") -> str:
        path = os.path.join(self.dir, name)
        header = textwrap.dedent(
            """\
            # Brief

            prereg: prereg.md
            prereg_sha256: %s
            run_id: t-0001
            audience: consumer
            """
        ) % (digest or self.digest)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(header + body)
        return path

    def run_gate(self, path: str, custody: bool = False) -> tuple[int, str]:
        argv = [path] + ([] if custody else ["--no-custody"])
        from io import StringIO

        buffer, saved = StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            code = check_brief.main(argv)
        finally:
            sys.stdout = saved
        return code, buffer.getvalue()

    WELL_FORMED = (
        "\n## Bound clauses\n\n### (d)\n\n"
        + quoted(QUOTE_D)
        + "\n\nRead the fold and nothing else.\n\n## Declared additions\n\n- none\n"
    )


class BriefGateTest(GateFixture):
    """One test per law."""

    def test_well_formed_brief_travels(self) -> None:
        code, out = self.run_gate(self.write_brief(self.WELL_FORMED))
        self.assertEqual(code, 0, out)
        self.assertIn("may travel", out)

    def test_b2_paraphrase_is_refused(self) -> None:
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            "> The consumer may read the fold and the runtime documentation.\n\n"
            "Read the fold and the CLI docs.\n\n## Declared additions\n\n- none\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("B2_QUOTES_EXACT", out)
        self.assertIn("does not appear in the preregistration", out)

    def test_b2_trailing_whitespace_is_refused_but_named(self) -> None:
        drifted = QUOTE_D.replace("one\n", "one \n")
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            + quoted(drifted)
            + "\n\nRead the fold.\n\n## Declared additions\n\n- none\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("trailing whitespace", out)

    def test_b1_stale_source_is_refused(self) -> None:
        path = self.write_brief(self.WELL_FORMED)
        with open(self.prereg, "a", encoding="utf-8") as handle:
            handle.write("\nAmended after the brief was written.\n")
        code, out = self.run_gate(path)
        self.assertEqual(code, 2)
        self.assertIn("STALE_SOURCE", out)

    def test_b1_missing_source_is_refused(self) -> None:
        path = self.write_brief(self.WELL_FORMED)
        os.remove(self.prereg)
        code, out = self.run_gate(path)
        self.assertEqual(code, 2)
        self.assertIn("preregistration not found", out)

    def test_b1_unpinned_source_blocks_quote_checking(self) -> None:
        path = os.path.join(self.dir, "unpinned.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "# Brief\n\n## Bound clauses\n\n### (d)\n\n> anything at all\n\nDo it.\n"
                "\n## Declared additions\n\n- none\n"
            )
        code, out = self.run_gate(path)
        self.assertEqual(code, 2)
        self.assertIn("B1_SOURCE_PINNED", out)
        self.assertIn("the source is unpinned", out)

    def test_b3_instruction_without_a_quote_is_refused(self) -> None:
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            + quoted(QUOTE_D)
            + "\n\nRead the fold.\n\n### (f)\n\nPass roles as session identifiers.\n"
            "\n## Declared additions\n\n- none\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("nothing quoted behind it", out)

    def test_b3_quote_without_an_instruction_is_refused(self) -> None:
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            + quoted(QUOTE_D)
            + "\n\n## Declared additions\n\n- none\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("instructs nobody", out)

    def test_b3_instruction_outside_a_clause_is_refused(self) -> None:
        body = (
            "\n## Bound clauses\n\nAlso, use the current epoch.\n\n### (d)\n\n"
            + quoted(QUOTE_D)
            + "\n\nRead the fold.\n\n## Declared additions\n\n- none\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("outside any clause block", out)

    def test_b4_missing_additions_section_is_refused(self) -> None:
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            + quoted(QUOTE_D)
            + "\n\nRead the fold.\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("B4_ADDITIONS_DECLARED", out)

    def test_b4_empty_additions_section_is_refused(self) -> None:
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            + quoted(QUOTE_D)
            + "\n\nRead the fold.\n\n## Declared additions\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 2)
        self.assertIn("B4_ADDITIONS_DECLARED", out)

    def test_fenced_block_is_not_read_as_structure(self) -> None:
        body = (
            "\n## Bound clauses\n\n### (d)\n\n"
            + quoted(QUOTE_D)
            + "\n\nRun this:\n\n```\n## Declared additions\n> not a quote\n```\n"
            "\n## Declared additions\n\n- none\n"
        )
        code, out = self.run_gate(self.write_brief(body))
        self.assertEqual(code, 0, out)


@unittest.skipUnless(shutil.which("git"), "git not available")
class CustodyTest(GateFixture):
    def setUp(self) -> None:
        super().setUp()
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        self.env = dict(os.environ, **env)
        self.git("init", "-q")
        self.git("config", "user.email", "gate@example.invalid")
        self.git("config", "user.name", "brief gate test")

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args),
            cwd=self.dir,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )

    def test_b5_untracked_brief_is_refused(self) -> None:
        path = self.write_brief(self.WELL_FORMED)
        code, out = self.run_gate(path, custody=True)
        self.assertEqual(code, 2)
        self.assertIn("not tracked", out)

    def test_b5_dirty_brief_is_refused(self) -> None:
        path = self.write_brief(self.WELL_FORMED)
        self.git("add", ".")
        self.git("commit", "-qm", "brief")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\nOne more thing, off the record.\n")
        code, out = self.run_gate(path, custody=True)
        self.assertEqual(code, 2)
        self.assertIn("uncommitted changes", out)

    def test_b5_committed_brief_travels(self) -> None:
        path = self.write_brief(self.WELL_FORMED)
        self.git("add", ".")
        self.git("commit", "-qm", "brief")
        code, out = self.run_gate(path, custody=True)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
