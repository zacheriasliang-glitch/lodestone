"""Experimental three-codec decisions over exact, bounded source bytes.

This is a separate storage-policy experiment, not the six-action Origami P2.
Policy output selects an exact codec only; it carries no execution authority.
"""
from collections import Counter
import hashlib
import itertools
import json
import random
import sqlite3
import time

from . import crystals as cr
from . import transformer as tf
from .crystal_mutations import a3_features


VERSION = "experimental-crystal-codec-policy/v1"


class ExactRecoveryError(ValueError):
    """One candidate failed its exact recovered-byte comparison."""


CODECS = ("literal", "zlib", "recursive")
FEATURES = ("identity", "mutation", "random")
MAX_BYTES = 32768
MAX_PREFIX = 2
VOCABULARY = [f"B{value}" for value in range(-20, 21)] + ["OOR"] + list(CODECS)
ACTION_IDS = [VOCABULARY.index(codec) for codec in CODECS]
_rng = random.Random(211)
RANDOM_MATRIX = tuple(tuple(_rng.randint(-2, 2) for _ in range(3)) for _ in range(12))
del _rng
CONFIG = {"schema": VERSION, "codecs": list(CODECS), "action_ids": ACTION_IDS,
          "vocabulary": VOCABULARY, "max_bytes": MAX_BYTES, "prefix_max": MAX_PREFIX,
          "chunk_bytes": 4096, "bucket_rule": "zero=0; nonzero=sign*bit_length(abs(x)); abs(bucket)>20 is OOR",
          "random_seed": 211, "random_matrix": RANDOM_MATRIX,
          "model_config": {"width": 8, "heads": 2, "layers": 1, "ff_width": 16, "max_tokens": 12}}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _bytes(data):
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise ValueError("Codec policy requires exact bytes of at most 32768 bytes")
    return data


def codec_plan(document, codec):
    """Only adapter to core private codec machinery; verify and price one plan.

    Primary cost is canonical descriptor bytes plus payload bytes for each unique
    new node, and one canonical source record (including its checksum). Concat
    rules already are payload: never add them twice. This is not SQLite size.
    The source record is the store's immutable fields, excluding local integer ID.
    """
    if type(document) is not dict or set(document) != {"source_id", "data", "metadata"}:
        raise ValueError("Document needs source_id, data and metadata")
    source_id = cr._source_id(document["source_id"])
    data = _bytes(document["data"])
    if type(document["metadata"]) is not dict:
        raise ValueError("Document metadata must be a dictionary")
    metadata = cr.packet_bytes(document["metadata"])
    if len(metadata) > cr.MAX_METADATA_BYTES:
        raise ValueError("Document metadata exceeds its bound")
    if codec not in CODECS:
        raise ValueError("Unknown codec")
    root, nodes = cr._plan(data, codec)
    terms, truncated = cr._terms(data)
    record = {"source_id": source_id, "sha256": _sha(data), "size": len(data), "root": root,
              "strategy": codec, "metadata": document["metadata"], "indexed_terms": len(terms),
              "index_truncated": int(truncated)}
    # Use the production bounded decoder against an isolated in-memory node table.
    # It has no original-file access; compare to input only after decoding finishes.
    scratch = object.__new__(cr.CrystalStore)
    scratch._db = sqlite3.connect(":memory:")
    scratch._db.row_factory = sqlite3.Row
    try:
        scratch._db.execute("CREATE TABLE nodes(handle TEXT PRIMARY KEY,kind TEXT,size INTEGER,sha256 TEXT,payload BLOB)")
        scratch._db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?)", ((key,) + row for key, row in nodes.items()))
        recovered, _ = scratch._decode(record)
    finally:
        scratch.close()
    if recovered != data:
        raise ExactRecoveryError("Candidate exact recovery mismatch")
    record["record_sha256"] = _sha(cr.packet_bytes(record))
    return {"root": root, "nodes": nodes,
            "node_costs": {key: len(row[3]) + len(cr.packet_bytes(cr._descriptor(*row))) for key, row in nodes.items()},
            "source_record": record, "source_record_bytes": len(cr.packet_bytes(record)), "exact_recovery": True}


def continuation_oracle(documents, prefix=()):
    """At most 27 complete paths; costs conditional on first action include optimal continuation."""
    if type(documents) not in (list, tuple) or not 1 <= len(documents) <= 3:
        raise ValueError("Supply one to three candidate documents")
    if type(prefix) not in (list, tuple) or len(prefix) > MAX_PREFIX:
        raise ValueError("Supply at most two prefix documents")
    ids = [doc["source_id"] for doc in list(prefix) + list(documents)]
    if len(ids) != len(set(ids)):
        raise ValueError("A state cannot repeat an immutable source ID")
    started = time.process_time()
    known = set()
    for document in prefix:
        known.update(codec_plan(document, "zlib")["nodes"])
    prefix_cpu = time.process_time() - started
    plans, preparation = [], []
    for document in documents:
        choices, measured = {}, {}
        for codec in CODECS:
            before = time.process_time()
            choices[codec] = codec_plan(document, codec)
            measured[codec] = time.process_time() - before
        plans.append(choices)
        preparation.append(measured)
    encoding_cpu = time.process_time() - started
    started = time.process_time()

    def marginal(plan, seen):
        return plan["source_record_bytes"] + sum(cost for key, cost in plan["node_costs"].items() if key not in seen)

    immediate = {codec: marginal(plans[0][codec], known) for codec in CODECS}
    conditional = {codec: None for codec in CODECS}
    best, best_path = None, None
    paths = 0
    for sequence in itertools.product(CODECS, repeat=len(documents)):
        seen, cost = set(known), 0
        for choices, codec in zip(plans, sequence):
            plan = choices[codec]
            cost += marginal(plan, seen)
            seen.update(plan["nodes"])
        paths += 1
        if conditional[sequence[0]] is None or cost < conditional[sequence[0]]:
            conditional[sequence[0]] = cost
        if best is None or cost < best:
            best, best_path = cost, sequence
    return {"codec": best_path[0], "sequence": list(best_path), "cost": best,
            "immediate_costs": immediate, "horizon_costs": conditional,
            "greedy_codec": min(CODECS, key=lambda codec: immediate[codec]),
            "evaluated_paths": paths, "horizon": len(documents),
            "encoding_cpu_seconds": encoding_cpu, "oracle_cpu_seconds": time.process_time() - started,
            "prefix_preparation_cpu_seconds": prefix_cpu, "candidate_preparation_cpu_seconds": preparation,
            "exact_recovery_checks": len(prefix) + len(documents) * 3,
            "cost_unit": "new node payload + canonical descriptor + canonical source-record bytes",
            "continuation_scope": "minimum frozen-horizon continuation conditional on first action; not policy rollout"}


