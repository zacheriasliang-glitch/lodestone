import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery.__main__ import main
from lodestone_machinery import transformer
from lodestone_machinery.crystals import CrystalStore


class TrainingCLITests(unittest.TestCase):
    def test_train_full_to_one_store_and_prevent_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = transformer.initialize_model(["a", "b", "KEEP", "SHARE"], width=4, heads=2,
                layers=1, ff_width=8, max_tokens=4, seed=7)
            transformer.save_model(model, root / "model.json")
            dataset = json.dumps({"examples": [{"token_ids": [0, 1], "target": 2},
                                               {"token_ids": [1, 0], "target": 3}], "action_ids": [2, 3]}).encode()
            (root / "data.json").write_bytes(dataset)
            args = ["origami", "transformer", "train", "--model", str(root / "model.json"),
                    "--dataset", str(root / "data.json"), "--expected-dataset-sha256", hashlib.sha256(dataset).hexdigest(),
                    "--store", str(root / "crystal.sqlite"), "--run-id", "unit-full", "--authority-ref", "unit test",
                    "--steps", "5", "--mode", "full", "--max-seconds", "20"]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
            result = json.loads(out.getvalue())
            with CrystalStore(root / "crystal.sqlite") as store:
                raw = store.get(result["model_source_id"])
                report = json.loads(store.get(result["report_source_id"]))
                self.assertEqual(report["semantic_validation"], "NOT_ESTABLISHED")
                self.assertNotEqual(json.loads(raw)["sha256"], transformer.model_sha256(model))
                before = store.inspect()["sources"]
            (root / "recovered.json").write_bytes(raw)
            trained = transformer.load_model(root / "recovered.json")
            self.assertEqual(trained["provenance"]["training_status"], "UNVERIFIED")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(args), 2)
            self.assertIn("not replayed", err.getvalue())
            with CrystalStore(root / "crystal.sqlite") as store:
                self.assertEqual(store.inspect()["sources"], before)
            self.assertEqual(transformer.load_model(root / "model.json"), model)


if __name__ == "__main__":
    unittest.main()
