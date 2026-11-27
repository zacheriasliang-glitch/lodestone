"""Native application logging and shutdown; isolated configuration, no models."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()


class WxLoggingTests(unittest.TestCase):
    def test_native_warnings_errors_and_pending_thread_record_survive_quit(self):
        code = r'''
import argparse, faulthandler, gc, json, logging, sys, threading
faulthandler.dump_traceback_later(20)
from pathlib import Path
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]) / 'gui')]
import wx, yaml
from install import defaults
root = Path(sys.argv[2])
for name, value in defaults(root).items():
    (root / name).write_text(yaml.safe_dump(value), encoding='utf-8')
from basilisk import global_vars
global_vars.user_data_path = root
global_vars.args = argparse.Namespace(log_level='CRITICAL', language='en', no_env_account=True)
from basilisk import main_app
from basilisk.logger import set_log_level
translate = main_app.init_translation
def translate_with_native_diagnostics(language):
    wx.LogWarning('native localization warning fixture — 雪')
    wx.LogError('native localization error fixture')
    return translate(language)
main_app.init_translation = translate_with_native_diagnostics
main_app.initialize_sound_manager = lambda: None
class FixtureApp(main_app.MainApp):
    def OnInit(self):
        # This occurs before MainApp.OnInit, like native startup diagnostics.
        assert wx.Log.GetActiveTarget() is self.wx_log_target, repr(wx.Log.GetActiveTarget())
        wx.LogWarning('native early initialization warning fixture')
        return super().OnInit()
    def init_conversation_db(self):
        self.conv_db = None
    def init_main_frame(self):
        self.frame = wx.Frame(None, title='Native logging shutdown fixture')
        self.SetTopWindow(self.frame)
        self.frame.Show()
    def init_ipc(self):
        self.ipc = None
    def init_system_cert_store(self):
        pass
    def OnExit(self):
        wx.LogError('native shutdown error fixture')
        result = super().OnExit()
        self.exited = True
        return result
app = FixtureApp(False)
assert not app.server and not app.auto_update
app.exited = False
# Both Python/native wrapper lifetime and delivery after changing ordinary
# Python verbosity are part of the regression, not just a logging mock.
del app.wx_log_target
gc.collect()
set_log_level('ERROR')
set_log_level('CRITICAL')
def close():
    wx.LogWarning('native post-gc warning fixture')
    worker = threading.Thread(target=lambda: wx.LogError('native worker pending error fixture'))
    worker.start()
    worker.join()
    app.frame.Close()
close_call = wx.CallLater(100, close)
app.MainLoop()
faulthandler.cancel_dump_traceback_later()
assert app.exited, 'Native application OnExit did not finish'
for handler in logging.getLogger().handlers:
    handler.flush()
print(json.dumps({'normal_quit':True, 'model_calls':0}))
'''
        with tempfile.TemporaryDirectory(prefix="lodestone-wx-logging-") as directory:
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code, str(PAYLOAD), directory],
                    capture_output=True, text=True, timeout=45,
                )
            except subprocess.TimeoutExpired as exc:
                log_path = Path(directory) / "basilisk.log"
                self.fail("Native wx logging blocked shutdown: " + str(exc.stdout) + str(exc.stderr)
                          + (log_path.read_text() if log_path.exists() else " (no log file)"))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            log_text = (Path(directory) / "basilisk.log").read_text(encoding="utf-8")
            for message in (
                "WARNING - native early initialization warning fixture",
                "WARNING - native localization warning fixture — 雪",
                "ERROR - native localization error fixture",
                "WARNING - native post-gc warning fixture",
                "ERROR - native worker pending error fixture",
                "ERROR - native shutdown error fixture",
            ):
                self.assertIn(message, log_text)
            self.assertIn('"normal_quit": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
