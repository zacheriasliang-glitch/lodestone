"""Mutation operand checks; no inspected command is ever executed."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "tools/ecosystem/conscience_targets.py"
spec = importlib.util.spec_from_file_location("conscience_targets_under_test", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MutationTargetsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name).resolve()

    def inspect(self, *argv):
        return module.inspect_mutations(argv, self.cwd)

    def test_copy_reads_source_and_writes_only_destination(self):
        result = self.inspect("cp", "old.json", "new.json")
        self.assertEqual(result["targets"], [str(self.cwd / "new.json")])
        self.assertEqual(result["uncertain"], [])

    def test_directory_destination_includes_effective_basename(self):
        (self.cwd / "destination").mkdir()
        result = self.inspect("/bin/cp", "source.json", "destination")
        self.assertEqual(result["targets"], [str(self.cwd / "destination/source.json")])
        for program in ("mv", "rsync", "install"):
            result = self.inspect(program, "source.json", "destination")
            self.assertIn(str(self.cwd / "destination/source.json"), result["targets"])
            self.assertNotIn(str(self.cwd / "destination"), result["targets"])

    def test_target_directory_option_and_separator(self):
        result = self.inspect("cp", "-t", "destination", "--", "-source.json")
        self.assertIn(str(self.cwd / "destination/-source.json"), result["targets"])
        self.assertEqual(result["uncertain"], [])

    def test_move_and_removal_preserve_destructive_sources(self):
        move = self.inspect("mv", "before", "after")
        self.assertEqual(move["destructive_sources"], [str(self.cwd / "before")])
        for program in ("rm", "unlink", "shred"):
            result = self.inspect(program, "--", "a")
            self.assertTrue(result["writes"])
            self.assertEqual(result["destructive_sources"], [str(self.cwd / "a")])

    def test_directory_removal_is_not_lost_to_file_check(self):
        (self.cwd / "records").mkdir()
        result = self.inspect("rm", "-rf", "records")
        self.assertEqual(result["targets"], [str(self.cwd / "records")])

    def test_dd_output_and_truncate_reference_are_distinct(self):
        result = self.inspect("dd", "if=input", "of=out", "bs=4")
        self.assertEqual(result["targets"], [str(self.cwd / "out")])
        self.assertFalse(self.inspect("dd", "if=input")["writes"])
        result = self.inspect("truncate", "--reference", "input", "out")
        self.assertEqual(result["targets"], [str(self.cwd / "out")])

    def test_install_mode_and_directory_mode(self):
        result = self.inspect("install", "-m", "0400", "source", "dest")
        self.assertEqual(result["targets"], [str(self.cwd / "dest")])
        result = self.inspect("install", "-d", "first", "second")
        self.assertEqual(result["targets"], [str(self.cwd / "first"), str(self.cwd / "second")])

    def test_tee_multiple_targets(self):
        result = self.inspect("tee", "-a", "one", "two")
        self.assertEqual(result["targets"], [str(self.cwd / "one"), str(self.cwd / "two")])

    def test_unknown_option_stays_uncertain(self):
        result = self.inspect("cp", "--unrecognized", "input", "out")
        self.assertTrue(result["writes"])
        self.assertTrue(result["uncertain"])
        result = self.inspect("tee", "--unrecognized")
        self.assertTrue(result["writes"])
        self.assertTrue(result["uncertain"])

    def test_rsync_file_and_remote_or_directory_limits(self):
        result = self.inspect("rsync", "-av", "source", "destination")
        self.assertEqual(result["targets"], [str(self.cwd / "destination")])
        self.assertEqual(result["uncertain"], [])
        self.assertTrue(self.inspect("rsync", "host:source", "destination")["uncertain"])
        self.assertTrue(self.inspect("rsync", "source/", "destination")["uncertain"])

    def test_short_redirections_are_shell_only(self):
        for text in (">a", "echo hi>a", "echo hi>>a"):
            result = self.inspect("bash", "-c", text)
            self.assertTrue(result["writes"], text)
            self.assertEqual(result["targets"], [str(self.cwd / "a")], text)
            self.assertEqual(result["uncertain"], [], text)
        self.assertFalse(self.inspect("echo", ">a")["writes"])

    def test_quoted_redirect_is_data_but_quoted_path_is_target(self):
        result = self.inspect("sh", "-c", "echo '>' 'a'")
        self.assertFalse(result["writes"])
        result = self.inspect("sh", "-c", "echo hi > 'Application Support/out'")
        self.assertEqual(result["targets"], [str(self.cwd / "Application Support/out")])

    def test_shell_input_redirect_is_not_a_write_target(self):
        result = self.inspect("sh", "-c", "cat < input > output")
        self.assertEqual(result["targets"], [str(self.cwd / "output")])

    def test_complex_shell_is_explicitly_uncertain(self):
        for script in ("echo hi && rm a", "echo $(rm a)", "rm *.json", "echo hi 2>a", "echo hi; rm a", "echo x >a >$(where)", "rm 'rec'ords", "rm rec'ords'"):
            result = self.inspect("bash", "-c", script)
            self.assertTrue(result["writes"], script)
            self.assertTrue(result["uncertain"], script)
        self.assertTrue(self.inspect("bash", "-lc", "rm a")["uncertain"])

    def test_shell_reserved_prefixes_cannot_hide_mutations(self):
        for shell, script in (
            ("sh", "! rm protected"),
            ("bash", "time rm protected"),
            ("bash", "time -p rm protected"),
            ("zsh", "noglob rm protected"),
            ("bash", "coproc rm protected"),
            ("sh", "if rm protected; then echo done; fi"),
            ("sh", "f() { rm protected; }"),
            ("bash", "function f { rm protected; }"),
        ):
            result = self.inspect(shell, "-c", script)
            self.assertTrue(result["writes"], script)
            self.assertTrue(result["uncertain"], script)
        self.assertFalse(self.inspect("sh", "-c", "echo '!' time")["writes"])
        self.assertTrue(self.inspect("sh", "-c", "'time' rm protected")["uncertain"])

    def test_symlink_entry_and_referent_both_remain_visible(self):
        try:
            (self.cwd / "link").symlink_to("actual")
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable on this platform")
        result = self.inspect("unlink", "link")
        self.assertEqual(result["targets"], [str(self.cwd / "link"), str(self.cwd / "actual")])

    def test_parent_segments_normalized(self):
        result = self.inspect("rm", "nested/../target")
        self.assertEqual(result["targets"], [str(self.cwd / "target")])

    def test_symlink_parent_segments_cover_observed_filesystem_destination(self):
        (self.cwd / "actual/nested").mkdir(parents=True)
        try:
            (self.cwd / "link").symlink_to("actual/nested", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable on this platform")
        before = self.inspect("rm", "link/../target")
        # Ask the filesystem, rather than Path.resolve or an OS-name branch,
        # where symlink/../name writes land on this platform.
        marker = b"observed symlink parent destination\n"
        with (self.cwd / "link/../target").open("xb") as output:
            output.write(marker)
        lexical = self.cwd / "target"
        observed = [path for path in (lexical, self.cwd / "actual/target") if path.is_file()]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].read_bytes(), marker)
        expected = list(dict.fromkeys((str(lexical), str(observed[0]))))
        for state, result in (("absent", before), ("present", self.inspect("rm", "link/../target"))):
            with self.subTest(destination=state):
                self.assertTrue(result["writes"])
                self.assertEqual(result["targets"], expected)
                self.assertEqual(result["destructive_sources"], expected)
                self.assertEqual(result["uncertain"], [])

    def test_direct_tilde_is_literal_and_quoted_shell_tilde_stays_literal(self):
        self.assertEqual(self.inspect("rm", "~/target")["targets"], [str(self.cwd / "~/target")])
        self.assertEqual(self.inspect("sh", "-c", "rm '~/target'")["targets"], [str(self.cwd / "~/target")])
        self.assertEqual(self.inspect("sh", "-c", "rm ~/target")["targets"], [str(Path.home() / "target")])

    def test_inspection_bound_does_not_silently_allow(self):
        result = self.inspect("echo", "x" * 65537)
        self.assertTrue(result["writes"])
        self.assertTrue(result["uncertain"])


if __name__ == "__main__":
    unittest.main()
