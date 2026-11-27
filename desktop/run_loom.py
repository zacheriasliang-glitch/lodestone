#!/usr/bin/env python3
"""Launch one installed preview workspace; no model call occurs at startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verify-startup", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    from install import recover
    recover(root)
    from platform_support import file_lock
    lifecycle = file_lock(root / ".lifecycle.lock", shared=True)
    try:
        lifecycle.__enter__()
    except BlockingIOError:
        raise SystemExit("Lodestone setup or recovery is running. Open the preview after it finishes.")
    try:
        return run(root, args)
    finally:
        lifecycle.__exit__(None, None, None)


def run(root, args):
    from install import active_install
    active = active_install(root)
    release = Path(active["release"])
    source = release / "gui"
    sys.path[:0] = [str(release), str(source), str(release / "runtime/src")]
    os.chdir(source)
    for key in list(os.environ):
        if key.startswith("BASILISK_") or key.endswith("_API_KEY"):
            os.environ.pop(key, None)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    from bootstrap_learning import reconcile
    reconciliation = reconcile(root, release / "runtime", Path(active["worker_python"]))
    from basilisk import global_vars, consts
    from lodestone_machinery.loom_runtime import LoomBridge
    global_vars.user_data_path = root / "Loom/data"
    global_vars.loom_bridge = LoomBridge(root / "Loom/runtime.json")
    global_vars.args = argparse.Namespace(language="en", log_level="INFO",
        no_env_account=True, minimize=False, bskc_file=None)
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    consts.APP_NAME = "Lodestone Preview"
    consts.IPC_NAME = "lodestone_preview_" + identity
    consts.TMP_DIR = str(Path(tempfile.gettempdir()) / ("lodestone-preview-" + identity))
    consts.FILE_LOCK_PATH = str(Path(consts.TMP_DIR) / "app.lock")
    consts.FOCUS_FILE = str(Path(consts.TMP_DIR) / "focus_file")
    consts.OPEN_BSKC_FILE = str(Path(consts.TMP_DIR) / "open_bskc_file")
    from basilisk.singleton_instance import SingletonInstance
    singleton = SingletonInstance()
    if sys.platform == "win32":
        # Bind the native mutex to this workspace and login session, just like
        # the IPC and temporary paths. A separate preview must not focus it.
        singleton.mutex_name = "Local\\" + consts.IPC_NAME
    if not singleton.acquire():
        from basilisk.send_signal import send_focus_signal
        send_focus_signal()
        return
    try:
        from basilisk.main_app import MainApp
        if args.verify_startup:
            import locale
            import wx

            class VerifiedMainApp(MainApp):
                def OnInit(self):
                    # Record the native locale candidate without changing the
                    # selected locale or suppressing initialization diagnostics.
                    info = wx.Locale.FindLanguageInfo(global_vars.args.language)
                    print(json.dumps({"startup_locale": {
                        "requested": global_vars.args.language,
                        "system": locale.setlocale(locale.LC_ALL),
                        "wx_language": info.Language if info else None,
                        "wx_canonical": info.CanonicalName if info else None,
                        "available": wx.Locale.IsAvailable(info.Language) if info else False,
                    }}), file=sys.stderr, flush=True)
                    return super().OnInit()

            app = VerifiedMainApp()
            def verify():
                report = {"schema": "lodestone-desktop-startup/v1", "shown": app.frame.IsShown(),
                          "title": app.frame.GetTitle(), "models": global_vars.loom_bridge.models(),
                          "release_id": active["release_id"], "reconciliation": reconciliation,
                          "model_calls": 0}
                (root / "startup-verification.json").write_text(json.dumps(report, indent=2) + "\n")
                app.frame.Close()
            wx.CallLater(1500, verify)
        else:
            app = MainApp()
        app.MainLoop()
    except BaseException:
        (root / "startup-error.txt").write_text(traceback.format_exc())
        raise
    finally:
        global_vars.loom_bridge.cancel_all()
        singleton.release()


if __name__ == "__main__":
    main()
