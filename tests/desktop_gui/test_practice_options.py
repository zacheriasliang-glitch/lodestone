"""Per-encounter UI, persistence and transport fixtures; no model/provider calls.

Real modal wx events are enabled with LODESTONE_GUI_EVENT_TESTS=1.
"""
import argparse
import builtins
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()
GUI = PAYLOAD / "gui"
sys.path.insert(0, str(GUI))
builtins._ = lambda text: text
_configuration = tempfile.TemporaryDirectory(prefix="lodestone-practice-test-config-")
from basilisk import global_vars
global_vars.user_data_path = Path(_configuration.name)
global_vars.args = argparse.Namespace(no_env_account=True)

import wx
from upath import UPath
from basilisk.conversation import Conversation, Message, MessageBlock, MessageRoleEnum
from basilisk.conversation.database import ConversationDatabase
from basilisk.presenters.conversation_presenter import ConversationPresenter
from basilisk.provider_ai_model import AIModelInfo
from basilisk.provider_engine.loom_engine import LoomEngine
from basilisk.services.conversation_service import ConversationService
from basilisk.services.loom_practice import bind_options, bridge_options, summary
from basilisk.views.loom_practice_dialog import LoomPracticeDialog

HANDLE = "bezoar:" + "a" * 64
CASE = '{"observations":[{"id":"probe-one","value":3}]}'
TEXT = "Inspect the following supplied case without executing anything.\n" + CASE
PURPOSE = "Record a conditional response to this supplied case"


def setUpModule():
    for name in ("basilisk.services.loom_practice", "basilisk.provider_engine.loom_engine",
                 "basilisk.views.loom_practice_dialog"):
        if not Path(sys.modules[name].__file__).resolve().is_relative_to(GUI):
            raise AssertionError("Practice test imported outside selected payload: " + name)


def options():
    return bind_options(TEXT, PURPOSE, seed_handles=[HANDLE], history_window=2,
                        decision_case_json=CASE, decision_strategy="evidence_first")


def block(value=None, text=TEXT):
    return MessageBlock(request=Message(role=MessageRoleEnum.USER, content=text),
                        model=AIModelInfo(provider_id="loom", model_id="loom-qwen3-base"),
                        max_tokens=512, loom_purpose=PURPOSE, loom_practice_options=value)


class FixtureBridge:
    """Captures transport only. Never opens native memory or starts a provider."""
    def __init__(self):
        self.calls = []

    def practice_candidates(self):
        return {"schema": "loom-practice-candidates/v1", "configured_seed_handles": [HANDLE],
                "candidates": [{"handle": HANDLE, "id": "fixture-source-observations", "state": "SEED",
                    "conditions": ["Use only when the source observations are supplied"],
                    "frame_limits": ["Fixture; does not demonstrate semantic learning"],
                    "correction": "Keep the source observation attached to the decision.",
                    "ready": True, "error": None}]}

    def models(self):
        return [{"id": "loom-qwen3-base", "name": "Scripted GUI fixture (no model)",
                 "context_window": 8192, "max_output_tokens": 512}]

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "COMPLETED", "response": "Scripted transport fixture", "outcome_handle": None}


def engine():
    result = LoomEngine(None)
    result.__dict__["client"] = FixtureBridge()
    return result


