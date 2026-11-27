"""Native locale fallback preserves gettext and avoids invalid wx locales."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()


class LocalizationTests(unittest.TestCase):
    def test_available_requested_and_unavailable_native_locales(self):
        code = r'''
import builtins, gc, io, logging, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]) / 'gui'))
import wx
from babel import Locale
from basilisk import global_vars
global_vars.user_data_path = Path(sys.argv[2])
from basilisk import localization
app = wx.App(False)
class Capture(wx.Log):
    def __init__(self):
        super().__init__()
        self.messages = []
    def DoLogText(self, message):
        self.messages.append(message)
capture = Capture()
previous = wx.Log.SetActiveTarget(capture)
output = io.StringIO()
handler = logging.StreamHandler(output)
localization.log.addHandler(handler)
localization.log.setLevel(logging.DEBUG)
real_locale = wx.Locale

# A supported region is passed intact and keeps the existing native choice.
with patch.object(localization.wx, 'Locale') as factory:
    factory.FindLanguageInfo.return_value = SimpleNamespace(Language=91)
    factory.IsAvailable.return_value = True
    selected = localization.get_wx_locale(Locale.parse('en_US'))
    factory.FindLanguageInfo.assert_called_once_with('en_US')
    factory.IsAvailable.assert_called_once_with(91)
    factory.assert_called_once_with(91)
    assert selected is factory.return_value

# Control only availability; instantiate real native default/C locale objects.
# gettext still installs the translation requested by the application.
for use_default in (True, False):
    with patch.object(localization.wx, 'Locale', wraps=real_locale) as factory, \
         patch.object(localization.gettext, 'translation') as translation:
        factory.IsAvailable.side_effect = lambda lang: use_default and lang == wx.LANGUAGE_DEFAULT
        # Represent a valid system default with the universally supported C
        # locale, so the controlled branch is independent of this host's region.
        factory.side_effect = lambda *args: real_locale('C', 'C', 'C') if args == (wx.LANGUAGE_DEFAULT,) else real_locale(*args)
        selected = localization.init_translation('en')
        assert selected.IsOk()
        translation.assert_called_once_with(domain=localization.APP_NAME,
            localedir=localization.LOCALE_DIR, languages=['en'], fallback=True)
        translation.return_value.install.assert_called_once_with()
        if use_default:
            factory.assert_called_once_with(wx.LANGUAGE_DEFAULT)
        else:
            factory.assert_called_once_with('C', 'C', 'C')
        del selected
    gc.collect()

# UI availability may disagree with the C runtime on some systems. A failed
# system-default activation must fall back again without returning an invalid
# wx locale or abandoning the requested gettext translation.
with patch.object(localization.wx, 'Locale', wraps=real_locale) as factory:
    factory.IsAvailable.side_effect = lambda lang: lang == wx.LANGUAGE_DEFAULT
    factory.side_effect = lambda *args: SimpleNamespace(IsOk=lambda: False) if args == (wx.LANGUAGE_DEFAULT,) else real_locale(*args)
    selected = localization.get_wx_locale(Locale.parse('en'))
    assert selected.IsOk()
    del selected
gc.collect()

# Exercise the real native availability and default locale, including the
# minimal Linux runner where the generic English locale is not installed.
selected = localization.init_translation('en')
assert selected.IsOk()
del selected
gc.collect()
wx.Log.FlushActive()
assert not any('Cannot set locale' in line for line in capture.messages), capture.messages
assert 'Requested application translation remains en.' in output.getvalue()
assert 'using C (' in output.getvalue()
assert 'using system default (' in output.getvalue()
localization.log.removeHandler(handler)
wx.Log.SetActiveTarget(previous)
print('native locale fallback passed')
'''
        with tempfile.TemporaryDirectory(prefix="lodestone-locale-") as directory:
            result = subprocess.run([sys.executable, "-B", "-c", code, str(PAYLOAD), directory],
                                    capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("native locale fallback passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
