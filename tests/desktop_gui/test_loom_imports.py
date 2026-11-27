"""Prove the shipped Loom route loads in the exact pruned GUI environment."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = Path(os.environ.get("LODESTONE_DESKTOP_TEST_PAYLOAD", ROOT / "desktop")).resolve()


class LoomImportTests(unittest.TestCase):
    def test_shipped_imports_and_scripted_route_without_provider_sdks(self):
        code = r'''
import argparse, builtins, faulthandler, hashlib, importlib, importlib.abc, importlib.metadata, json, re, socket, sys
faulthandler.dump_traceback_later(20)
from pathlib import Path
payload, root = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(payload), str(payload / 'gui')]
profile = json.loads((payload / 'gui-dependencies.json').read_bytes())
lock = json.loads((payload / 'dependencies.lock.json').read_bytes())
normalize = lambda value: re.sub(r'[-_.]+', '-', value).lower()
actual = {normalize(d.metadata['Name']):d.version for d in importlib.metadata.distributions()}
expected = {w['name']:w['version'] for w in lock['environments']['gui']['wheels']}
assert actual == expected, {'actual':actual, 'expected':expected}
assert not set(profile['unregistered_provider_distributions']) & actual.keys()
class RefuseProviderImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'anthropic', 'openai', 'mistralai', 'google', 'ollama'}:
            raise AssertionError('Shipped GUI attempted an unregistered provider import: ' + fullname)
sys.meta_path.insert(0, RefuseProviderImports())
def refuse_network(*args, **kwargs):
    raise AssertionError('Import/scripted route fixture attempted a network connection')
socket.socket.connect = refuse_network
socket.create_connection = refuse_network
builtins._ = lambda text:text
import yaml
from install import defaults
for name, value in defaults(root).items():
    (root / name).write_text(yaml.safe_dump(value), encoding='utf-8')
from basilisk import global_vars
global_vars.user_data_path = root
global_vars.args = argparse.Namespace(no_env_account=True, log_level='INFO', language='en')
class ScriptedBridge:
    calls = []
    def models(self):
        return [{'id':'loom-qwen3-base', 'name':'Scripted import fixture',
                 'context_window':8192, 'max_output_tokens':512}]
    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {'status':'COMPLETED', 'response':'Scripted import fixture', 'outcome_handle':None}
global_vars.loom_bridge = ScriptedBridge()
import run_loom
from basilisk import main_app, provider
for folder in ('views', 'presenters', 'services', 'conversation', 'config', 'model_catalog'):
    for path in sorted((payload / 'gui/basilisk' / folder).rglob('*.py')):
        module = '.'.join(path.relative_to(payload / 'gui').with_suffix('').parts)
        if module.endswith('.__init__'):
            module = module[:-9]
        importlib.import_module(module)
assert [p.id for p in provider.providers] == ['loom']
from basilisk.views import about_dialog
manifest_path = payload / 'MANIFEST.json'
assert about_dialog.app_version == (json.loads(manifest_path.read_bytes())['version']
    if manifest_path.is_file() else 'Source checkout (unpackaged)')
engine = provider.providers[0].engine_cls(None)
assert engine.__class__.__module__ == 'basilisk.provider_engine.loom_engine'
assert engine.models[0].id == 'loom-qwen3-base'
from basilisk.conversation import Conversation, Message, MessageBlock, MessageRoleEnum
from basilisk.provider_ai_model import AIModelInfo
block = MessageBlock(request=Message(role=MessageRoleEnum.USER, content='Scripted dependency check'),
    model=AIModelInfo(provider_id='loom', model_id='loom-qwen3-base'), max_tokens=512,
    loom_purpose='Verify the local route imports without provider SDKs')
result = engine.completion(block, Conversation(), None,
    loom_purpose=block.loom_purpose, loom_practice_options=block.loom_practice_options)
assert result['status'] == 'COMPLETED' and len(global_vars.loom_bridge.calls) == 1
modules = {}
for name, module in sorted(sys.modules.items()):
    filename = getattr(module, '__file__', None)
    if name.startswith('basilisk.') and filename:
        path = Path(filename).resolve()
        assert path.is_relative_to(payload / 'gui'), (name, str(path))
        modules[name] = {'path':path.relative_to(payload).as_posix(),
                         'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
assert len(modules) > 30
for name in ('anthropic_engine', 'openai_engine', 'mistralai_engine', 'mistralai_ocr', 'gemini_engine', 'ollama_engine'):
    assert 'basilisk.provider_engine.' + name not in modules
report = {'schema':'lodestone-desktop-loom-imports/v1', 'status':'PASSED', 'payload':str(payload),
          'lock_sha256':hashlib.sha256((payload / 'dependencies.lock.json').read_bytes()).hexdigest(),
          'distributions':actual, 'modules':modules, 'provider_registry':['loom'],
          'scripted_bridge_calls':1, 'model_calls':0, 'paid_api_calls':0}
(root / 'imports.json').write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
faulthandler.cancel_dump_traceback_later()
'''
        with tempfile.TemporaryDirectory(prefix="lodestone-loom-imports-") as directory:
            try:
                result = subprocess.run([sys.executable, "-B", "-c", code, str(PAYLOAD), directory],
                                        capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired as exc:
                self.fail("Loom import fixture timed out: " + str(exc.stdout) + str(exc.stderr))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            raw = (Path(directory) / "imports.json").read_bytes()
            report = json.loads(raw)
            self.assertEqual(report["status"], "PASSED")
            evidence = os.environ.get("LODESTONE_GUI_EVIDENCE_DIR")
            if evidence:
                output = Path(evidence)
                output.mkdir(parents=True, exist_ok=True)
                (output / "loom-imports.json").write_bytes(raw)


if __name__ == "__main__":
    unittest.main()
