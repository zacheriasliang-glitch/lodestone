"""Run with the managed GUI Python; no wx.App or provider/model is started."""
import argparse
import builtins
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()
GUI = PAYLOAD / "gui"
sys.path.insert(0, str(GUI))
builtins._ = lambda text: text
_configuration = tempfile.TemporaryDirectory(prefix="lodestone-gui-test-config-")
from basilisk import global_vars
global_vars.user_data_path = Path(_configuration.name)
global_vars.args = argparse.Namespace(no_env_account=True)

from basilisk.conversation import Conversation, Message, MessageBlock, MessageRoleEnum, SystemMessage
from basilisk.conversation.database import ConversationDatabase
from basilisk.provider_ai_model import AIModelInfo
from basilisk.services.conversation_service import ConversationService


def setUpModule():
    for name in ("basilisk.conversation", "basilisk.conversation.database",
                 "basilisk.services.conversation_service"):
        if not Path(sys.modules[name].__file__).resolve().is_relative_to(GUI):
            raise AssertionError("GUI test imported outside selected payload: " + name)


def block(text="Unsent inspection checklist", purpose="Keep the inspection date undecided"):
    return MessageBlock(request=Message(role=MessageRoleEnum.USER, content=text),
                        model=AIModelInfo(provider_id="loom", model_id="loom-qwen3-base"),
                        max_tokens=512, loom_purpose=purpose)


class ConversationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="lodestone-gui-persistence-")
        self.root = Path(self.workspace.name)
        self.path = self.root / "conversations.db"
        # The existing Alembic environment uses the application data root.
        global_vars.user_data_path = self.root
        self.db = ConversationDatabase(self.path)
        self.addCleanup(self.workspace.cleanup)
        self.addCleanup(self.db.close)

    def test_first_draft_survives_without_model_or_completed_answer(self):
        service = ConversationService(lambda: self.db)
        self.assertTrue(service.should_auto_save_draft())
        conversation = Conversation(title="Kiln project")
        service.save_draft_to_db(conversation, block(), SystemMessage(content="Keep sources explicit"))
        self.assertIsNotNone(service.db_conv_id)
        restored = self.db.load_conversation(service.db_conv_id)
        self.assertEqual(restored.title, "Kiln project")
        self.assertEqual(restored.messages[0].request.content, "Unsent inspection checklist")
        self.assertIsNone(restored.messages[0].response)
        self.assertEqual(restored.messages[0].loom_purpose, "Keep the inspection date undecided")
        self.assertEqual(restored.systems[restored.messages[0].system_index].content, "Keep sources explicit")
        self.assertEqual(conversation.messages, [])

    def test_blank_tab_does_not_create_a_conversation(self):
        service = ConversationService(lambda: self.db)
        service.save_draft_to_db(Conversation(), None, None)
        self.assertIsNone(service.db_conv_id)
        self.assertEqual(self.db.get_conversation_count(), 0)

    def test_replaced_draft_keeps_only_its_current_purpose(self):
        service = ConversationService(lambda: self.db)
        conversation = Conversation(title="Changing conditions")
        service.save_draft_to_db(conversation, block(purpose="First purpose"), None)
        service.save_draft_to_db(conversation, block(purpose="Changed purpose"), None)
        restored = self.db.load_conversation(service.db_conv_id)
        self.assertEqual(len(restored.messages), 1)
        self.assertEqual(restored.messages[0].loom_purpose, "Changed purpose")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT purpose FROM loom_block_purposes").fetchall(), [("Changed purpose",)])
            self.assertEqual(db.execute("SELECT version_num FROM alembic_version").fetchone(), ("001",))

    def _purpose_only_presenter(self, *, model, purpose):
        from basilisk.presenters.conversation_presenter import ConversationPresenter
        service = ConversationService(lambda: self.db)
        conversation = Conversation(title="Purpose-only project")
        service.save_draft_to_db(conversation, block(text="", purpose=purpose), None)
        view = SimpleNamespace(
            current_account=SimpleNamespace(provider=SimpleNamespace(id="loom")),
            current_model=model,
            prompt_panel=SimpleNamespace(prompt_text="", attachment_files=[]),
            loom_purpose=SimpleNamespace(GetValue=lambda: purpose),
            system_prompt_txt=SimpleNamespace(GetValue=lambda: ""),
        )
        return ConversationPresenter(view, service, conversation, self.root)

    def test_unavailable_route_does_not_erase_purpose_only_draft(self):
        presenter = self._purpose_only_presenter(model=None, purpose="Preserve the inspection purpose")
        presenter.on_draft_timer()
        restored = self.db.load_conversation(presenter.service.db_conv_id)
        self.assertEqual(len(restored.messages), 1)
        self.assertEqual(restored.messages[0].request.content, "")
        self.assertEqual(restored.messages[0].loom_purpose, "Preserve the inspection purpose")

    def test_available_route_can_remove_cleared_purpose_only_draft(self):
        from basilisk.services.loom_records import DEFAULT_LOOM_PURPOSE
        presenter = self._purpose_only_presenter(
            model=SimpleNamespace(id="loom-qwen3-base"), purpose="Previous inspection purpose")
        presenter.view.loom_purpose.GetValue = lambda: DEFAULT_LOOM_PURPOSE
        presenter.on_draft_timer()
        self.assertEqual(self.db.load_conversation(presenter.service.db_conv_id).messages, [])

    def test_export_import_preserves_changed_purpose(self):
        from upath import UPath
        conversation = Conversation(title="Export project", messages=[block()])
        destination = self.root / "project.bskc"
        conversation.save(str(destination))
        restored = Conversation.open(str(destination), UPath("memory://gui-persistence-export"))
        self.assertEqual(restored.messages[0].loom_purpose, conversation.messages[0].loom_purpose)
        self.assertEqual(restored.messages[0].model, conversation.messages[0].model)
        self.assertEqual(restored.messages[0].max_tokens, 512)

    def test_previous_preview_can_read_database_with_optional_metadata(self):
        old_source = os.environ.get("LODESTONE_OLD_GUI_SOURCE")
        if not old_source:
            self.skipTest("Set LODESTONE_OLD_GUI_SOURCE to test the previous preview reader")
        conversation = Conversation(title="Rollback-readable project", messages=[block()])
        conv_id = self.db.save_conversation(conversation)
        self.db.close()
        program = """
import argparse,builtins,json,pathlib,sys
builtins._=lambda text:text
sys.path.insert(0,sys.argv[1])
from basilisk import global_vars
global_vars.user_data_path=pathlib.Path(sys.argv[2]).parent
global_vars.args=argparse.Namespace(no_env_account=True)
from basilisk.conversation.database import ConversationDatabase
db=ConversationDatabase(pathlib.Path(sys.argv[2]))
print(json.dumps(db.load_conversation(int(sys.argv[3])).model_dump(mode='json')))
db.close()
"""
        result = subprocess.run([sys.executable, "-B", "-c", program, old_source, str(self.path), str(conv_id)],
                                text=True, capture_output=True, check=True)
        restored = json.loads(result.stdout)
        self.assertEqual(restored["title"], conversation.title)
        self.assertEqual(restored["messages"][0]["request"]["content"], block().request.content)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT purpose FROM loom_block_purposes").fetchone(), (block().loom_purpose,))


if __name__ == "__main__":
    unittest.main()
