"""Untrusted lexical catalogues and complete-source evidence packets.

The catalogue is derived cache metadata, never source authority. Pin its
``content_sha256`` outside the cache; recomputing that pin from an untrusted
catalogue defeats tamper detection. Packet assembly checks every candidate's
immutable source record, but decodes only explicitly selected/required sources.
Nothing in source text is interpreted as an instruction or a required source.
"""
from collections import Counter
import hashlib
import json
import re
import unicodedata

from . import crystals as cr


CATALOGUE_VERSION = "experimental-crystal-claim-catalogue/v1"
PACKET_VERSION = "experimental-crystal-claim-packet/v1"
MAX_SOURCES = 512
MAX_SOURCE_BYTES = 32768
MAX_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_QUERY_BYTES = 4096
MAX_PACKET_BYTES = 1024 * 1024
TOKENIZER = {"algorithm": "unicode-word-casefold/v1", "pattern": r"\w+",
             "normalization": "casefold-only", "unicode_version": unicodedata.unidata_version}
FEATURE_NAMES = ["overlap_terms", "query_covered_tokens", "query_tokens",
                 "query_unique_terms", "document_tokens", "document_unique_terms",
                 "document_bytes"]
_WORDS = re.compile(r"\w+", re.UNICODE)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CATALOGUE_FIELDS = {"schema", "authority", "freshness", "cache_role", "tokenizer",
                     "sources", "content_sha256"}