class HandoffSourcePathTests(unittest.TestCase):
    def test_picker_passes_windows_alias_to_runtime_without_resolving_it(self):
        from basilisk.views.conversation_tab import ConversationTab
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "sources"
            source.mkdir()
            (source / "note.txt").write_text("Authored source fixture", encoding="utf-8")
            alias = root / "selected alias"
            if os.name == "nt":
                completed = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(source)],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            else:
                alias.symlink_to(source, target_is_directory=True)
            try:
                selected = alias / "note.txt"
                calls = []
                def preview(receipt, **kwargs):
                    calls.append(kwargs)
                    raise ValueError("Stop after the real picker-to-runtime boundary")
                tab = SimpleNamespace(completion_handler=SimpleNamespace(is_running=lambda: False),
                    conversation=object(), current_account=SimpleNamespace(provider=SimpleNamespace(id="loom")),
                    current_engine=SimpleNamespace(preview_origami=preview))
                with patch("basilisk.views.conversation_tab.latest_receipt", return_value={"outcome_handle": HANDLE}), \
                        patch("basilisk.views.conversation_tab.wx.FileDialog") as chooser, \
                        patch("basilisk.views.conversation_tab.wx.MessageBox") as message:
                    chooser.return_value.__enter__.return_value.ShowModal.return_value = wx.ID_OK
                    chooser.return_value.__enter__.return_value.GetPaths.return_value = [str(selected)]
                    with self.assertLogs("basilisk.views.conversation_tab", level="ERROR"):
                        ConversationTab.on_loom_origami(tab, None)
                    message.assert_called_once()
                self.assertEqual(calls, [{"root": str(alias if os.name == "nt" else source),
                                         "paths": ["note.txt"]}])
            finally:
                alias.rmdir() if os.name == "nt" else alias.unlink()


class PracticeOptionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lodestone-practice-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        global_vars.user_data_path = self.root
        self.db = ConversationDatabase(self.root / "conversations.db")
        self.addCleanup(self.db.close)

    def presenter(self, value=None, text=TEXT):
        panel = SimpleNamespace(prompt_text=text, attachment_files=[],
            ensure_model_compatibility=lambda model: model, resize_all_attachments=lambda: None,
            check_attachments_valid=lambda: True, refresh_attachments_list=lambda: None)
        panel.clear = lambda **kwargs: setattr(panel, "prompt_text", "")
        value_control = lambda value: SimpleNamespace(GetValue=lambda: value)
        view = SimpleNamespace(prompt_panel=panel, current_model=SimpleNamespace(id="loom-qwen3-base"),
            current_account=SimpleNamespace(provider=SimpleNamespace(id="loom", engine_cls=LoomEngine)),
            current_engine=engine(), loom_practice_options=deepcopy(value), loom_purpose=value_control(PURPOSE),
            temperature_spinner=value_control(0.1), top_p_spinner=value_control(1),
            max_tokens_spin_ctrl=value_control(512), stream_mode=value_control(False),
            system_prompt_txt=value_control(""), submit_btn=SimpleNamespace(IsEnabled=lambda: True),
            stop_draft_timer=lambda: None, refresh_loom_practice_summary=lambda: None,
            errors=[], show_error=lambda message, title: view.errors.append((message, title)))
        presenter = ConversationPresenter(view, ConversationService(lambda: self.db), Conversation(), self.root)
        calls = []
        presenter.completion_handler.start_completion = lambda **kwargs: calls.append(kwargs)
        return presenter, calls

    def test_ordinary_route_omits_new_bridge_kwargs(self):
        presenter, calls = self.presenter()
        presenter.on_submit()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("loom_practice_options", calls[0])
        kwargs = dict(calls[0]); target = kwargs.pop("engine"); kwargs.pop("stream")
        target.completion(**kwargs)
        request = target.client.calls[0]
        for key in ("seed_handles", "history_window", "decision_case_json", "decision_strategy"):
            self.assertNotIn(key, request)
        self.assertIsNone(bind_options(TEXT, PURPOSE))
        self.assertEqual(request["encounter"], TEXT)

    def test_options_reach_bridge_exactly_then_are_consumed_and_error_recoverable(self):
        presenter, calls = self.presenter(options())
        presenter.on_submit()
        self.assertEqual(calls[0]["new_block"].loom_practice_options, options())
        self.assertIsNone(presenter.view.loom_practice_options)
        self.assertEqual(presenter.view.prompt_panel.prompt_text, "")
        kwargs = dict(calls[0]); target = kwargs.pop("engine"); kwargs.pop("stream")
        target.completion(**kwargs)
        request = target.client.calls[0]
        self.assertEqual({key: request[key] for key in bridge_options(options(), TEXT, PURPOSE)},
                         bridge_options(options(), TEXT, PURPOSE))
        self.assertEqual(request["encounter"], TEXT)
        presenter._restore_prompt_content()
        self.assertEqual(presenter.view.loom_practice_options, options())
        self.assertEqual(presenter.view.prompt_panel.prompt_text, TEXT)

    def test_stale_message_or_purpose_cannot_dispatch_and_draft_stays_saved(self):
        for change in ("message", "purpose"):
            presenter, calls = self.presenter(options())
            if change == "message":
                presenter.view.prompt_panel.prompt_text += "\nA different condition."
            else:
                presenter.view.loom_purpose.GetValue = lambda: "A different purpose"
            original = presenter.view.prompt_panel.prompt_text
            presenter.on_draft_timer()
            presenter.on_submit()
            self.assertEqual(calls, [])
            self.assertEqual(presenter.view.prompt_panel.prompt_text, original)
            self.assertIn("Review practice options", presenter.view.errors[0])
            restored = self.db.load_conversation(presenter.service.db_conv_id).messages[0]
            self.assertEqual(restored.loom_practice_options, options())
            self.assertIn("need review", summary(restored.loom_practice_options, original,
                                                presenter.view.loom_purpose.GetValue()))
            target = engine()
            with self.assertRaises(ValueError):
                target.completion(restored, Conversation(), None,
                    loom_purpose=presenter.view.loom_purpose.GetValue(), loom_practice_options=options())
            self.assertEqual(target.client.calls, [])

    def test_empty_selection_bounds_and_nonunique_source_refuse(self):
        for values in ({"seed_handles": []}, {"seed_handles": [HANDLE, HANDLE]},
                       {"history_window": True}, {"history_window": 0}, {"history_window": 33},
                       {"decision_strategy": "evidence_first"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                bind_options(TEXT, PURPOSE, **values)
        for encounter in ("Missing case", TEXT + CASE):
            with self.assertRaises(ValueError):
                bind_options(encounter, PURPOSE, decision_case_json=CASE)

    def test_saved_selection_rechecked_before_dispatch_without_dialog(self):
        for kind in ("missing", "unavailable", "superseded"):
            with self.subTest(kind=kind):
                target = engine()
                catalogue = target.practice_candidates()
                if kind == "missing":
                    catalogue["candidates"] = []
                elif kind == "unavailable":
                    catalogue["candidates"][0]["ready"] = False
                else:
                    catalogue["candidates"][0]["resolved_handle"] = "bezoar:" + "b" * 64
                target.client.practice_candidates = lambda: catalogue
                with self.assertRaisesRegex(ValueError, "Review Practice options"):
                    target.completion(block(options()), Conversation(), None,
                        loom_purpose=PURPOSE, loom_practice_options=options())
                self.assertEqual(target.client.calls, [])

    def test_draft_database_and_project_roundtrip_preserve_exact_options(self):
        presenter, _ = self.presenter(options())
        presenter.on_draft_timer()
        conv_id = presenter.service.db_conv_id
        restored = self.db.load_conversation(conv_id)
        self.assertEqual(restored.messages[0].loom_practice_options, options())
        destination = self.root / "practice-project.bskc"
        self.assertTrue(presenter.save_conversation(str(destination)))
        reopened = Conversation.open(str(destination), UPath("memory://practice-options-roundtrip"))
        self.assertEqual(reopened.messages[0].loom_practice_options, options())
        self.assertEqual(reopened.messages[0].request.content, TEXT)
        presenter.view.loom_practice_options = None
        presenter.on_draft_timer()
        with closing(sqlite3.connect(self.root / "conversations.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM loom_block_practice_options").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT version_num FROM alembic_version").fetchone(), ("001",))

    def test_previous_reader_ignores_optional_options_without_changing_current_records(self):
        readers = (
            ("preview1", os.environ.get("LODESTONE_OLD_GUI_SOURCE"), False),
            ("preview3", os.environ.get("LODESTONE_PREVIOUS_GUI_SOURCE"), True),
        )
        if any(not source for _, source, _ in readers):
            self.skipTest("Set LODESTONE_OLD_GUI_SOURCE and LODESTONE_PREVIOUS_GUI_SOURCE for both pinned readers")
        saved_block = block(options())
        saved_block.response = Message(role=MessageRoleEnum.ASSISTANT,
                                       content="Scripted prior-reader response; no model call")
        conversation = Conversation(title="Prior-reader project", messages=[saved_block])
        conv_id = self.db.save_conversation(conversation)
        project = self.root / "old-readable.bskc"
        conversation.save(str(project))
        self.db.close()
        project_before = project.read_bytes()
        def stored_options():
            with closing(sqlite3.connect(self.root / "conversations.db")) as db:
                return db.execute("SELECT * FROM loom_block_practice_options ORDER BY block_id").fetchall()
        options_before = stored_options()
        program = '''
import argparse,builtins,json,pathlib,sys
builtins._=lambda text:text
sys.path.insert(0,sys.argv[1])
from basilisk import global_vars
global_vars.user_data_path=pathlib.Path(sys.argv[2]).parent
global_vars.args=argparse.Namespace(no_env_account=True)
from basilisk.conversation import Conversation,MessageBlock
import basilisk.conversation.conversation_model as conversation_model
assert pathlib.Path(conversation_model.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())
from basilisk.conversation.database import ConversationDatabase
from upath import UPath
purpose_supported="loom_purpose" in MessageBlock.model_fields
assert purpose_supported == (sys.argv[5] == "true"), "Pinned reader feature set changed"
assert "loom_practice_options" not in MessageBlock.model_fields
db=ConversationDatabase(pathlib.Path(sys.argv[2]))
saved=db.load_conversation(int(sys.argv[3]))
project=Conversation.open(sys.argv[4],UPath("memory://previous-practice-reader"))
def core(block):
    return {"request":block.request.content,"request_role":block.request.role.value,
            "response":block.response.content,"response_role":block.response.role.value,
            "purpose":block.loom_purpose if purpose_supported else None}
assert core(saved.messages[0]) == core(project.messages[0])
print(json.dumps({"purpose_supported":purpose_supported,**core(saved.messages[0])}))
db.close()
'''
        for label, source, purpose_supported in readers:
            with self.subTest(reader=label):
                result = subprocess.run([sys.executable, "-B", "-c", program, source,
                    str(self.root / "conversations.db"), str(conv_id), str(project),
                    str(purpose_supported).lower()], text=True, capture_output=True)
                evidence = os.environ.get("LODESTONE_GUI_EVIDENCE_DIR")
                if evidence:
                    destination = Path(evidence) / "previous-reader-compatibility"
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / (label + ".json")).write_text(json.dumps({
                        "reader": label, "source": source, "purpose_supported": purpose_supported,
                        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                        "scope": "Read existing current DB and project only; no older-reader re-save or model call",
                    }, indent=2) + "\n")
                self.assertEqual(result.returncode, 0,
                    f"{label} reader failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
                self.assertEqual(json.loads(result.stdout), {"request": TEXT, "request_role": "user",
                    "response": saved_block.response.content, "response_role": "assistant",
                    "purpose_supported": purpose_supported, "purpose": PURPOSE if purpose_supported else None})
                self.assertEqual(project.read_bytes(), project_before)
                self.assertEqual(stored_options(), options_before)
        reopened_db = ConversationDatabase(self.root / "conversations.db")
        self.addCleanup(reopened_db.close)
        restored = reopened_db.load_conversation(conv_id).messages[0]
        self.assertEqual(restored.loom_practice_options, options())
        self.assertEqual(restored.loom_purpose, PURPOSE)
        self.assertEqual(restored.response.content, saved_block.response.content)


@unittest.skipUnless(os.environ.get("LODESTONE_GUI_EVENT_TESTS") == "1", "Enable actual desktop wx events")
class PracticeOptionsEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        evidence = os.environ.get("LODESTONE_GUI_EVIDENCE_DIR")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix="wx-" + hashlib.sha256(self.id().encode()).hexdigest()[:12] + "-", dir=evidence))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="lodestone-practice-wx-")
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        global_vars.user_data_path = self.root
        (self.root / "test-identity.json").write_text(json.dumps({"test_id": self.id(),
            "scope": "Actual wx handlers with authored catalogue fixture; model calls 0"}, indent=2))
        self.frame = wx.Frame(None, title="Isolated Practice options verification")
        self.frame.Show()
        self.dialogs = []
        self.addCleanup(self.cleanup_windows)

    def cleanup_windows(self):
        for dialog in reversed(self.dialogs):
            dialog.Destroy()
        self.frame.Destroy()
        self.app.Yield()

    def dialog(self, **kwargs):
        result = LoomPracticeDialog(self.frame, engine=engine(), encounter=TEXT, purpose=PURPOSE, **kwargs)
        self.dialogs.append(result)
        return result

    def wait_catalogue(self, dialog):
        deadline = time.monotonic() + 5
        while not dialog._catalogue_ready and time.monotonic() < deadline:
            self.app.Yield()
            time.sleep(0.01)
        self.assertTrue(dialog._catalogue_ready, dialog.loading.GetLabel())

    def modal(self, dialog, configure):
        errors = []
        def operate():
            if not dialog._catalogue_ready:
                wx.CallLater(10, operate)
                return
            try:
                configure(dialog)
                button = dialog.FindWindow(wx.ID_OK)
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                wx.PostEvent(button, event)
            except BaseException as error:
                errors.append(error)
                dialog.EndModal(wx.ID_CANCEL)
        timeout = wx.CallLater(5000, lambda: dialog.EndModal(wx.ID_CANCEL) if dialog.IsModal() else None)
        wx.CallLater(40, operate)
        status = dialog.ShowModal()
        timeout.Stop()
        if errors:
            raise errors[0]
        self.assertEqual(status, wx.ID_OK, dialog.message.GetLabel())
        return dialog.result

    def test_real_controls_save_and_reopen_exact_selection_and_clear(self):
        def select(dialog):
            dialog.configured.SetValue(False)
            dialog.lessons.Check(0, True)
            dialog.window_enabled.SetValue(True)
            event = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.window_enabled.GetId())
            event.SetEventObject(dialog.window_enabled)
            dialog.window_enabled.ProcessWindowEvent(event)
            self.assertTrue(dialog.history_window.IsEnabled())
            dialog.history_window.SetValue(2)
            dialog.case_json.SetValue(CASE)
            dialog.strategy.SetSelection(1)
        selected = self.modal(self.dialog(), select)
        self.assertEqual(selected, options())
        project = self.root / "practice-selection.bskc"
        Conversation(title="Actual Practice control fixture", messages=[block(selected)]).save(str(project))
        reopened = Conversation.open(str(project), UPath("memory://wx-practice-reopen"))
        restored = self.dialog(options=reopened.messages[0].loom_practice_options)
        self.wait_catalogue(restored)
        self.assertEqual(restored.case_json.GetValue(), CASE)
        self.assertEqual(restored.strategy.GetSelection(), 1)
        self.assertEqual(restored.lessons.GetCheckedItems(), (0,))
        self.assertEqual(self.modal(restored, lambda dialog: None), selected)
        clearing = self.dialog(options=selected)
        def clear(dialog):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, dialog.defaults.GetId())
            event.SetEventObject(dialog.defaults)
            dialog.defaults.ProcessWindowEvent(event)
        self.assertIsNone(self.modal(clearing, clear))
        (self.root / "result.json").write_text(json.dumps({"options": selected,
            "project_roundtrip": True, "clear_omits_options": True, "model_calls": 0}, indent=2))

    def test_dialog_fits_small_client_area_with_action_buttons_visible(self):
        with patch("basilisk.views.loom_practice_dialog._display_client_area", return_value=wx.Rect(0, 24, 900, 620)):
            dialog = self.dialog()
        def inspect(dialog):
            self.assertLessEqual(dialog.GetSize().height, 588)
            self.assertTrue(dialog.GetWindowStyle() & wx.RESIZE_BORDER)
            client = dialog.GetClientRect()
            for identifier in (wx.ID_OK, wx.ID_CANCEL):
                button = dialog.FindWindow(identifier)
                position = dialog.ScreenToClient(button.ClientToScreen((0, 0)))
                self.assertGreaterEqual(position.y, 0)
                self.assertLessEqual(position.y + button.GetSize().height, client.height)
        self.assertIsNone(self.modal(dialog, inspect))

    def test_ordinary_tab_handlers_restore_draft_and_submit_scripted_bridge(self):
        from basilisk import config
        from basilisk.views.conversation_tab import ConversationTab
        from basilisk import sound_manager
        account = config.Account(name="Isolated Loom fixture", provider_id="loom")
        accounts = config.AccountManager(accounts=[account], default_account_info=account.id)
        bridge = FixtureBridge()
        database = ConversationDatabase(self.root / "conversations.db")
        self.addCleanup(database.close)
        self.frame.CreateStatusBar()
        tabs = []
        def cleanup_tabs():
            for tab in reversed(tabs):
                tab.stop_draft_timer()
                tab._is_destroying = True
                tab.Destroy()
        self.addCleanup(cleanup_tabs)
        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                self.app.Yield()
                time.sleep(0.01)
            self.assertTrue(predicate())
        def button(control):
            self.assertTrue(control.IsEnabled())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
            event.SetEventObject(control)
            control.ProcessWindowEvent(event)
        with patch.object(global_vars, "loom_bridge", bridge), patch.object(config, "accounts", return_value=accounts), \
             patch.object(ConversationTab, "_conv_db", database), patch.object(sound_manager, "sound_manager",
                SimpleNamespace(play_sound=lambda *args, **kwargs: None, stop_sound=lambda: None)):
            tab = ConversationTab(self.frame, title="Actual ordinary tab fixture")
            tabs.append(tab)
            wait_for(lambda: bool(tab._displayed_models))
            # A fresh tab expects the user to select a model in its actual list.
            tab.model_list.SetItemState(0, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
            wait_for(lambda: tab.current_model is not None)
            tab.prompt_panel.prompt_text = TEXT
            tab.loom_purpose.SetValue(PURPOSE)
            errors = []
            def choose():
                dialog = next((window for window in wx.GetTopLevelWindows()
                               if isinstance(window, LoomPracticeDialog) and window.IsModal()), None)
                if dialog is None:
                    errors.append("Practice dialog did not open")
                    return
                if not dialog._catalogue_ready:
                    wx.CallLater(10, choose)
                    return
                try:
                    dialog.configured.SetValue(False)
                    dialog.lessons.Check(0, True)
                    dialog.window_enabled.SetValue(True)
                    dialog.history_window.SetValue(2)
                    dialog.case_json.SetValue(CASE)
                    dialog.strategy.SetSelection(1)
                    button(dialog.FindWindow(wx.ID_OK))
                except BaseException as exc:
                    errors.append(str(exc))
                    dialog.EndModal(wx.ID_CANCEL)
            def cancel_stuck():
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, LoomPracticeDialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
            timeout = wx.CallLater(5000, cancel_stuck)
            wx.CallLater(50, choose)
            button(tab.loom_practice_btn)
            timeout.Stop()
            self.assertEqual(errors, [])
            self.assertEqual(tab.loom_practice_options, options())
            project = self.root / "ordinary-tab-draft.bskc"
            self.assertTrue(tab.save_conversation(str(project)))
            restored = ConversationTab.open_conversation(self.frame, str(project), "Restored")
            tabs.append(restored)
            wait_for(lambda: restored.current_model is not None)
            self.assertEqual(restored.prompt_panel.prompt_text, TEXT)
            self.assertEqual(restored.loom_purpose.GetValue(), PURPOSE)
            self.assertEqual(restored.loom_practice_options, options())
            errors = []
            restored.show_error = lambda message, title: errors.append((message, title))
            restored.prompt_panel.prompt_text = "A different current encounter"
            button(restored.submit_btn)
            self.assertEqual(bridge.calls, [])
            self.assertIn("Review practice options", errors[0])
            restored.prompt_panel.prompt_text = TEXT
            button(restored.submit_btn)
            wait_for(lambda: len(restored.conversation.messages) == 1 and not restored.completion_handler.is_running())
            answer = restored.conversation.messages[0]
            self.assertEqual(answer.response.content, "Scripted transport fixture")
            self.assertEqual(answer.loom_practice_options, options())
            self.assertIsNone(restored.loom_practice_options)
            self.assertEqual(len(bridge.calls), 1)
            self.assertEqual(bridge.calls[0]["decision_strategy"], "evidence_first")
            self.assertEqual(bridge.calls[0]["seed_handles"], [HANDLE])
            self.assertEqual(bridge.calls[0]["encounter"], TEXT)
            completed = self.root / "ordinary-tab-completed.bskc"
            self.assertTrue(restored.save_conversation(str(completed)))
            again = ConversationTab.open_conversation(self.frame, str(completed), "Completed")
            tabs.append(again)
            wait_for(lambda: again.current_model is not None)
            self.assertEqual(again.conversation.messages[0].loom_practice_options, options())
            self.assertEqual(again.conversation.messages[0].response.content, answer.response.content)
            self.assertEqual(len(bridge.calls), 1)
            (self.root / "ordinary-tab-result.json").write_text(json.dumps({"draft_saved_and_reopened": True,
                "completed_saved_and_reopened": True, "options": options(), "raw_response": answer.response.content,
                "scripted_bridge_callbacks": 1, "model_calls": 0, "new_case_dispatch_blocked": True}, indent=2))

    def test_superseded_saved_selection_requires_explicit_current_replacement(self):
        old = "bezoar:" + "b" * 64
        target = engine()
        catalogue = target.practice_candidates()
        catalogue["configured_seed_handles"] = [old]
        catalogue["resolved_seed_handles"] = [HANDLE]
        prior = deepcopy(catalogue["candidates"][0])
        prior.update(handle=old, id="Old fixture lesson", ready=False,
                     error="A newer retained lesson supersedes this candidate.", superseded_by=HANDLE,
                     resolved_handle=HANDLE)
        catalogue["candidates"].insert(0, prior)
        target.client.practice_candidates = lambda: deepcopy(catalogue)
        default = LoomPracticeDialog(self.frame, engine=target, encounter=TEXT, purpose=PURPOSE)
        self.dialogs.append(default)
        self.wait_catalogue(default)
        self.assertEqual(default.lessons.GetCheckedItems(), (1,))
        self.assertIn("Choose its current replacement: fixture-source-observations", default.lesson_details.GetValue())
        self.assertNotIn('"conditions":', default.lesson_details.GetValue())
        saved = bind_options(TEXT, PURPOSE, seed_handles=[old])
        restored = LoomPracticeDialog(self.frame, engine=target, encounter=TEXT, purpose=PURPOSE, options=saved)
        self.dialogs.append(restored)
        errors = []
        def first_attempt(dialog):
            self.assertEqual(dialog.lessons.GetCheckedItems(), (0,))
            def after_rejection():
                try:
                    self.assertTrue(dialog.IsModal())
                    self.assertIn("unavailable", dialog.message.GetLabel())
                    dialog.lessons.Check(0, False)
                    dialog.lessons.Check(1, True)
                    button = dialog.FindWindow(wx.ID_OK)
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                    event.SetEventObject(button)
                    wx.PostEvent(button, event)
                except BaseException as exc:
                    errors.append(exc)
                    dialog.EndModal(wx.ID_CANCEL)
            wx.CallLater(120, after_rejection)
        result = self.modal(restored, first_attempt)
        self.assertEqual(errors, [])
        self.assertEqual(result["seed_handles"], [HANDLE])
        self.assertEqual(target.client.calls, [])

    def test_slow_catalogue_stays_responsive_and_cancellation_discards_late_result(self):
        release = threading.Event()
        entered = threading.Event()
        target = engine()
        catalogue = target.practice_candidates()
        def slow_catalogue():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Fixture catalogue release timed out")
            return catalogue
        target.client.practice_candidates = slow_catalogue
        start = time.monotonic()
        dialog = LoomPracticeDialog(self.frame, engine=target, encounter=TEXT, purpose=PURPOSE, options=options())
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(entered.wait(1))
        self.assertIn("Loading", dialog.loading.GetLabel())
        self.assertFalse(dialog.FindWindow(wx.ID_OK).IsEnabled())
        self.assertFalse(dialog.lessons.IsEnabled())
        callbacks, ticks = [], []
        dialog._catalogue_loaded = lambda *args: callbacks.append(args)
        def tick_and_cancel():
            ticks.append(time.monotonic())
            button = dialog.FindWindow(wx.ID_CANCEL)
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            wx.PostEvent(button, event)
        timeout = wx.CallLater(1500, lambda: dialog.EndModal(wx.ID_CANCEL) if dialog.IsModal() else None)
        wx.CallLater(30, tick_and_cancel)
        status = dialog.ShowModal()
        timeout.Stop()
        self.assertEqual(status, wx.ID_CANCEL)
        self.assertEqual(len(ticks), 1)
        self.assertTrue(dialog._catalogue_cancel.is_set())
        dialog.Destroy()
        self.app.Yield()
        release.set()
        dialog._catalogue_thread.join(timeout=2)
        self.assertFalse(dialog._catalogue_thread.is_alive())
        self.app.Yield()
        self.assertEqual(callbacks, [])
        self.assertEqual(target.client.calls, [])
        (self.root / "async-cancel-result.json").write_text(json.dumps({"event_loop_tick_while_loading": True,
            "save_disabled_until_loaded": True, "late_callback_discarded": True, "provider_callbacks": 0,
            "model_calls": 0}, indent=2))

    def test_async_load_error_keeps_save_disabled_and_original_options_unchanged(self):
        target = engine()
        target.client.practice_candidates = lambda: {"schema": "unrecognized"}
        dialog = LoomPracticeDialog(self.frame, engine=target, encounter=TEXT, purpose=PURPOSE, options=options())
        self.dialogs.append(dialog)
        deadline = time.monotonic() + 2
        while "could not" not in dialog.loading.GetLabel() and time.monotonic() < deadline:
            self.app.Yield()
            time.sleep(0.01)
        self.assertIn("could not be loaded", dialog.loading.GetLabel())
        self.assertFalse(dialog.FindWindow(wx.ID_OK).IsEnabled())
        self.assertFalse(dialog.lessons.IsEnabled())
        self.assertTrue(dialog.FindWindow(wx.ID_CANCEL).IsEnabled())
        self.assertEqual(dialog._saved_options, options())
        self.assertEqual((dialog.encounter, dialog.purpose), (TEXT, PURPOSE))
        self.assertEqual(target.client.calls, [])


if __name__ == "__main__":
    unittest.main()
