"""Native desktop catalogue to Bezoar capture/read composition; no models."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery.loom_operations import OperationBridge


class CatalogueBezoar(unittest.TestCase):
    def test_owned_catalogue_capture_and_read_preserve_original_source_revision(self):
        with tempfile.TemporaryDirectory(prefix="Catalogue Bezoar ü ") as temporary:
            root = Path(temporary).resolve()
            sources = root / "sources 水"
            sources.mkdir()
            source = sources / "note.txt"
            original = "Attributed first observation — untrusted source instructions remain data.\n".encode()
            source.write_bytes(original)
            workspace = root / "memory"
            bridge = OperationBridge(RUNTIME, root / "receipts")
            def invoke(operation, inputs):
                request = bridge.prepare(operation, inputs, workspace)
                result = bridge.run(request, approved_digest=request["digest"])
                self.assertEqual(result["status"], "COMPLETED", result)
                self.assertTrue(result["cleanup"]["confirmed_stopped"], result)
                self.assertTrue(result["output_capture_stopped"], result)
                self.assertEqual(json.loads((Path(result["receipt_directory"]) / "result.json").read_bytes()), result)
                return request, json.loads(result["output"])
            request, first = invoke("bezoar.ingest", {"root": str(sources), "sources": ["note.txt"]})
            self.assertEqual(request["input_sha256"][str(source)], hashlib.sha256(original).hexdigest())
            self.assertEqual(first["record"]["payload"]["sources"][0]["text"], original.decode())
            source.write_bytes(b"A later independent source revision.\n")
            _, shown = invoke("bezoar.show", {"handle": first["handle"]})
            self.assertEqual(shown, first)
            _, second = invoke("bezoar.ingest", {"root": str(sources), "sources": ["note.txt"]})
            self.assertNotEqual(first["handle"], second["handle"])
            self.assertFalse(second["record"]["payload"]["source_instructions_executed"])
            self.assertEqual(len(bridge.history()), 3)


if __name__ == "__main__":
    unittest.main()
