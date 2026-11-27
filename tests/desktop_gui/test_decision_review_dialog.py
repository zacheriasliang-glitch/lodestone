"""GUI review checks; real wx events opt in with LODESTONE_GUI_EVENT_TESTS=1.

Uses isolated authored native fixtures, never a model or an existing installation.
Set LODESTONE_GUI_EVIDENCE_DIR to preserve those fixtures and actual receipts.
"""
import argparse
import builtins
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()
GUI = PAYLOAD / "gui"
RUNTIME = PAYLOAD / "runtime"
sys.path.insert(0, str(GUI))
sys.path.insert(0, str(RUNTIME / "src"))
builtins._ = lambda text: text
_configuration = tempfile.TemporaryDirectory(prefix="lodestone-review-test-config-")
from basilisk import global_vars
global_vars.user_data_path = Path(_configuration.name)
global_vars.args = argparse.Namespace(no_env_account=True)

import wx
from basilisk.conversation import Conversation, Message, MessageBlock, MessageRoleEnum
from basilisk.provider_ai_model import AIModelInfo
from basilisk.provider_engine.loom_engine import LoomEngine
from basilisk.services.loom_records import latest_receipt, save_receipt
from basilisk.views.loom_correction_dialog import LoomCorrectionDialog
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery import loom_runtime

_spec = importlib.util.spec_from_file_location("gui_review_bootstrap", PAYLOAD / "bootstrap_learning.py")
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)


def setUpModule():
    for name, root in (("basilisk.provider_engine.loom_engine", GUI),
                       ("basilisk.views.loom_correction_dialog", GUI),
                       ("lodestone_machinery.loom_runtime", RUNTIME),
                       ("lodestone_machinery.loom_unified", RUNTIME)):
        if not Path(sys.modules[name].__file__).resolve().is_relative_to(root):
            raise AssertionError("Review test imported outside selected payload: " + name)


