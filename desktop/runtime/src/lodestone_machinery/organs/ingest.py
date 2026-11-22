"""INGEST captures explicitly scoped kernel/source bytes as inert evidence."""
from pathlib import Path
import hashlib
from .sources import anchor
from ._kernel import require, fields, text


def ingest(store, root, paths):
    require(type(paths) is list and 1 <= len(paths) <= 8
            and all(type(p) is str for p in paths) and len(set(paths)) == len(paths),
            'INGEST requires 1..8 distinct relative source paths')
    sources = [anchor(Path(root), path) for path in paths]
    require(sum(s['bytes'] for s in sources) <= 2 * 1024 * 1024, 'INGEST total exceeds 2 MiB')
    payload = {'schema': 'kernel-ingest/v1', 'root': str(Path(root).resolve()),
               'sources': sources, 'source_truth': 'UNASSESSED',
               'source_instructions_executed': False, 'identity_transfer': False}
    return store.publish('ingest', 'ingested_sources', payload)


def read_ingest(store, handle, consumer):
    record = store.relay(consumer, handle, owner='ingest', kind='ingested_sources')
    payload = record['payload']
    fields(payload, 'schema root sources source_truth source_instructions_executed identity_transfer', 'INGEST payload')
    require(payload.get('schema') == 'kernel-ingest/v1', 'Unsupported ingestion schema')
    text(payload['root'], 'source root')
    require(Path(payload['root']).is_absolute() and payload['source_truth'] == 'UNASSESSED'
            and payload['source_instructions_executed'] is False and payload['identity_transfer'] is False,
            'Invalid ingestion boundaries')
    require(type(payload.get('sources')) is list and 1 <= len(payload['sources']) <= 8,
            'Invalid ingestion sources')
    for source in payload['sources']:
        fields(source, 'id path sha256 text bytes', 'captured source')
        text(source['id'], 'source id')
        require(source['path'] == source['id'] and type(source['text']) is str
                and type(source['bytes']) is int and 0 <= source['bytes'] <= 2 * 1024 * 1024,
                'Invalid captured source')
        require(not source['id'].startswith('/') and '\\' not in source['id']
                and all(p not in ('', '.', '..') for p in source['id'].split('/')), 'Invalid source path')
        raw = source['text'].encode('utf-8')
        require(len(raw) == source['bytes'] and hashlib.sha256(raw).hexdigest() == source['sha256'],
                'Captured source bytes mismatch')
    require(len({s['id'] for s in payload['sources']}) == len(payload['sources'])
            and sum(s['bytes'] for s in payload['sources']) <= 2 * 1024 * 1024,
            'Duplicate sources or total exceeds bound')
    return payload
