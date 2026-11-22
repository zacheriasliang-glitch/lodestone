#!/usr/bin/env python3
"""Verify prior local evidence without inference; preserve fresh CI checks in logs.

This verifier uses only the standard library. It never imports a provider or
claims that recorded local generation happened on the fresh CI machine.
"""
from __future__ import annotations
import argparse
import ast
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

MAX_MANIFEST = 65536
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EVIDENCE = 32 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')
PRIVATE = ('/Users/', '/home/', 'Library/Application Support', 'codex-remote-attachments',
           'file://', 'BEGIN PRIVATE KEY', 'BEGIN OPENSSH PRIVATE KEY')
TOKENIZER_NAMES = {'added_tokens.json','config.json','merges.txt','special_tokens_map.json',
                   'tokenizer.json','tokenizer_config.json','vocab.json'}
TOP_FIELDS = {'schema','attribution','scope','base_public_commit','runtime_code_sha256',
              'system_prompt_sha256','dependencies_lock_sha256','tokenizer_files','model_digest',
              'attempts','fresh_ci_model_calls','paid_api_calls','weight_updates','semantic_learning'}
ATTEMPT_FIELDS = {'ordinal','case_id','runtime_arm','status','provider_called','provider_completed',
                  'binding_valid','format_valid','semantic_assessable','stop_confirmed','reason',
                  'prompt_tokens','generated_tokens','elapsed_seconds','response','response_sha256',
                  'response_characters','request_sha256','rendered_prompt_sha256','original_receipt_sha256'}


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def code_hash(root):
    files={str(p.relative_to(root)):sha(p) for p in sorted(Path(root).rglob('*.py'))
           if '__pycache__' not in p.parts}
    return hashlib.sha256(canonical(files)).hexdigest()


def system_prompt_hash(runtime):
    tree=ast.parse((Path(runtime)/'src/lodestone_machinery/loom_adapter.py').read_text())
    values=[ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='SYSTEM_PROMPT' for t in n.targets)]
    if len(values)!=1 or not isinstance(values[0],str): raise ValueError('Unrecognized literal system prompt')
    return hashlib.sha256(values[0].encode()).hexdigest()


def validate_manifest(path):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>MAX_MANIFEST: raise ValueError('Manifest must be bounded regular text')
    raw=path.read_bytes(); text=raw.decode()
    if any(v in text for v in PRIVATE) or re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{30,})\b',text):
        raise ValueError('Private path or credential pattern in public evidence')
    data=json.loads(raw)
    if set(data)!=TOP_FIELDS or data['schema']!='lodestone-prior-local-study/v1': raise ValueError('Manifest fields differ')
    if data['attribution']!='DECLARED_PRIOR_LOCAL_EVIDENCE_NOT_FRESH_CI_INFERENCE': raise ValueError('Prior evidence attribution missing')
    if any(data[k]!=0 for k in ('fresh_ci_model_calls','paid_api_calls','weight_updates')) or data['semantic_learning']!='NOT_ESTABLISHED':
        raise ValueError('Unsupported inference, billing or learning claim')
    if set(data['runtime_code_sha256'])!={'baseline','candidate'} or set(data['tokenizer_files'])!=TOKENIZER_NAMES:
        raise ValueError('Incomplete source/tokenizer identity')
    for value in [*data['runtime_code_sha256'].values(),data['system_prompt_sha256'],data['dependencies_lock_sha256'],data['model_digest']]:
        if not isinstance(value,str) or not HEX.fullmatch(value): raise ValueError('Invalid identity digest')
    for name,v in data['tokenizer_files'].items():
        if set(v)!={'bytes','sha256'} or type(v['bytes']) is not int or not 0<v['bytes']<20*1024*1024 or not HEX.fullmatch(v['sha256']):
            raise ValueError('Invalid tokenizer file identity')
    if len(data['attempts'])!=2: raise ValueError('Both actual local calibration outcomes are required')
    for row,ordinal,status,runtime in zip(data['attempts'],(1,2),('INTERRUPTED','COMPLETED'),('baseline','candidate')):
        if set(row)!=ATTEMPT_FIELDS or row['ordinal']!=ordinal or row['case_id']!='SR00'+str(ordinal) or row['status']!=status or row['runtime_arm']!=runtime:
            raise ValueError('Calibration identity/status differs from the declared evidence')
        if row['provider_called'] is not True or row['binding_valid'] is not True or row['stop_confirmed'] is not True:
            raise ValueError('Actual provider/input/stop evidence is missing')
        if row['provider_completed'] is not (ordinal==2) or row['format_valid'] is not False or row['semantic_assessable'] is not False:
            raise ValueError('Completion or answer-quality claim differs from the actual result')
        response=row['response']
        if not isinstance(response,str) or len(response.encode())>8192 or len(response)!=row['response_characters']:
            raise ValueError('Bounded response length differs')
        if hashlib.sha256(response.encode()).hexdigest()!=row['response_sha256']: raise ValueError('Actual response changed')
        for key in ('response_sha256','request_sha256','rendered_prompt_sha256','original_receipt_sha256'):
            if not HEX.fullmatch(row[key]): raise ValueError('Invalid attempt evidence digest')
        if type(row['prompt_tokens']) is not int or not 0<row['prompt_tokens']<=8192:
            raise ValueError('Invalid actual prompt token count')
        if row['generated_tokens'] is not None and (type(row['generated_tokens']) is not int or not 0<=row['generated_tokens']<=512):
            raise ValueError('Invalid generated token count')
    return data


