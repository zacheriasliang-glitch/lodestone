"""Experimental three-slot routing plumbing, with no semantic/authority claim.

Features always recover all three bounded candidates. Smaller packets therefore
do not demonstrate reduced source I/O. The exact lexical comparator uses the same
features; production callers should continue using that comparator.
"""
from collections import Counter, defaultdict
import hashlib
import itertools
import json
import re
import time

from . import transformer as tf
from .crystal_mutations import a3_features
from .crystal_policy import RANDOM_MATRIX
from .crystals import CrystalStore, packet_bytes


VERSION = "experimental-crystal-router/v1"
FEATURES = ("identity", "mutation", "random")
MAX_BYTES = 32768
TOKEN_PATTERN = r"[a-z][a-z0-9]{3,31}"
VOCABULARY = [f"V{value}" for value in range(-16, 17)] + ["OOR", "slot0", "slot1", "slot2"]
ACTION_IDS = [34, 35, 36]
FAMILIES = {"F-A-KERNEL-SUMMARY": ("train", 64), "F-B-DATES": ("validation", 16),
            "F-C-ATTRIBUTION": ("validation", 16), "F-D-SENSOR-CONDITIONAL": ("test", 16)}
CONFIG = {"schema": VERSION, "features": list(FEATURES), "vocabulary": VOCABULARY,
          "action_ids": ACTION_IDS, "token_pattern": TOKEN_PATTERN,
          "token_rule": "UTF-8 replacement decode, lower(), unanchored regex findall, distinct tokens",
          "max_source_bytes": MAX_BYTES, "max_candidates": 3,
          "random_matrix": RANDOM_MATRIX, "random_seed": 211,
          "model_config": {"width": 8, "heads": 2, "layers": 1, "ff_width": 16, "max_tokens": 12}}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def tokens(data):
    """Frozen ASCII regex; deliberately differs from the store's Unicode index."""
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise ValueError("Routing requires exact bytes of at most 32768 bytes")
    return set(re.findall(TOKEN_PATTERN, data.decode("utf-8", errors="replace").lower()))


def _query_terms(query):
    if type(query) is not str:
        raise ValueError("Routing query must be text")
    terms = tokens(query.encode("utf-8"))
    if len(terms) != 2:
        raise ValueError("Routing query requires exactly two distinct frozen-regex tokens")
    return terms


def overlaps(query, candidate_source_ids, store):
    """Recover exactly three candidates, never search an index or the corpus."""
    terms = _query_terms(query)
    if (type(candidate_source_ids) not in (list, tuple) or len(candidate_source_ids) != 3
            or any(type(value) is not str or not value for value in candidate_source_ids)
            or len(set(candidate_source_ids)) != 3):
        raise ValueError("Require three distinct candidate source IDs")
    wall, cpu = time.monotonic(), time.process_time()
    counts, hashes, decoded = [], [], 0
    for source_id in candidate_source_ids:
        # Check the header before decoding, and the recovered size again below.
        if store._source(source_id)["size"] > MAX_BYTES:
            raise ValueError("Routing candidate exceeds 32768 bytes")
        raw = store.get(source_id)
        counts.append(len(tokens(raw) & terms))
        hashes.append(_sha(raw))
        decoded += len(raw)
    return {"counts": counts, "candidate_sha256": hashes,
            "work": {"decoded_sources": 3, "decoded_bytes": decoded, "index_queries": 0,
                     "method": "exact_candidate_decode", "lexical_mode": store.index_mode,
                     "cpu_seconds": time.process_time() - cpu, "wall_seconds": time.monotonic() - wall}}


def feature_tokens(counts, feature="identity"):
    if (type(counts) not in (list, tuple) or len(counts) != 3
            or any(type(value) is not int or not 0 <= value <= 2 for value in counts)):
        raise ValueError("Require three integer overlaps in 0..2")
    if feature == "identity":
        values = list(counts) + [0] * 9
    elif feature == "mutation":
        values = list(a3_features(counts))
    elif feature == "random":
        values = [sum(a * b for a, b in zip(row, counts)) for row in RANDOM_MATRIX]
    else:
        raise ValueError("Unknown routing feature")
    ids = [value + 16 if -16 <= value <= 16 else 33 for value in values]
    return {"raw": list(counts), "values": values, "token_ids": ids,
            "out_of_range": 33 in ids, "out_of_range_count": ids.count(33)}