def structural_features(data, prefix=()):
    """Three source-only integers; IDs, metadata and candidate costs never enter."""
    _bytes(data)
    if type(prefix) not in (list, tuple) or len(prefix) > MAX_PREFIX:
        raise ValueError("At most two prefix byte strings")
    chunks = [data[offset:offset + 4096] for offset in range(0, len(data), 4096)]
    counts = Counter(chunks)
    for previous in prefix:
        _bytes(previous)
        counts.update(previous[offset:offset + 4096] for offset in range(0, len(previous), 4096))
    multiplicity = max((counts[chunk] for chunk in chunks), default=0)
    return (multiplicity, len(data), len(data) - sum(map(len, set(chunks))))


def feature_tokens(data, prefix=(), feature="identity"):
    x = structural_features(data, prefix)
    if feature == "identity":
        values = x + (0,) * 9
    elif feature == "mutation":
        values = a3_features(x)
    elif feature == "random":
        values = tuple(sum(a * b for a, b in zip(row, x)) for row in RANDOM_MATRIX)
    else:
        raise ValueError("Unknown feature arm")
    buckets = [(1 if value > 0 else -1) * abs(value).bit_length() if value else 0 for value in values]
    ids = [VOCABULARY.index(f"B{value}") if -20 <= value <= 20 else VOCABULARY.index("OOR") for value in buckets]
    return {"raw": list(x), "values": list(values), "token_ids": ids, "out_of_range": VOCABULARY.index("OOR") in ids}


def initialize(seed):
    return tf.initialize_model(VOCABULARY, seed=seed, **CONFIG["model_config"])


def make_artifact(model, feature, training_record):
    if feature not in FEATURES or type(training_record) is not dict:
        raise ValueError("Invalid policy feature or training record")
    model = tf.validate_model(model)
    if (model["vocabulary"] != VOCABULARY or model["config"]["epsilon"] != 1e-5
            or any(model["config"][key] != value for key, value in CONFIG["model_config"].items())):
        raise ValueError("Policy architecture or vocabulary differs from frozen configuration")
    return json.loads(cr.packet_bytes({"schema": VERSION, "status": "EXPERIMENTAL_CODEC_POLICY", "authority": "NONE",
            "config": CONFIG, "feature": feature, "model": model, "model_sha256": tf.model_sha256(model),
            "training_record": training_record, "semantic_validation": "NOT_ESTABLISHED",
            "original_six_action_P2": "INCOMPLETE", "P3": "INCOMPLETE"}))


def validate_artifact(artifact):
    if (type(artifact) is not dict or artifact.get("schema") != VERSION
            or artifact.get("status") != "EXPERIMENTAL_CODEC_POLICY"
            or artifact.get("authority") != "NONE"
            or cr.packet_bytes(artifact.get("config")) != cr.packet_bytes(CONFIG)):
        raise ValueError("Invalid policy artifact configuration")
    rebuilt = make_artifact(artifact["model"], artifact["feature"], artifact["training_record"])
    if artifact != rebuilt:
        # JSON converts configuration tuples to lists; compare canonical bytes.
        if cr.packet_bytes(artifact) != cr.packet_bytes(rebuilt):
            raise ValueError("Policy artifact hash or fields differ")
    return rebuilt


def choose(data, prefix, artifact):
    """Choose among three legal exact codecs; no originals are modified or opened."""
    started = time.process_time()
    artifact = validate_artifact(artifact)
    features = feature_tokens(data, prefix, artifact["feature"])
    if features["out_of_range"]:
        codec, guard = "literal", "FEATURE_OUT_OF_RANGE"
    else:
        logits = tf.forward(artifact["model"], features["token_ids"])["logits"][-1]
        index = max(range(3), key=lambda item: logits[ACTION_IDS[item]])
        codec, guard = CODECS[index], None
    return {"codec": codec, "guarded_out": guard is not None, "guard_reason": guard,
            "authority": "NONE", "status": "EXPERIMENTAL_CODEC_POLICY",
            "model_sha256": artifact["model_sha256"], "source_sha256": _sha(data),
            "inference_cpu_seconds": time.process_time() - started}


def store_artifact(store, source_id, artifact):
    artifact = validate_artifact(artifact)
    return store.put(source_id, cr.packet_bytes(artifact), {"kind": "EXPERIMENTAL_CODEC_POLICY"},
                     strategy="zlib", report=False)


def load_artifact(store, source_id):
    return validate_artifact(json.loads(store.get(source_id)))