def verify(preview, source, evidence):
    preview,source=Path(preview).resolve(),Path(source).resolve()
    data=validate_manifest(evidence); active=read(preview/'ACTIVE.json')
    release=Path(active['release']).resolve()
    if release!=preview/'releases'/active['release_id']: raise ValueError('Active release escaped its preview')
    expected=data['runtime_code_sha256']['candidate']
    if code_hash(source/'desktop/runtime')!=expected or code_hash(release/'runtime')!=expected:
        raise ValueError('Checked-out or installed runtime differs from the previously exercised candidate')
    if system_prompt_hash(release/'runtime')!=data['system_prompt_sha256']:
        raise ValueError('Installed system prompt differs')
    if sha(source/'desktop/dependencies.lock.json')!=data['dependencies_lock_sha256'] or sha(release/'dependencies.lock.json')!=data['dependencies_lock_sha256']:
        raise ValueError('Dependency lock changed')
    config=read(preview/'Loom/runtime.json')
    expected_config={'backend':'ollama_qwen3_base','adapter_state':None,'ollama_model':'qwen3:0.6b',
                     'ollama_model_digest':data['model_digest'],'max_new_tokens':512,
                     'max_total_tokens':8192,'max_rss_bytes':4294967296,'wall_seconds':1200}
    if any(config.get(k)!=v for k,v in expected_config.items()): raise ValueError('Installed model/profile bounds differ')
    tokenizer=Path(config['assets_dir']).resolve()
    if not tokenizer.is_relative_to(preview): raise ValueError('Tokenizer is not in this fresh preview')
    for name,v in data['tokenizer_files'].items():
        path=tokenizer/name
        if path.is_symlink() or path.stat().st_size!=v['bytes'] or sha(path)!=v['sha256']:
            raise ValueError('Installed tokenizer changed: '+name)
    return {'schema':'lodestone-prior-evidence-check/v1','status':'VERIFIED_PRIOR_EVIDENCE_AND_INSTALLED_IDENTITY',
            'manifest_sha256':sha(evidence),'installed_runtime_code_sha256':expected,
            'tokenizer_files_verified':len(data['tokenizer_files']), 'fresh_ci_model_calls':0,
            'prior_local_provider_calls':2,'prior_completed_responses':1,
            'prior_decision_format_passes':0,'semantic_learning':'NOT_ESTABLISHED','weight_updates':0,'paid_api_calls':0,
            'scope':'Fresh installation/source/tokenizer identity is verified against an explicitly attributed prior local evidence record. This CI run does not independently reproduce inference or prove answer quality.'}


def safe_name(name):
    p=PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or str(p)!=name: raise ValueError('Unsafe archive path')
    return name


def evidence_files(root):
    root=Path(root).resolve(); result={}; total=0
    for p in sorted(root.rglob('*')):
        name=str(p.relative_to(root)); parts=PurePosixPath(name).parts
        selected=(len(parts)==1 and p.suffix in ('.json','.txt') or
                  len(parts)==2 and parts[0]=='logs' and p.suffix in ('.log','.txt') or
                  len(parts)>=2 and parts[0]=='native')
        if not selected or p.is_dir(): continue
        if p.is_symlink() or not p.is_file(): raise ValueError('Only regular evidence files are allowed')
        safe_name(name); total+=p.stat().st_size
        if p.stat().st_size>8*1024*1024 or total>MAX_EVIDENCE or len(result)>=4096:
            raise ValueError('Evidence export exceeds bound')
        result[name]=p.read_bytes()
    if not result: raise ValueError('No generated evidence exists')
    return result