def collision_audit():
    """All 27 points, including ties, retained before any fit or corpus labels."""
    rows, encodings, conflicts = [], {feature: defaultdict(set) for feature in FEATURES}, {}
    for counts in itertools.product(range(3), repeat=3):
        maxima = [slot for slot in range(3) if counts[slot] == max(counts)]
        label = maxima[0] if len(maxima) == 1 else None
        arms = {feature: feature_tokens(counts, feature) for feature in FEATURES}
        rows.append({"x": list(counts), "unique_argmax_label": label, "tied_slots": maxima, "features": arms})
        for feature in FEATURES:
            if label is not None:
                encodings[feature][tuple(arms[feature]["token_ids"])].add(label)
    for feature in FEATURES:
        conflicts[feature] = [{"token_ids": list(key), "labels": sorted(labels)}
                              for key, labels in encodings[feature].items() if len(labels) > 1]
    return {"points": rows, "conflicting_unique_argmax_encodings": conflicts,
            "out_of_range_counts": {feature: sum(row["features"][feature]["out_of_range_count"]
                                                for row in rows) for feature in FEATURES},
            "value_ranges": {feature: [min(v for row in rows for v in row["features"][feature]["values"]),
                                        max(v for row in rows for v in row["features"][feature]["values"])]
                             for feature in FEATURES}}


def build_tasks(documents):
    """Deterministic source-directed toy tasks; only TRAIN supplies token DF."""
    if type(documents) is not list or not 1 <= len(documents) <= 2000:
        raise ValueError("Require 1..2000 eligible documents")
    by_id, termsets, df = {}, {}, Counter()
    for document in documents:
        source_id, family = document["source_id"], document["family"]
        if (type(source_id) is not str or not source_id or source_id in by_id
                or family not in FAMILIES or document["split"] != FAMILIES[family][0]):
            raise ValueError("Duplicate identity or invalid frozen family split")
        by_id[source_id] = document
        termsets[source_id] = tokens(document["data"])
        if document["split"] == "train":
            df.update(termsets[source_id])
    tasks, excluded = [], []
    for source_id in sorted(by_id):
        document = by_id[source_id]
        query_terms = sorted(termsets[source_id], key=lambda term: (df[term], term))[:2]
        if len(query_terms) < 2:
            excluded.append({"source_id": source_id, "reason": "FEWER_THAN_TWO_TOKENS"})
            continue
        distractors = [other for other, item in by_id.items()
                       if other != source_id and item["family"] == document["family"]
                       and len(termsets[other] & set(query_terms)) < 2]
        distractors.sort(key=lambda other: (_sha((source_id + ":distractor:" + other).encode()), other))
        if len(distractors) < 2:
            excluded.append({"source_id": source_id, "reason": "FEWER_THAN_TWO_DISTRACTORS"})
            continue
        candidates = [source_id] + distractors[:2]
        candidates.sort(key=lambda other: (_sha((source_id + ":slot:" + other).encode()), other))
        counts = [len(termsets[other] & set(query_terms)) for other in candidates]
        tasks.append({"target_source_id": source_id, "family": document["family"], "split": document["split"],
                      "query": " ".join(query_terms), "candidate_source_ids": candidates,
                      "target_slot": candidates.index(source_id), "overlaps": counts})
    selected = []
    for family, (_, cap) in FAMILIES.items():
        group = sorted((task for task in tasks if task["family"] == family),
                       key=lambda task: (_sha(("route-task-v1:" + task["target_source_id"]).encode()), task["target_source_id"]))
        selected.extend(group[:cap])
        excluded.extend({"source_id": task["target_source_id"], "reason": "FROZEN_FAMILY_CAP"} for task in group[cap:])
    coverage = {}
    for split in ("train", "validation", "test"):
        points = Counter(tuple(task["overlaps"]) for task in selected if task["split"] == split)
        coverage[split] = {"unique_points": len(points), "of_possible": 27,
                           "points": [{"x": list(point), "count": count} for point, count in sorted(points.items())]}
    return {"tasks": selected, "exclusions": excluded, "feature_coverage_by_split": coverage,
            "training_document_frequency": dict(sorted(df.items())),
            "natural_class_counts": {family: dict(sorted(Counter(task["target_slot"] for task in selected
                                                                  if task["family"] == family).items()))
                                     for family in FAMILIES}}


