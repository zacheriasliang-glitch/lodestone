"""Foreground evidence observer. Observation never repairs or approves state."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
from ._kernel import require
from .ingest import read_ingest
from .sources import _source_path, MAX_SOURCE_BYTES
from ..records import HANDLE
from ..yggdrasil_sources import read_bound_sources, binding_payload, BoundSourceError


def observe(store, handles, *, capture_roots=None):
    require(type(handles) is list and 1 <= len(handles) <= 16
            and all(type(h) is str and HANDLE.fullmatch(h) for h in handles)
            and len(set(handles)) == len(handles), 'Watcher requires 1..16 distinct record handles')
    capture_roots = {} if capture_roots is None else capture_roots
    require(type(capture_roots) is dict and len(capture_roots) <= 16
            and all(type(h) is str and HANDLE.fullmatch(h) and isinstance(p, (str, Path))
                    for h, p in capture_roots.items()), 'Explicit capture root mappings required')
    pending, seen, source_seen, issues = list(handles), set(), set(), []
    records_ok, sources_ok, source_bytes = 0, 0, 0
    bound_slots, bound_attempted, used_roots = 0, 0, set()
    while pending:
        handle = pending.pop()
        if handle in seen:
            continue
        if len(seen) >= 128:
            issues.append({'code': 'COVERAGE_LIMIT', 'detail': '128 record observation bound reached'})
            break
        seen.add(handle)
        try:
            record = store.read(handle)
        except (OSError, ValueError) as exc:
            issues.append({'code': 'RECORD_UNAVAILABLE', 'handle': handle, 'detail': str(exc)[:500]})
            continue
        records_ok += 1
        pending.extend(record['links'])
        if (record['owner'], record['kind']) == ('origami', 'source_bindings'):
            used_roots.add(handle)
            finding = {'handle': handle, 'source': 'bound-capture',
                       'expected_sha256': handle.split(':', 1)[1]}
            if handle not in capture_roots:
                issues.append({**finding, 'code': 'SOURCE_UNAVAILABLE',
                               'detail': 'Explicit current capture root was not supplied; saved roots are attribution only'})
                continue
            remaining = 8 * 1024 * 1024 - source_bytes
            entries = []
            try:
                capture = binding_payload(record)
                entries = capture['scope']['sources']
                allowance = max(0, 32 - len(source_seen) - bound_slots)
                bound_slots += min(len(entries), allowance)
                checked = read_bound_sources(record, capture_roots[handle],
                                             max_bytes=remaining, max_sources=allowance)
                source_bytes += checked['read_bytes']
                bound_attempted += checked['sources_attempted']
                sources_ok += len(entries)
            except (OSError, BoundSourceError) as exc:
                source_bytes += min(remaining, getattr(exc, 'read_bytes', remaining))
                bound_attempted += getattr(exc, 'sources_attempted', 0)
                path = getattr(exc, 'source', None)
                if path is not None:
                    for entry in entries:
                        if entry['path'] == path:
                            finding.update(source=path, expected_sha256=entry['sha256'])
                            break
                code = exc.code if isinstance(exc, BoundSourceError) else 'SOURCE_UNAVAILABLE'
                issue = {**finding, 'code': code, 'detail': str(exc)[:500]}
                if getattr(exc, 'observed_sha256', None) is not None:
                    issue['observed_sha256'] = exc.observed_sha256
                issues.append(issue)
            continue
        if (record['owner'], record['kind']) != ('ingest', 'ingested_sources'):
            continue
        try:
            capture = read_ingest(store, handle, 'watcher')
        except (OSError, ValueError) as exc:
            issues.append({'code': 'CAPTURE_INVALID', 'handle': handle, 'detail': str(exc)[:500]})
            continue
        for source in capture['sources']:
            identity = (capture['root'], source['id'], source['sha256'])
            if identity in source_seen:
                continue
            remaining = 8 * 1024 * 1024 - source_bytes
            if len(source_seen) + bound_slots >= 32 or remaining <= 0:
                issues.append({'code': 'COVERAGE_LIMIT', 'handle': handle,
                               'detail': '32 source / 8 MiB observation bound reached'})
                continue
            source_seen.add(identity)
            finding = {'handle': handle, 'source': source['id'], 'expected_sha256': source['sha256']}
            try:
                path = _source_path(Path(capture['root']), source['id'])
                allowance = min(remaining, MAX_SOURCE_BYTES + 1)
                with path.open('rb') as stream:
                    raw = stream.read(allowance)
            except (OSError, ValueError) as exc:
                # A failed read may have consumed bytes internally. Conservatively
                # charge its maximum allowance, retaining the actual-read ceiling.
                source_bytes += min(remaining, MAX_SOURCE_BYTES + 1)
                issues.append({**finding, 'code': 'SOURCE_UNAVAILABLE', 'detail': str(exc)[:500]})
                continue
            source_bytes += len(raw)
            if len(raw) == allowance:
                issues.append({**finding, 'code': 'COVERAGE_LIMIT',
                               'detail': 'Fresh file reaches remaining read budget or exceeds 2 MiB; not verified'})
                continue
            current_hash = hashlib.sha256(raw).hexdigest()
            if current_hash != source['sha256']:
                issues.append({**finding, 'code': 'SOURCE_DRIFT', 'observed_sha256': current_hash})
            else:
                sources_ok += 1
    require(not set(capture_roots) - used_roots, 'Capture root mapping does not select a reachable source_bindings record')
    payload = {'schema': 'watcher-observation/v1', 'observed_at': datetime.now(timezone.utc).isoformat(),
               'targets': handles, 'records_attempted': len(seen), 'records_intact': records_ok,
               'sources_attempted': len(source_seen) + bound_attempted, 'sources_fresh': sources_ok,
               'source_read_bytes_charged': source_bytes,
               'issues': issues, 'status': 'ATTENTION' if issues else 'INTACT',
               'scope': 'Record bytes/references, INGEST captures and explicitly mapped Origami source bindings; no semantic verdict',
               'repair_performed': False, 'authority': 'NONE'}
    # Targets are observations, not required links: a damaged target must not make
    # the saved report of that damage unreadable. Its handle is retained verbatim.
    return store.publish('watcher', 'observation', payload)
