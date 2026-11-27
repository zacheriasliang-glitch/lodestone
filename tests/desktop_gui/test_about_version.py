"""About displays the built archive identity without Git or build packages."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import wx.adv

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()
sys.path.insert(0, str(PAYLOAD / "gui"))


class AboutVersionTests(unittest.TestCase):
    def load_about(self, path):
        spec = importlib.util.spec_from_file_location("fixture_about_version", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_about_display_uses_manifest_from_actual_built_archive(self):
        # Reuse the source-only build fixture to exercise the real ZIP builder;
        # no environment installation or inference occurs in this check.
        spec = importlib.util.spec_from_file_location("about_build_fixture", ROOT / "tests/desktop/test_build.py")
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        fixture = fixture_module.BuildTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        relative = "gui/basilisk/views/about_dialog.py"
        source = fixture.source / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes((PAYLOAD / relative).read_bytes())
        provenance_path = fixture.source / "SOURCE_PROVENANCE.json"
        provenance = json.loads(provenance_path.read_bytes())
        provenance["exported_files"][relative] = {"sha256":hashlib.sha256(source.read_bytes()).hexdigest()}
        provenance_path.write_text(json.dumps(provenance))
        result = fixture_module.build.build(fixture.source, fixture.base / "download")
        with zipfile.ZipFile(fixture.base / "download" / result["archive"]) as archive:
            archive.extractall(fixture.base / "extracted")
        payload = fixture.base / "extracted" / ("Lodestone-Desktop-" + result["version"])
        about = self.load_about(payload / relative)
        with patch("builtins._", lambda text:text, create=True), patch.object(wx.adv, "AboutBox") as display:
            about.display_about_dialog(None)
        self.assertEqual(display.call_count, 1)
        self.assertEqual(display.call_args.args[0].GetVersion(), result["version"])

    def test_source_checkout_has_explicit_unpackaged_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gui/basilisk/views/about_dialog.py"
            path.parent.mkdir(parents=True)
            path.write_bytes((PAYLOAD / "gui/basilisk/views/about_dialog.py").read_bytes())
            self.assertEqual(self.load_about(path).app_version, "Source checkout (unpackaged)")


if __name__ == "__main__":
    unittest.main()