def initialize(seed=7):
    return tf.initialize_model(VOCABULARY, seed=seed, **CONFIG["model_config"])


def make_artifact(model, feature, training_record):
    if feature not in FEATURES or type(training_record) is not dict:
        raise ValueError("Invalid router feature or training record")
    model = tf.validate_model(model)
    if (model["vocabulary"] != VOCABULARY or model["config"]["epsilon"] != 1e-5
            or any(model["config"][key] != value for key, value in CONFIG["model_config"].items())):
        raise ValueError("Router architecture or vocabulary differs")
    return json.loads(packet_bytes({"schema": VERSION, "status": "EXPERIMENTAL_SOURCE_ROUTER",
        "result_type": "INTEGRATION_SMOKE_TEST", "authority": "NONE", "config": CONFIG,
        "feature": feature, "model": model, "model_sha256": tf.model_sha256(model),
        "training_record": training_record, "semantic_validation": "NOT_ESTABLISHED",
        "original_six_action_P2": "INCOMPLETE", "P3": "INCOMPLETE"}))


def validate_artifact(artifact):
    if type(artifact) is not dict:
        raise ValueError("Router artifact must be a dictionary")
    try:
        rebuilt = make_artifact(artifact["model"], artifact["feature"], artifact["training_record"])
    except KeyError as exc:
        raise ValueError("Incomplete router artifact") from exc
    if packet_bytes(rebuilt) != packet_bytes(artifact):
        raise ValueError("Router artifact configuration, hash or fields differ")
    return rebuilt


def choose(query, candidate_source_ids, store, artifact):
    """Report raw model slot separately from the declared tie/OOR fallback."""
    started_wall, started_cpu = time.monotonic(), time.process_time()
    artifact = validate_artifact(artifact)
    prepared = overlaps(query, candidate_source_ids, store)
    features = feature_tokens(prepared["counts"], artifact["feature"])
    logits = tf.forward(artifact["model"], features["token_ids"])["logits"][-1]
    scores = [logits[action] for action in ACTION_IDS]
    tied = [slot for slot, score in enumerate(scores) if score == max(scores)]
    raw_slot = tied[0]
    reason = "FEATURE_OUT_OF_RANGE" if features["out_of_range"] else "MODEL_TIE" if len(tied) > 1 else None
    lexical = [slot for slot, score in enumerate(prepared["counts"]) if score == max(prepared["counts"])]
    if reason and len(lexical) != 1:
        raise ValueError("Router fallback is ambiguous: lexical tie; no source selected")
    selected = lexical[0] if reason else raw_slot
    return {"raw_slot": raw_slot, "raw_tied_slots": tied, "action_logits": scores,
            "selected_slot": selected, "selected_source_id": candidate_source_ids[selected],
            "fallback_reason": reason, "fallback_used": reason is not None,
            "features": features, "candidate_sha256": prepared["candidate_sha256"], "work": prepared["work"],
            "inference_cpu_seconds": time.process_time() - started_cpu - prepared["work"]["cpu_seconds"],
            "total_wall_seconds": time.monotonic() - started_wall,
            "model_sha256": artifact["model_sha256"], "authority": "NONE",
            "status": "EXPERIMENTAL_SOURCE_ROUTER"}


def store_artifact(store, source_id, artifact):
    return store.put(source_id, packet_bytes(validate_artifact(artifact)),
                     {"kind": "EXPERIMENTAL_SOURCE_ROUTER"}, strategy="zlib", report=False)


def load_artifact(store, source_id):
    return validate_artifact(json.loads(store.get(source_id)))


def packet_for_source(query, source_id, store, *, required_source_ids=(), max_bytes=4096):
    """Explicit one-source view; never invoke the underlying store's search.

    max_bytes covers canonical JSON only. CLI callers must reserve their newline.
    Required UTF-8 sources pass through in full or the core builder raises.
    """
    _query_terms(query)

    class SelectedView:
        index_mode = store.index_mode
        get = staticmethod(store.get)
        _source = staticmethod(store._source)

        def _search(self, unused_query, unused_limit):
            record = store._source(source_id)
            return [{**store._reference(record), "matched_terms": 0, "score": 0,
                     "integrity": "SOURCE_RECORD_CHECKED_GRAPH_UNCHECKED"}], 1

    return CrystalStore.packet(SelectedView(), query, max_bytes=max_bytes, limit=1,
                               required_source_ids=required_source_ids)
