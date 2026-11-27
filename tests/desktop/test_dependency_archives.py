"""Cross-platform archive custody; no downloads or external executable calls."""
from pathlib import Path
import hashlib
import io
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from desktop import dependencies as d


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / 'cache'
        self.cache.mkdir()

    def test_zip_windows_executable_is_verified_and_rechecked(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('tool.exe', b'verified executable fixture')
        raw = buffer.getvalue()
        artifact = {'filename': 'tool.zip', 'url': 'https://example.org/tool.zip',
                    'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'executable': 'tool.exe'}
        (self.cache / artifact['sha256']).write_bytes(raw)
        destination = self.root / 'zip-tree'
        # This fixture verifies custody; native Windows CI executes the pinned tools.
        with patch.object(d.os, 'access', return_value=True), patch.object(d.subprocess, 'run', side_effect=AssertionError('unexpected launch')):
            tool = d.unpack(artifact, self.cache, destination, offline=True)
            self.assertEqual(tool.read_bytes(), b'verified executable fixture')
            tool.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'differs'):
                d.unpack(artifact, self.cache, destination, offline=True)

    def test_zip_unsafe_names_aliases_links_and_expansion_are_refused(self):
        cases = [('../outside', 'safe'), ('C:/outside', 'safe'),
                 ('folder\\outside', 'safe'), ('folder\x00hidden', 'safe'),
                 ('Tool.exe', 'tool.exe')]
        path = self.root / 'unsafe.zip'
        for names in cases:
            with self.subTest(names=names):
                with zipfile.ZipFile(path, 'w') as z:
                    for name in names:
                        member = zipfile.ZipInfo('fixture')
                        # Bypass constructor normalization so Windows writes
                        # the same malformed stored name as every other host.
                        member.filename = name
                        z.writestr(member, b'fixture')
                with zipfile.ZipFile(path) as z:
                    self.assertEqual(tuple(m.orig_filename for m in z.infolist()), names)
                with self.assertRaises(ValueError):
                    d._archive_inventory(path)
        with zipfile.ZipFile(path, 'w') as z:
            member = zipfile.ZipInfo('alias')
            member.external_attr = 0o120777 << 16
            z.writestr(member, b'../outside')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            d._archive_inventory(path)
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('large', b'12345')
        with self.assertRaisesRegex(ValueError, 'extraction bound'):
            d._archive_inventory(path, maximum=4)


if __name__ == '__main__':
    unittest.main()
