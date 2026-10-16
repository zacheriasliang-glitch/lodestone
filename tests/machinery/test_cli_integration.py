"""Both merged interfaces must remain reachable through the public CLI."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery import Engine, RecordStore
from lodestone_machinery.__main__ import main


class CombinedInterfaceTests(unittest.TestCase):
    def test_origami_dispatch_and_record_catalog_coexist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.md").write_text("Ceramic source evidence.\n")
            workspace = root / "work"
            engine = Engine(workspace)
            job = engine.run(engine.submit(root, ["source.md"], "ceramic"))
            self.assertEqual(job["state"], "COMPLETED")
            self.assertIsInstance(engine.records, RecordStore)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["catalog-records", "--workspace", str(workspace)]), 0)
            catalog = json.loads(output.getvalue())
            handles = {entry["handle"] for entry in catalog["entries"]}
            self.assertIn(job["result"]["organ_records"]["orchestrator"], handles)
            self.assertEqual(catalog["verdict"], "OK")
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as help_exit:
                main(["origami", "--help"])
            self.assertEqual(help_exit.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