_SOURCE_FIELDS = {"source_id", "sha256", "size", "record_sha256", "term_counts",
                  "term_count", "unique_term_count"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise cr.CrystalLimitError(f"{label} must be an integer from {low} to {high}")
    return value


def _hash(value, label):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise cr.CrystalError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _ids(values, label):
    if type(values) not in (list, tuple) or len(values) > MAX_SOURCES:
        raise cr.CrystalLimitError(f"{label} must be a list/tuple of at most {MAX_SOURCES} IDs")
    return sorted({cr._source_id(value) for value in values})


def _catalogue_bytes(value):
    """Same canonical JSON as packet_bytes, stopping at the catalogue bound."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    chunks, total = [], 0
    try:
        for piece in encoder.iterencode(value):
            chunk = piece.encode("utf-8")
            total += len(chunk)
            if total > MAX_CATALOGUE_BYTES:
                raise cr.CrystalLimitError("Catalogue exceeds 4 MiB")
            chunks.append(chunk)
    except cr.CrystalLimitError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise cr.CrystalError("Catalogue must contain valid UTF-8 JSON") from exc
    return b"".join(chunks)


def _terms(text):
    # No truncation, stemming, stopwords, labels, IDs, or provenance features.
    return dict(sorted(Counter(_WORDS.findall(text.casefold())).items()))


def _record(store, source_id):
    record = store._source(source_id)
    _integer(record["size"], 0, MAX_SOURCE_BYTES, "Source bytes")
    return record


def _recover(store, record):
    raw = store.get(record["source_id"])
    if type(raw) is not bytes or len(raw) != record["size"] or _sha(raw) != record["sha256"]:
        raise cr.CrystalCorruptionError("Recovered source differs from pinned hash or size")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise cr.CrystalError("Claim sources must be valid UTF-8") from exc
    return raw, text


def build_catalogue(store, candidate_source_ids):
    """Return (pure JSON catalogue, work) after one get per unique source ID.

    ``index_serialized_bytes`` is canonical catalogue JSON bytes, no newline.
    ``source_bytes_read`` counts complete recovered bytes, including equal-body
    aliases; it is not compressed disk I/O or decoder internal graph work.
    IDs sort lexically so input ordering/duplicate IDs cannot change the cache.
    """
    source_ids = _ids(candidate_source_ids, "candidate_source_ids")
    records = [_record(store, source_id) for source_id in source_ids]
    catalogue = {"schema": CATALOGUE_VERSION, "authority": "NONE", "freshness": "UNCHECKED",
                 "cache_role": "UNTRUSTED_DERIVED_METADATA", "tokenizer": dict(TOKENIZER),
                 "sources": []}
    source_bytes = 0
    estimated_bytes = len(_catalogue_bytes(catalogue))
    for record in records:
        raw, text = _recover(store, record)
        counts = _terms(text)
        source_bytes += len(raw)
        entry = {
            "source_id": record["source_id"], "sha256": record["sha256"], "size": len(raw),
            "record_sha256": _sha(cr.packet_bytes(record)), "term_counts": counts,
            "term_count": sum(counts.values()), "unique_term_count": len(counts)}
        estimated_bytes += len(_catalogue_bytes(entry)) + bool(catalogue["sources"])
        if estimated_bytes > MAX_CATALOGUE_BYTES:
            raise cr.CrystalLimitError("Catalogue exceeds 4 MiB")
        catalogue["sources"].append(entry)
    catalogue["content_sha256"] = _sha(_catalogue_bytes(catalogue))
    serialized = _catalogue_bytes(catalogue)
    return catalogue, {"source_bytes_read": source_bytes, "source_get_calls": len(records),
                       "explicit_source_record_checks": len(records),
                       "index_serialized_bytes": len(serialized)}


def validate_catalogue(catalogue, *, expected_catalogue_sha256, store=None):
    """Validate a caller-pinned cache; optionally recheck all store records.

    A valid hash establishes cache identity, not semantic correctness. Lexical
    counts are established at build time, not recomputed by selection. Supplying
    the store checks metadata/record identity without any source graph decode.
    """
    _hash(expected_catalogue_sha256, "Expected catalogue hash")
    if type(catalogue) is not dict or set(catalogue) != _CATALOGUE_FIELDS:
        raise cr.CrystalError("Invalid catalogue fields")
    if (catalogue["schema"] != CATALOGUE_VERSION or catalogue["authority"] != "NONE"
            or catalogue["freshness"] != "UNCHECKED"
            or catalogue["cache_role"] != "UNTRUSTED_DERIVED_METADATA"
            or type(catalogue["tokenizer"]) is not dict or catalogue["tokenizer"] != TOKENIZER):
        raise cr.CrystalError("Invalid catalogue contract or tokenizer version")
    _hash(catalogue["content_sha256"], "Catalogue hash")
    sources = catalogue["sources"]
    if type(sources) is not list or len(sources) > MAX_SOURCES:
        raise cr.CrystalLimitError("Catalogue requires at most 512 sources")
    source_ids = []
    for entry in sources:
        if type(entry) is not dict or set(entry) != _SOURCE_FIELDS:
            raise cr.CrystalError("Invalid catalogue source fields")
        source_ids.append(cr._source_id(entry["source_id"]))
        _hash(entry["sha256"], "Source hash")
        _hash(entry["record_sha256"], "Source record hash")
        size = _integer(entry["size"], 0, MAX_SOURCE_BYTES, "Source bytes")
        _integer(entry["term_count"], 0, size, "Document term count")
        _integer(entry["unique_term_count"], 0, size, "Document unique term count")
        counts = entry["term_counts"]
        if type(counts) is not dict or len(counts) > size:
            raise cr.CrystalError("Invalid lexical term counts")
        for term, count in counts.items():
            if (type(term) is not str or not term or len(term) > 3 * MAX_SOURCE_BYTES
                    or not _WORDS.fullmatch(term) or term.casefold() != term):
                raise cr.CrystalError("Invalid normalized lexical term")
            _integer(count, 1, size, "Lexical term frequency")
        if entry["term_count"] != sum(counts.values()) or entry["unique_term_count"] != len(counts):
            raise cr.CrystalError("Lexical totals differ from term counts")
    if source_ids != sorted(set(source_ids)):
        raise cr.CrystalError("Catalogue source IDs must be unique and sorted")
    _catalogue_bytes(catalogue)
    body = {key: value for key, value in catalogue.items() if key != "content_sha256"}
    if _sha(_catalogue_bytes(body)) != catalogue["content_sha256"]:
        raise cr.CrystalCorruptionError("Catalogue content hash mismatch")
    if catalogue["content_sha256"] != expected_catalogue_sha256:
        raise cr.CrystalCorruptionError("Catalogue differs from expected pinned hash")
    if store is not None:
        for entry in sources:
            record = _record(store, entry["source_id"])
            if (record["sha256"] != entry["sha256"] or record["size"] != entry["size"]
                    or _sha(cr.packet_bytes(record)) != entry["record_sha256"]):
                raise cr.CrystalCorruptionError("Catalogue differs from exact store source record")
    return catalogue


def features(query, catalogue, *, expected_catalogue_sha256):
    """Return generic integer features in catalogue source order; never decode.

    Overlap counts distinct query terms present in the document. Coverage is
    an integer numerator (query occurrences whose term appears) plus the query
    token denominator. Empty queries have zero coverage, with denominator zero.
    Source IDs are output handles only; labels, keys and provenance never enter
    feature values. This function does not rank, select or train a policy.
    """
    if type(query) is not str:
        raise cr.CrystalError("Query must be UTF-8 text")
    try:
        query_bytes = query.encode("utf-8")
    except UnicodeError as exc:
        raise cr.CrystalError("Query must be UTF-8 text") from exc
    if len(query_bytes) > MAX_QUERY_BYTES:
        raise cr.CrystalLimitError("Query exceeds 4096 UTF-8 bytes")
    validate_catalogue(catalogue, expected_catalogue_sha256=expected_catalogue_sha256)
    query_counts = _terms(query)
    rows = []
    for entry in catalogue["sources"]:
        common = query_counts.keys() & entry["term_counts"].keys()
        values = [len(common), sum(query_counts[term] for term in common),
                  sum(query_counts.values()), len(query_counts), entry["term_count"],
                  entry["unique_term_count"], entry["size"]]
        rows.append({"source_id": entry["source_id"], "values": values})
    return {"catalogue_sha256": expected_catalogue_sha256, "feature_names": list(FEATURE_NAMES),
            "rows": rows, "work": {"source_bytes_read": 0, "source_get_calls": 0}}


def assemble_packet(store, catalogue, selected_source_ids, required_source_ids, max_bytes,
                    *, expected_catalogue_sha256, expected_required_record_sha256=None):
    """Load complete requested sources or fail; return (JSON packet, work).

    Required IDs are explicit caller input and may be outside the catalogue.
    ``expected_required_record_sha256`` must pin exactly those outside IDs to
    their immutable record hashes, obtained from a trusted earlier observation.
    Missing/extra pins fail before any decode. Required IDs inside the catalogue
    are already pinned there and must not also appear in this mapping.
    All candidate plus required IDs together are bounded to 512. Equal bodies
    deduplicate by exact SHA-256 + size; every loaded alias retains its complete
    immutable provenance and both roles. No source text is trimmed. Expansion
    repeats this call with the union of previous and additional selected IDs,
    preserving required IDs and the returned ``required_record_sha256`` pins,
    and allowing a larger budget up to 1 MiB.

    The budget is ``len(cr.packet_bytes(packet)) + 1`` for a trailing newline.
    This bounds serialized output only: an overbudget failure still decodes
    requested sources, and episode accounting must include that failed work.
    Freshness remains UNCHECKED: stored provenance is not a live-source check.
    """
    _integer(max_bytes, 1, MAX_PACKET_BYTES, "Packet budget bytes")
    selected = _ids(selected_source_ids, "selected_source_ids")
    required = _ids(required_source_ids, "required_source_ids")
    validate_catalogue(catalogue, expected_catalogue_sha256=expected_catalogue_sha256, store=store)
    entries = {entry["source_id"]: entry for entry in catalogue["sources"]}
    if any(source_id not in entries for source_id in selected):
        raise cr.CrystalError("Selected source ID is not a catalogue candidate")
    if len(set(entries) | set(required)) > MAX_SOURCES:
        raise cr.CrystalLimitError("Candidate and required sources exceed 512")
    outsiders = set(required) - set(entries)
    pins = {} if expected_required_record_sha256 is None else expected_required_record_sha256
    if type(pins) is not dict or len(pins) > MAX_SOURCES:
        raise cr.CrystalError("Required record pins must be a bounded source-ID-to-hash dictionary")
    for source_id, digest in pins.items():
        cr._source_id(source_id)
        _hash(digest, "Expected required source record hash")
    if set(pins) != outsiders:
        raise cr.CrystalError("Required record pins must exactly match required IDs outside the catalogue")
    pins = {source_id: pins[source_id] for source_id in sorted(pins)}
    loaded = sorted(set(selected) | set(required))
    records = {source_id: _record(store, source_id) for source_id in loaded}
    # Check the requested source identity again at the recovery boundary.
    for source_id, record in records.items():
        if source_id in entries and _sha(cr.packet_bytes(record)) != entries[source_id]["record_sha256"]:
            raise cr.CrystalCorruptionError("Source record changed before recovery")
        if source_id in pins and _sha(cr.packet_bytes(record)) != pins[source_id]:
            raise cr.CrystalCorruptionError("Required source record differs from expected pinned hash")
    bodies, references = {}, []
    source_bytes = required_bytes = selected_bytes = 0
    for source_id, record in records.items():
        raw, text = _recover(store, record)
        key = (record["sha256"], len(raw))
        if key in bodies and bodies[key]["text"] != text:
            raise cr.CrystalCorruptionError("Equal source hashes and sizes have different bytes")
        body = bodies.setdefault(key, {"sha256": key[0], "size": key[1], "text": text, "source_ids": []})
        body["source_ids"].append(source_id)
        roles = (["SELECTED"] if source_id in selected else []) + (["REQUIRED"] if source_id in required else [])
        references.append({"source_record": record, "record_sha256": _sha(cr.packet_bytes(record)),
                           "roles": roles, "freshness": "UNCHECKED"})
        source_bytes += len(raw)
        required_bytes += len(raw) if source_id in required else 0
        selected_bytes += len(raw) if source_id in selected else 0
    omissions = []
    for source_id, entry in entries.items():
        if source_id in records:
            continue
        equal_body = bodies.get((entry["sha256"], entry["size"]))
        omissions.append({"source_id": source_id, "sha256": entry["sha256"], "size": entry["size"],
                          "record_sha256": entry["record_sha256"],
                          "reason": "UNRECOVERED_EQUAL_BODY_ALIAS" if equal_body else "SOURCE_NOT_RECOVERED",
                          "equivalent_loaded_source_ids": list(equal_body["source_ids"]) if equal_body else [],
                          "expansion": {"operation": "assemble_packet", "add_selected_source_id": source_id}})
    packet = {"schema": PACKET_VERSION, "authority": "NONE", "freshness": "UNCHECKED",
              "catalogue_sha256": expected_catalogue_sha256, "selected_source_ids": selected,
              "required_source_ids": required, "required_record_sha256": pins,
              "bodies": list(bodies.values()),
              "references": references, "omissions": omissions,
              "integrity_scope": "ALL_CANDIDATE_RECORDS_AND_LOADED_SOURCE_BYTES",
              "unloaded_source_integrity": "UNCHECKED", "packet_bytes": 0}
    # The decimal width of packet_bytes can itself change the serialized size.
    while True:
        wire_size = len(cr.packet_bytes(packet)) + 1
        if wire_size == packet["packet_bytes"]:
            break
        packet["packet_bytes"] = wire_size
    if wire_size > max_bytes:
        raise cr.CrystalLimitError(f"Complete selected/required packet needs {wire_size} bytes; budget is {max_bytes}")
    return packet, {"source_bytes_read": source_bytes, "source_get_calls": len(records),
                    "explicit_source_record_checks": len(entries) + len(records),
                    "selected_source_bytes": selected_bytes, "required_source_bytes": required_bytes,
                    "unique_body_bytes": sum(body["size"] for body in bodies.values()),
                    "packet_wire_bytes_including_newline": wire_size}