class PolicyInputTests(unittest.TestCase):
    def test_ambiguous_or_nonfinite_policy_json_is_not_silently_normalized(self):
        for text in ('{"rules":[],"rules":[]}', '{"value":NaN}', '{"value":Infinity}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                LoomCorrectionDialog._policy(text)
        self.assertIsNone(LoomCorrectionDialog._policy(" \n"))


class CountingBridge:
    def __init__(self, client):
        self.client = client
        self.context_calls = self.preview_calls = self.save_calls = 0

    def decision_review_context(self, *args, **kwargs):
        self.context_calls += 1
        return self.client.decision_review_context(*args, **kwargs)

    def preview_decision_review(self, *args, **kwargs):
        self.preview_calls += 1
        return self.client.preview_decision_review(*args, **kwargs)

    def review_decision(self, *args, **kwargs):
        self.save_calls += 1
        return self.client.review_decision(*args, **kwargs)

    def run(self, *args, **kwargs):
        raise AssertionError("A GUI review must never dispatch a provider")


@unittest.skipUnless(os.environ.get("LODESTONE_GUI_EVENT_TESTS") == "1",
                     "Set LODESTONE_GUI_EVENT_TESTS=1 in an available desktop session")
class DecisionReviewEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        evidence = os.environ.get("LODESTONE_GUI_EVIDENCE_DIR")
        if evidence:
            directory = Path(evidence).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            # Keep native record paths within the existing USTAR evidence
            # envelope's bounds; preserve the complete test ID as data below.
            prefix = "wx-" + hashlib.sha256(self.id().encode()).hexdigest()[:12] + "-"
            self.root = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="lodestone-native-gui-review-")
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name).resolve()
        self.dump("test-identity.json", {"test_id": self.id(), "scope": "Authored GUI fixture; no model."})
        global_vars.user_data_path = self.root / "gui-data"
        self.frame = wx.Frame(None, title="Isolated Lodestone review verification")
        self.frame.Show()
        self.dialogs = []

    def tearDown(self):
        for dialog in reversed(self.dialogs):
            dialog.Destroy()
        self.frame.Destroy()
        self.app.Yield()

    def dump(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def click(self, control):
        self.assertTrue(control.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        wx.PostEvent(control, event)
        self.app.Yield()

    def select_review(self, dialog, index):
        control = dialog.review_selection
        control.SetSelection(index)
        event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
        event.SetEventObject(control)
        wx.PostEvent(control, event)
        self.app.Yield()

    def open_dialog(self, receipt, engine=None, *, structured=False):
        dialog = LoomCorrectionDialog(self.frame, receipt, engine=engine)
        self.dialogs.append(dialog)
        dialog.Show()
        self.app.Yield()
        if structured:
            dialog.pages.SetSelection(1)
            self.app.Yield()
        return dialog

    def native_fixture(self, *, status="COMPLETED", wrong=True, automatic=False):
        initialized = bootstrap.initialize(self.root / "bootstrap", RUNTIME, Path(sys.executable))
        self.memory = LearningMemory(initialized["memory_workspace"])
        self.native = UnifiedLoom(self.memory, initialized["seed_handles"])
        case = {"case_id": "gui-authored-fixture", "target": "pump-A", "recorded_prior_action": "KEEP",
                "rules": ["A steady target calls for KEEP.", "An unstable target calls for STOP."],
                "observations": [
                    {"id": "A1", "order": 1, "target": "pump-A", "verified": True, "text": "Pump-A is steady."},
                    {"id": "B2", "order": 2, "target": "pump-B", "verified": True, "text": "Pump-B is unstable."}],
                "allowed_actions": ["KEEP", "STOP"]}
        self.case_json = json.dumps(case, ensure_ascii=False, indent=2)
        self.encounter = "Authored GUI fixture; no model evidence.\n" + self.case_json + "\n"
        self.policy = {"rules": [
            {"condition": "steady", "action": "KEEP", "source_quote": case["rules"][0]},
            {"condition": "unstable", "action": "STOP", "source_quote": case["rules"][1]}],
            "observed_conditions": [
                {"observation_id": row["id"], "condition": condition, "source_quote": row["text"]}
                for row, condition in zip(case["observations"], ("steady", "unstable"))]}
        self.raw = " \n" + json.dumps({"case_id": case["case_id"], "action": "STOP" if wrong else "KEEP",
            "transition": "CHANGE" if wrong else "HOLD", "evidence_ids": ["B2" if wrong else "A1"]}, indent=2) + "\n "
        self.scripted_calls = 0
        def fixture_provider(packet):
            self.scripted_calls += 1
            return {"status": status, "response": self.raw,
                    "observation": {"source": "Authored GUI test fixture; no model", "model_calls": 0}}
        from lodestone_machinery.decision_format import bind_decision_format
        contract = bind_decision_format(self.encounter, case_json=self.case_json)['source_contract'] if automatic else None
        self.returned = self.native.run(encounter=self.encounter, purpose="Inspect an explicitly structured saved case.",
            reader="Scripted GUI fixture only", context="", provider=fixture_provider, structural_contract=contract)
        self.original = self.memory.read_outcome(self.returned["outcome_handle"])
        block = MessageBlock(request=Message(role=MessageRoleEnum.USER, content=self.encounter),
            response=Message(role=MessageRoleEnum.ASSISTANT, content=self.raw),
            model=AIModelInfo(provider_id="loom", model_id="loom-qwen3-base"),
            loom_purpose="Inspect an explicitly structured saved case.")
        self.conversation = Conversation(title="Structured review fixture", messages=[block])
        self.receipt = save_receipt(block, self.returned, block.loom_purpose)
        # Real bridge forwarding; only profile lookup is replaced with this
        # task's explicit bootstrap config, since no model profile is needed.
        bridge = object.__new__(loom_runtime.LoomBridge)
        bridge.config_path = Path(initialized["active_config"])
        configuration = patch.object(loom_runtime, "_config", return_value={"learning_config": initialized["active_config"]})
        configuration.start()
        self.addCleanup(configuration.stop)
        self.counts = CountingBridge(bridge)
        self.engine = object.__new__(LoomEngine)
        self.engine.__dict__["client"] = self.counts
        self.dump("fixture.json", {"case_json": self.case_json, "policy": self.policy,
            "encounter": self.encounter, "raw_response": self.raw, "returned": self.returned,
            "scripted_provider_calls": self.scripted_calls, "model_calls": 0})

    def test_machine_finding_reopens_separately_and_never_changes_displayed_answer(self):
        self.native_fixture(automatic=True)
        dialog = self.open_dialog(self.receipt, self.engine, structured=True)
        finding = self.returned['decision_assessment']
        self.assertIn(finding['evidence_handle'], dialog.machine_findings.GetValue())
        self.assertIn('MACHINE_STRUCTURAL_FINDING', dialog.machine_findings.GetValue())
        self.assertIn('REJECTED', dialog.machine_findings.GetValue())
        self.assertEqual(dialog.original_response.GetValue(), self.raw)
        self.assertEqual(dialog.review_selection.GetCount(), 1)
        self.assertEqual(self.receipt['decision_assessment']['evidence_handle'], finding['evidence_handle'])
        self.assertEqual(self.counts.save_calls, 0)
        self.assertEqual(self.scripted_calls, 1)

    def fill(self, dialog, *, policy=True):
        dialog.decision_fields["case_json"].SetValue(self.case_json)
        dialog.decision_fields["policy_json"].SetValue(json.dumps(self.policy) if policy else "")
        dialog.decision_fields["author"].SetValue("GUI fixture curator")
        self.app.Yield()

    def record_hashes(self):
        return {str(path.relative_to(self.memory.workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.memory.workspace.glob("organs/*/records/*.json")}

    def test_ordinary_correction_stays_default_without_native_lookup(self):
        class NoNative:
            def decision_review_context(self, receipt):
                raise AssertionError("Ordinary correction must not read structured history")
        dialog = self.open_dialog({"purpose": "An open-ended ordinary question.", "outcome_handle": "unused"}, NoNative())
        self.assertEqual(dialog.pages.GetSelection(), 0)
        dialog.fields["correction"].SetValue("  Preserve the observed uncertainty. ")
        dialog.fields["conditions"].SetValue("When evidence is incomplete")
        self.assertEqual(dialog.values()["correction"], "Preserve the observed uncertainty.")
        self.assertTrue(dialog.FindWindowById(wx.ID_OK).IsEnabled())
        self.assertIsNone(dialog.review_context)
        self.assertIsNone(dialog.last_saved_review)

    def test_dialog_fits_usable_display_and_keeps_save_cancel_visible_when_resized(self):
        from basilisk.views import loom_correction_dialog
        actual = loom_correction_dialog._display_client_area(self.frame)
        available = wx.Rect(actual.x + 12, actual.y + 12,
                            min(900, actual.width - 24), min(620, actual.height - 24))
        observations = []
        with patch.object(loom_correction_dialog, "_display_client_area", return_value=available):
            dialog = self.open_dialog({"purpose": "Review without hiding the save controls.", "outcome_handle": "unused"})
        self.assertTrue(dialog.GetWindowStyleFlag() & wx.RESIZE_BORDER)
        self.assertTrue(available.Contains(dialog.GetScreenRect()))
        for page_index in (0, 1):
            dialog.pages.SetSelection(page_index)
            self.app.Yield()
            self.assertIsInstance(dialog.pages.GetPage(page_index), wx.ScrolledWindow)
            for resized in (False, True):
                if resized:
                    size = dialog.GetSize()
                    dialog.SetSize(size.width - 30, size.height - 35)
                    dialog.Layout()
                    self.app.Yield()
                buttons = {}
                for ident, name in ((wx.ID_OK, "save"), (wx.ID_CANCEL, "cancel")):
                    button = dialog.FindWindowById(ident)
                    self.assertTrue(button.IsShownOnScreen())
                    self.assertGreaterEqual(button.GetSize().width, button.GetBestSize().width)
                    self.assertTrue(dialog.GetScreenRect().Contains(button.GetScreenRect()))
                    self.assertTrue(available.Contains(button.GetScreenRect()))
                    buttons[name] = list(button.GetScreenRect())
                observations.append({"page": page_index, "resized": resized,
                                     "dialog_screen_rect": list(dialog.GetScreenRect()), "buttons": buttons})
        self.dump("wx-display-fit-result.json", {"actual_display_client_area": list(actual),
            "constrained_client_area": list(available), "observations": observations,
            "model_calls": 0, "scripted_provider_calls": 0,
            "scope": "Actual wx geometry/events with a deliberately constrained usable-area fixture."})

    def test_actual_save_and_project_reopen_restore_exact_native_review_without_rerun(self):
        from upath import UPath
        self.native_fixture()
        before = self.record_hashes()
        dialog = self.open_dialog(self.receipt, self.engine, structured=True)
        self.assertFalse(dialog.original_encounter.IsEditable())
        self.assertFalse(dialog.original_response.IsEditable())
        self.assertEqual(dialog.original_encounter.GetValue(), self.encounter)
        self.assertEqual(dialog.original_response.GetValue(), self.raw)
        self.fill(dialog)
        self.assertFalse(dialog.FindWindowById(wx.ID_OK).IsEnabled())
        self.click(dialog.preview_btn)
        self.assertEqual(dialog.decision_preview["result"]["assessment"]["status"], "REJECTED")
        self.assertEqual(before, self.record_hashes(), "Preview must not write native records")
        self.click(dialog.FindWindowById(wx.ID_OK))
        self.assertEqual(self.counts.save_calls, 1)
        self.assertIn("Learning review: UNRESOLVED", dialog.decision_summary.GetLabel())
        saved = deepcopy(dialog.saved_review)
        self.assertEqual(saved["contract"]["case_json"], self.case_json)
        self.assertEqual(self.memory.read_outcome(self.returned["outcome_handle"]), self.original)
        self.assertFalse(dialog.FindWindowById(wx.ID_OK).IsEnabled())
        path = self.root / "saved-project.bskc"
        self.conversation.save(str(path))
        restored = Conversation.open(str(path), UPath("memory://structured-review-test"))
        receipt = latest_receipt(restored)
        self.assertEqual(receipt, self.receipt)
        self.assertEqual(restored.messages[0].response.content, self.raw)
        dialog.Destroy()
        self.dialogs.remove(dialog)
        before_reopen = self.record_hashes()
        preview_count = self.counts.preview_calls
        reopened = self.open_dialog(receipt, self.engine, structured=True)
        self.select_review(reopened, 1)
        self.assertEqual(reopened.saved_review, saved)
        self.assertEqual(reopened.decision_fields["case_json"].GetValue(), self.case_json)
        self.assertEqual(self.counts.preview_calls, preview_count)
        self.assertEqual(self.counts.save_calls, 1)
        self.assertEqual(self.scripted_calls, 1)
        self.assertEqual(before_reopen, self.record_hashes())
        self.dump("wx-save-reopen-result.json", {"saved_review": saved,
            "reopened_review": reopened.saved_review, "original_outcome_unchanged": True,
            "native_records_unchanged_on_reopen": True, "preview_calls": self.counts.preview_calls,
            "save_calls": self.counts.save_calls, "scripted_provider_calls": self.scripted_calls,
            "model_calls": 0, "independent_learning_established": False})

    def test_changed_or_invalid_inputs_cannot_save_a_stale_preview(self):
        self.native_fixture(wrong=False)
        before = self.record_hashes()
        dialog = self.open_dialog(self.receipt, self.engine, structured=True)
        self.fill(dialog)
        self.click(dialog.preview_btn)
        self.assertEqual(dialog.decision_preview["result"]["assessment"]["status"], "PASS")
        dialog.decision_fields["case_json"].SetValue(self.case_json + " changed")
        self.app.Yield()
        self.assertIsNone(dialog.decision_preview)
        self.assertFalse(dialog.FindWindowById(wx.ID_OK).IsEnabled())
        self.click(dialog.preview_btn)
        self.assertIn("not previewed or saved", dialog.decision_summary.GetLabel())
        dialog.decision_fields["case_json"].SetValue(self.case_json)
        dialog.decision_fields["policy_json"].SetValue('{"rules":[],"rules":[]}')
        self.app.Yield()
        calls = self.counts.preview_calls
        self.click(dialog.preview_btn)
        self.assertEqual(self.counts.preview_calls, calls)
        self.assertIn("duplicate key", dialog.decision_summary.GetLabel())
        dialog.decision_fields["policy_json"].SetValue("")
        self.app.Yield()
        self.click(dialog.preview_btn)
        self.assertEqual(dialog.decision_preview["result"]["assessment"]["status"], "UNRESOLVED")
        self.assertEqual(self.counts.save_calls, 0)
        self.assertEqual(before, self.record_hashes())

    def test_interrupted_raw_answer_stays_interrupted_after_saved_check(self):
        self.native_fixture(status="INTERRUPTED", wrong=False)
        dialog = self.open_dialog(self.receipt, self.engine, structured=True)
        self.fill(dialog)
        self.click(dialog.preview_btn)
        self.assertEqual(dialog.decision_preview["result"]["assessment"]["status"], "UNRESOLVED")
        self.click(dialog.FindWindowById(wx.ID_OK))
        self.assertEqual(self.memory.read_outcome(self.returned["outcome_handle"]), self.original)
        self.assertEqual(dialog.saved_review["assessment"]["status"], "UNRESOLVED")
        self.assertEqual(dialog.saved_review["learning_verdict"], "UNRESOLVED")
        self.assertEqual(dialog.original_response.GetValue(), self.raw)
        self.assertIn("INTERRUPTED", dialog.native_status.GetLabel())

    def test_missing_native_history_is_explicit_and_ordinary_correction_remains_available(self):
        class MissingNative:
            def decision_review_context(self, receipt):
                raise ValueError("Native evidence is absent in this installation")
        dialog = self.open_dialog({"purpose": "Imported project", "outcome_handle": "missing"},
                                  MissingNative(), structured=True)
        self.assertIn("Native review unavailable", dialog.native_status.GetLabel())
        self.assertFalse(dialog.preview_btn.IsEnabled())
        self.assertFalse(dialog.FindWindowById(wx.ID_OK).IsEnabled())
        dialog.pages.SetSelection(0)
        self.app.Yield()
        self.assertTrue(dialog.FindWindowById(wx.ID_OK).IsEnabled())
        self.assertIsNone(dialog.last_saved_review)


if __name__ == "__main__":
    unittest.main()