def envelope(files):
    inventory={safe_name(k):{'bytes':len(v),'sha256':hashlib.sha256(v).hexdigest()} for k,v in sorted(files.items())}
    if 'EVIDENCE-MANIFEST.json' in files: raise ValueError('Reserved archive filename')
    manifest=canonical({'schema':'lodestone-ci-log-files/v1','files':inventory})
    packed=io.BytesIO()
    with tarfile.open(fileobj=packed,mode='w',format=tarfile.USTAR_FORMAT) as archive:
        for name,value in sorted({**files,'EVIDENCE-MANIFEST.json':manifest}.items()):
            info=tarfile.TarInfo(safe_name(name)); info.size=len(value); info.mode=0o600; info.mtime=0
            archive.addfile(info,io.BytesIO(value))
    raw=packed.getvalue()
    if len(raw)>MAX_EVIDENCE+2*1024*1024: raise ValueError('Uncompressed archive exceeds bound')
    zipped=gzip.compress(raw,compresslevel=9,mtime=0)
    if len(zipped)>MAX_ARCHIVE: raise ValueError('Compressed archive exceeds bound')
    encoded=base64.b64encode(zipped).decode(); chunks=[encoded[i:i+4096] for i in range(0,len(encoded),4096)]
    header={'schema':'lodestone-ci-log-envelope/v1','compressed_bytes':len(zipped),'tar_bytes':len(raw),
            'archive_sha256':hashlib.sha256(zipped).hexdigest(),'manifest_sha256':hashlib.sha256(manifest).hexdigest(),
            'chunks':len(chunks),'files':len(files),'encoding':'tar.gz+base64'}
    return header,chunks


def recover(log, output):
    header=None; chunks={}; ended=False
    for line in Path(log).read_text().splitlines():
        for marker in ('LODESTONE_ARCHIVE_BEGIN ','LODESTONE_ARCHIVE_CHUNK ','LODESTONE_ARCHIVE_END '):
            if marker not in line: continue
            value=line.split(marker,1)[1]
            if marker.endswith('BEGIN '):
                if header is not None: raise ValueError('Choose one job envelope per log')
                header=json.loads(value)
            elif marker.endswith('CHUNK '):
                index,payload=value.split(' ',1); index=int(index)
                if index in chunks: raise ValueError('Duplicate archive chunk')
                chunks[index]=payload
            else:
                if header is None or value!=header['archive_sha256']: raise ValueError('Archive end identity differs')
                ended=True
    if not header or not ended or header['schema']!='lodestone-ci-log-envelope/v1' or set(chunks)!=set(range(header['chunks'])):
        raise ValueError('Incomplete archive envelope')
    if header['compressed_bytes']>MAX_ARCHIVE or header['tar_bytes']>MAX_EVIDENCE+2*1024*1024:
        raise ValueError('Declared evidence size exceeds bound')
    zipped=base64.b64decode(''.join(chunks[i] for i in range(header['chunks'])),validate=True)
    if len(zipped)!=header['compressed_bytes'] or hashlib.sha256(zipped).hexdigest()!=header['archive_sha256']:
        raise ValueError('Archive digest/size mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(zipped)) as stream:
        raw=stream.read(MAX_EVIDENCE+2*1024*1024+1)
    if len(raw)!=header['tar_bytes']: raise ValueError('Uncompressed archive size mismatch')
    values={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
        for member in archive:
            name=safe_name(member.name)
            if not member.isfile() or name in values or member.size>8*1024*1024 or len(values)>4096:
                raise ValueError('Unsafe or duplicate archive member')
            values[name]=archive.extractfile(member).read()
    manifest=values.pop('EVIDENCE-MANIFEST.json')
    if hashlib.sha256(manifest).hexdigest()!=header['manifest_sha256']: raise ValueError('File-manifest digest mismatch')
    expected=json.loads(manifest)['files']
    if set(expected)!=set(values): raise ValueError('Archive file inventory differs')
    for name,raw in values.items():
        if len(raw)!=expected[name]['bytes'] or hashlib.sha256(raw).hexdigest()!=expected[name]['sha256']:
            raise ValueError('Recovered file differs: '+name)
    output=Path(output)
    if output.exists(): raise ValueError('Recover into a new directory to preserve prior evidence')
    output.mkdir(parents=True)
    for name,raw in values.items():
        p=output/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(raw)
    (output/'EVIDENCE-MANIFEST.json').write_bytes(manifest)
    return {'status':'RECOVERED_VERIFIED_BYTES','files':len(values),'archive_sha256':header['archive_sha256']}


def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='command',required=True)
    v=sub.add_parser('verify'); v.add_argument('--preview',required=True); v.add_argument('--source',required=True); v.add_argument('--evidence',required=True); v.add_argument('--output',required=True)
    e=sub.add_parser('emit'); e.add_argument('--root',required=True)
    r=sub.add_parser('recover'); r.add_argument('--log',required=True); r.add_argument('--output',required=True)
    a=p.parse_args()
    if a.command=='verify':
        result=verify(a.preview,a.source,a.evidence); Path(a.output).write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result,sort_keys=True))
    elif a.command=='emit':
        header,chunks=envelope(evidence_files(a.root)); print('LODESTONE_ARCHIVE_BEGIN '+json.dumps(header,sort_keys=True),flush=True)
        for i,value in enumerate(chunks): print('LODESTONE_ARCHIVE_CHUNK '+str(i)+' '+value,flush=True)
        print('LODESTONE_ARCHIVE_END '+header['archive_sha256'],flush=True)
    else: print(json.dumps(recover(a.log,a.output),sort_keys=True))

if __name__=='__main__': main()
