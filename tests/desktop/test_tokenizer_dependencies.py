"""The local Ollama reader verifies tokens without a tensor/training install."""
import hashlib
import importlib.metadata
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import loom_adapter_local as local
from lodestone_machinery import loom_runtime_worker as worker


class TokenizerDependencies(unittest.TestCase):
    def tokenizer_version(self, name):
        if name not in local.TOKENIZER_VERSIONS:
            raise importlib.metadata.PackageNotFoundError(name)
        return local.TOKENIZER_VERSIONS[name]

    def test_tokenizer_pins_do_not_require_tensor_packages(self):
        with patch.object(local.importlib.metadata, "version", side_effect=self.tokenizer_version) as lookup:
            self.assertEqual(local._tokenizer_versions(), local.TOKENIZER_VERSIONS)
        self.assertEqual({c.args[0] for c in lookup.call_args_list},
                         {"transformers", "safetensors", "tokenizers"})

    def test_full_model_route_still_requires_tensor_pins(self):
        with patch.object(local.importlib.metadata, "version", side_effect=self.tokenizer_version):
            with self.assertRaises(importlib.metadata.PackageNotFoundError):
                local._versions()

    def test_wrong_tokenizer_version_is_rejected(self):
        with patch.object(local.importlib.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(ValueError, "registered local package versions"):
                local._tokenizer_versions()

    def tokenizer_fixture(self, root):
        pins = {}
        for name in worker.TOKENIZER_FILES:
            raw = (name + "\n").encode()
            (root / name).write_bytes(raw)
            pins[name] = (len(raw), hashlib.sha256(raw).hexdigest())
        return pins

    def test_worker_loads_verified_tokenizer_without_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pins = self.tokenizer_fixture(root)
            tokenizer = types.SimpleNamespace(eos_token_id=151645, pad_token_id=151643)
            fake_class = types.SimpleNamespace(from_pretrained=lambda *a, **kw: tokenizer)
            fake_module = types.ModuleType("transformers")
            fake_module.Qwen2TokenizerFast = fake_class
            with patch.object(local, "ASSETS", pins), \
                    patch.object(local.importlib.metadata, "version", side_effect=self.tokenizer_version), \
                    patch.dict(sys.modules, {"transformers": fake_module}):
                returned, identity = worker.verified_tokenizer(root)
            self.assertIs(returned, tokenizer)
            self.assertEqual(set(identity), set(worker.TOKENIZER_FILES))
            self.assertFalse((root / "model.safetensors").exists())

    def test_changed_tokenizer_asset_still_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pins = self.tokenizer_fixture(root)
            target = root / "tokenizer.json"
            original = target.read_bytes()
            target.write_bytes(b"x" * len(original))
            with patch.object(local, "ASSETS", pins), \
                    patch.object(local.importlib.metadata, "version", side_effect=self.tokenizer_version):
                with self.assertRaisesRegex(ValueError, "registered SHA-256"):
                    worker.verified_tokenizer(root)


if __name__ == "__main__":
    unittest.main()
