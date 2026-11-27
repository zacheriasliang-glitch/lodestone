"""Native timer-owner shutdown regression; isolated config, no model calls."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()


class FrameLifecycleTests(unittest.TestCase):
    def test_frame_timer_stops_before_destruction_while_modal_loop_continues(self):
        code = r'''
import argparse, builtins, faulthandler, json, sys, traceback
from pathlib import Path
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]) / 'gui')]
import wx, yaml
from install import defaults
root = Path(sys.argv[2])
for name, value in defaults(root).items():
    (root / name).write_text(yaml.safe_dump(value), encoding='utf-8')
from basilisk import global_vars
builtins._ = lambda text: text
global_vars.user_data_path = root
global_vars.args = argparse.Namespace(no_env_account=True)
from basilisk import config
class Bridge:
    def models(self):
        return [{'id':'loom-qwen3-base', 'name':'Scripted lifecycle fixture',
                 'context_window':8192, 'max_output_tokens':512}]
global_vars.loom_bridge = Bridge()
from basilisk.conversation.database import ConversationDatabase
from basilisk.sound_manager import initialize_sound_manager
from basilisk.views.main_frame import MainFrame
app = wx.App(False)
# Keep native diagnostics visible in the child log; the test creates its own
# independent modal loop to reproduce GTK's log-dialog lifetime explicitly.
wx.Log.SetActiveTarget(wx.LogStderr())
app.conv_db = ConversationDatabase(root / 'conversations.db')
initialize_sound_manager()
frame = MainFrame(None, title='Timer lifecycle fixture', conf=config.conf())
frame.Show()
faulthandler.dump_traceback_later(10)
timer = frame.timer
ticks, errors, closed_ticks = [], [], []
def tick(event):
    ticks.append(1)
    event.Skip()
frame.Bind(wx.EVT_TIMER, tick, timer)
dialog = wx.Dialog(None, title='Keep dispatching after the frame closes')
def close_frame():
    try:
        assert ticks, 'Timer never delivered its ordinary pre-close callbacks'
        frame.Close()
        assert not timer.IsRunning(), 'Timer still running after frame close'
        assert timer.GetOwner() is not frame, 'Timer retains the native frame owner'
        closed_ticks.append(len(ticks))
    except BaseException:
        errors.append(traceback.format_exc())
        # Stop a regressed implementation before a known native use-after-free.
        timer.Stop()
        timer.SetOwner(None)
        dialog.EndModal(wx.ID_CANCEL)
def finish():
    try:
        assert not timer.IsRunning(), 'Timer restarted after frame close'
        assert closed_ticks == [len(ticks)], 'Native callbacks continued after frame close'
    except BaseException:
        errors.append(traceback.format_exc())
    dialog.EndModal(wx.ID_OK)
close_call = wx.CallLater(300, close_frame)
finish_call = wx.CallLater(800, finish)
try:
    result = dialog.ShowModal()
    assert not errors, '\n'.join(errors)
    assert result == wx.ID_OK
    print(json.dumps({'ordinary_timer_events':len(ticks), 'modal_survived_frame':True, 'model_calls':0}))
finally:
    faulthandler.cancel_dump_traceback_later()
    close_call.Stop()
    finish_call.Stop()
    timer.Stop()
    dialog.Destroy()
    app.ProcessPendingEvents()
    app.conv_db.close()
'''
        with tempfile.TemporaryDirectory(prefix="lodestone-frame-lifecycle-") as directory:
            try:
                result = subprocess.run([sys.executable, "-B", "-c", code, str(PAYLOAD), directory],
                                        capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                self.fail("Native GUI fixture timed out: " + str(exc.stdout) + str(exc.stderr))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"modal_survived_frame": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
