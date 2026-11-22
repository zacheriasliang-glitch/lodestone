"""Validate explicit, source-bound candidate plans; never generate candidates.

Callers first validate their existing source/table bounds and allowed modes.
Returned candidate tuples are snapshots, independent of mutable caller lists.
"""
import hashlib
import json

from . import codec

SCHEMA = 'crystal-supplied-candidates/v1'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def corpus_sha256(sources):
    """Pin the complete ordered source roster, including mandatory flags."""
    return _sha(_canonical([dict(id=s['id'], sha256=_sha(s['data']),
                                bytes=len(s['data']), mandatory=s['mandatory']) for s in sources]))


def _keys(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('Invalid supplied-candidate plan fields')


def _size(value, expected):
    if type(value) is not int or value != expected:
        raise ValueError('Supplied-candidate offset/size mismatch')


def validate(plan, sources, table, chunk_bytes, check):
    """Validate every binding and coordinate before callers may write artifacts."""
    _keys(plan, ('schema', 'corpus_sha256', 'table_sha256', 'sources'))
    if (plan['schema'] != SCHEMA or plan['corpus_sha256'] != corpus_sha256(sources)
            or plan['table_sha256'] != _sha(table)):
        raise ValueError('Supplied-candidate corpus/table binding mismatch')
    if type(plan['sources']) is not list or len(plan['sources']) != len(sources):
        raise ValueError('Supplied-candidate source roster mismatch')
    chunks = {}
    for bound, source in zip(plan['sources'], sources):
        check()
        _keys(bound, ('id', 'sha256', 'bytes', 'chunks'))
        raw = source['data']
        if bound['id'] != source['id'] or bound['sha256'] != _sha(raw):
            raise ValueError('Supplied-candidate source binding mismatch')
        _size(bound['bytes'], len(raw))
        count = max(1, (len(raw) + chunk_bytes - 1) // chunk_bytes)
        if type(bound['chunks']) is not list or len(bound['chunks']) != count:
            raise ValueError('Supplied-candidate chunk count mismatch')
        for ordinal, chunk in enumerate(bound['chunks']):
            check()
            _keys(chunk, ('offset', 'sha256', 'bytes', 'candidates'))
            offset = ordinal * chunk_bytes
            body = raw[offset:offset + chunk_bytes]
            _size(chunk['offset'], offset)
            _size(chunk['bytes'], len(body))
            if chunk['sha256'] != _sha(body):
                raise ValueError('Supplied-candidate chunk hash mismatch')
            sets = chunk['candidates']
            if type(sets) not in (list, tuple) or len(sets) != len(body) // codec.BLOCK_BYTES:
                raise ValueError('Supplied-candidate block count mismatch')
            snapshot = []
            for ranked in sets:
                check()
                if type(ranked) not in (list, tuple) or len(ranked) > codec.MAX_CANDIDATES:
                    raise ValueError('At most 64 candidates per block required')
                snapshot.append(tuple(codec.coordinates(codec.code_index(q)) for q in ranked))
            chunks[source['id'], offset] = tuple(snapshot)
    serialized = _canonical(plan)
    check()
    return chunks, dict(plan_sha256=_sha(serialized), plan_bytes=len(serialized),
                        plan_storage='CALLER_OWNED_NOT_RETAINED')
