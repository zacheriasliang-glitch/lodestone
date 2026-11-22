import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('desktop_prior_evidence',ROOT/'tools/desktop_prior_evidence.py')
check=importlib.util.module_from_spec(spec); spec.loader.exec_module(check)
PUBLIC=ROOT/'docs/desktop-validation/prior-local-study.json'


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value))


def log_text(files):
    header,chunks=check.envelope(files)
    return '\n'.join(['2026-01-01T00:00:00Z LODESTONE_ARCHIVE_BEGIN '+json.dumps(header),
        *['2026-01-01T00:00:00Z LODESTONE_ARCHIVE_CHUNK '+str(i)+' '+v for i,v in enumerate(chunks)],
        '2026-01-01T00:00:00Z LODESTONE_ARCHIVE_END '+header['archive_sha256']])


class PriorEvidenceTests(unittest.TestCase):
    def test_actual_public_record_keeps_failure_and_no_fresh_inference_claim(self):
        data=check.validate_manifest(PUBLIC)
        self.assertEqual([r['status'] for r in data['attempts']],['INTERRUPTED','COMPLETED'])
        self.assertEqual(data['fresh_ci_model_calls'],0)
        self.assertTrue(all(not row['semantic_assessable'] for row in data['attempts']))

    def test_public_manifest_rejects_privacy_and_false_completion_changes(self):
        original=check.read(PUBLIC)
        changes=[lambda d:d.update(unexpected='private record'),
                 lambda d:d.update(scope='/Users/someone/private'),
                 lambda d:d['attempts'][0].update(provider_completed=True),
                 lambda d:d['attempts'][1].update(response='changed actual response'),
                 lambda d:d.update(fresh_ci_model_calls=1)]
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'evidence.json'
            for mutate in changes:
                data=copy.deepcopy(original);mutate(data);write(path,data)
                with self.assertRaises(ValueError):check.validate_manifest(path)

    def test_offline_verifier_binds_installed_runtime_and_tokenizer_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); source=base/'source'; preview=base/'preview'; release=preview/'releases/fixture'
            data=check.read(PUBLIC)
            for root in (source/'desktop',release):
                (root/'runtime/src/lodestone_machinery').mkdir(parents=True)
                (root/'runtime/src/lodestone_machinery/loom_adapter.py').write_text('SYSTEM_PROMPT = "synthetic fixture"\n')
                (root/'dependencies.lock.json').write_text('{}\n')
            data['runtime_code_sha256']['candidate']=check.code_hash(release/'runtime')
            data['system_prompt_sha256']=check.system_prompt_hash(release/'runtime')
            data['dependencies_lock_sha256']=check.sha(release/'dependencies.lock.json')
            tokenizer=preview/'tokenizer';tokenizer.mkdir()
            for name in check.TOKENIZER_NAMES:
                raw=name.encode();(tokenizer/name).write_bytes(raw)
                data['tokenizer_files'][name]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            evidence=base/'evidence.json';write(evidence,data)
            write(preview/'ACTIVE.json',{'release':str(release),'release_id':'fixture'})
            write(preview/'Loom/runtime.json',{'backend':'ollama_qwen3_base','adapter_state':None,
                'ollama_model':'qwen3:0.6b','ollama_model_digest':data['model_digest'],
                'max_new_tokens':512,'max_total_tokens':8192,'max_rss_bytes':4294967296,
                'wall_seconds':1200,'assets_dir':str(tokenizer)})
            result=check.verify(preview,source,evidence)
            self.assertEqual(result['fresh_ci_model_calls'],0)
            (tokenizer/'config.json').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'tokenizer'):check.verify(preview,source,evidence)
            (tokenizer/'config.json').write_bytes(b'config.json')
            (release/'runtime/new.py').write_text('changed=True\n')
            with self.assertRaisesRegex(ValueError,'runtime'):check.verify(preview,source,evidence)

    def test_log_envelope_recovers_exact_native_bytes(self):
        files={'native/learning/memory/organs/bezoar/records/a.json':b'{"actual":"record"}\n',
               'logs/install.log':'completed\nUnicode \u03bb'.encode(),'lifecycle.json':b'{"uninstalled":true}'}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); log=root/'job.log';log.write_text(log_text(files))
            receipt=check.recover(log,root/'recovered')
            self.assertEqual(receipt['files'],3)
            for name,raw in files.items():self.assertEqual((root/'recovered'/name).read_bytes(),raw)
            with self.assertRaisesRegex(ValueError,'new directory'):check.recover(log,root/'recovered')

    def test_log_envelope_refuses_missing_or_corrupted_chunks_and_unsafe_paths(self):
        with self.assertRaisesRegex(ValueError,'Unsafe'):check.envelope({'../escape':b'no'})
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);log=root/'job.log';text=log_text({'result.json':b'important actual outcome'})
            log.write_text('\n'.join(line for line in text.splitlines() if 'ARCHIVE_CHUNK' not in line))
            with self.assertRaisesRegex(ValueError,'Incomplete'):check.recover(log,root/'missing')
            lines=text.splitlines(); index=next(i for i,v in enumerate(lines) if 'ARCHIVE_CHUNK' in v)
            value=lines[index];lines[index]=value[:-1]+('A' if value[-1]!='A' else 'B');log.write_text('\n'.join(lines))
            with self.assertRaises((ValueError,base64.binascii.Error)):check.recover(log,root/'corrupt')

    def test_evidence_allowlist_excludes_downloads_payloads_and_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ('lifecycle.json','logs/install.log','native/learning/ACTIVE.json',
                         'download/model.bin','extracted/source.py','synthetic-upgrade-payload/secret.json','environment.dump'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
            files=check.evidence_files(root)
            self.assertEqual(set(files),{'lifecycle.json','logs/install.log','native/learning/ACTIVE.json'})
            (root/'native/escape').symlink_to(root/'lifecycle.json')
            with self.assertRaisesRegex(ValueError,'regular'):check.evidence_files(root)


if __name__=='__main__':unittest.main()
