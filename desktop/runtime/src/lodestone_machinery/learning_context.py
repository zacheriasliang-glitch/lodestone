"""Carry native Bezoar practice state into actual readers without changing exams.

Complete retained teaching/history and literal caller context remain visible,
except where the adapter path's declared fold lets fields go (FOLD_RULE) or,
past that, lets whole older runs leave the view (DEPARTURE_RULE); both are named
in the projection. A deterministic reader projection explicitly keeps original
evidence bodies in Bezoar. This module performs no model inference and does not turn a candidate
correction into demonstrated learning.
"""
from copy import deepcopy
from collections import Counter
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from .records import HANDLE, canonical

POLICY_SCHEMA = "origami-learning-policy/v1"
PRACTICE_SCHEMA = "loom-practice-context/v1"
EXPOSURE = "EXPOSED_PRACTICE"
MAX_PRACTICE_BYTES = 65536
POLICY_FILE = "learning-policy.json"
UNATTRIBUTED_READER = "UNATTRIBUTED_ORIGAMI_PROVIDER"
READER_CONTEXT_SCHEMA = "bezoar-learning-reader-context/v1"
INTERNED_CONTEXT_SCHEMA = "bezoar-learning-reader-interned/v1"
COMPACT_CONTEXT_SCHEMA = "bezoar-learning-reader-compact/v1"
COMPACT_THRESHOLD_BYTES = 24576
HISTORY_ENCODING = ("Only content.history is encoded. An object containing only an object_columns key "
                    "holds values matching its listed columns in order. Whole string values in string_aliases "
                    "expand once to their exact listed text; other values are unchanged. Dictionary values are "
                    "literal. Expand this layer before native handle aliases. No history or prose is omitted or summarized.")
HANDLE_ENCODING = "Exact alias string values refer to handle_aliases; other strings and all prose are unchanged."
PRACTICE_USE = "Candidate retained correction and pattern for this practice; not an answer key, factual source citation, or established learning."
PROSE_FIELDS = {"correction", "conditions", "frame_limits", "pattern", "author", "attribution",
                "encounter", "purpose", "reader", "reason", "response", "observation", "use"}
EXACT_METHOD = "EXACT_TEACHING_AND_HISTORY_FIELDS"
FOLDED_METHOD = "EXACT_TEACHING_FOLDED_HISTORY"
FOLDED_HISTORY_FIELDS = ("encounter", "purpose", "outcome")
KEPT_REVIEW_FIELDS = ("reviewer", "verdict", "correction")
# The installed Loom route reads at most 8,192 tokens with 512 reserved for output. On real Loom
# history the Qwen3 tokenizer measured about 3 bytes per token (23,920 bytes -> 7,955 tokens), so an
# adapter reference above this target folds even when it is under the 32,768-byte schema bound.
# 19 Nov 2026: with ten prior runs the fully folded practice reference was still 22,308 bytes (its learning
# projection alone 22,043). It fit the schema bound, so it was sent, and the reader refused it at its token
# bound: 7,992 prompt tokens + 512 > 8,192 (Loom run 3974ab4a). Departure (below) continues past folding.
ADAPTER_FOLD_TARGET_BYTES = 16384
# FOLD_RULE travels in every folded packet and is pinned to its 18 Nov bytes, so packets built before
# departure existed rebuild identically. Departure carries its own rule, only when a run departs.
FOLD_RULE = ("Only when the complete reader input exceeds its target: first the oldest prior runs' encounter, "
             "purpose and outcome, then (oldest first) each review's fields other than reviewer, verdict and "
             "correction are let go from this view, one step at a time, until the input fits. What remains is "
             "exact: seeds, NACREs, pairs, every verdict and correction, and all newer runs. 'folded' names what "
             "was let go. The native records keep all of it, and Conscience admission recomputes this fold from them.")
DEPARTED_METHOD = "EXACT_TEACHING_DEPARTED_HISTORY"
DEPARTURE_RULE = ("This supersedes the fold rule's 'every verdict and correction': when every prior run and review is "
                  "already folded and the input still exceeds its target, whole prior runs leave this view, oldest "
                  "first, one per step, and the newest prior run never leaves. 'departed_history_runs' names each by "
                  "its run handle. Their verdicts and corrections are not in this input; the native records keep "
                  "them complete. Seeds, NACREs and pairs never leave. A correction meant to outlast its run belongs "
                  "in a SEED. If departing every run but the newest still misses the target, nothing departs and the "
                  "fully folded input is used as before.")
HISTORY_WINDOW_SCHEMA = "loom-reader-history-window/v1"
HISTORY_WINDOW_RULE = ("Explicit reader history window: fold every prior run's encounter, purpose and outcome, "
                       "and every review's fields other than reviewer, verdict and correction; then keep only "
                       "the declared number of newest prior runs. Older runs are named in departed_history_runs. "
                       "All retained review corrections, seeds, NACREs and pairs stay exact. Complete native "
                       "preparations, outcomes and reviews remain in Bezoar. This selection does not depend on "
                       "input length; if the selected view exceeds its existing bound, refuse without further departure.")


def history_window_policy(keep_latest_runs):
    """Validate an explicit per-call window; None preserves the existing rule."""
    if keep_latest_runs is None:
        return None
    if type(keep_latest_runs) is not int or not 1 <= keep_latest_runs <= 32:
        raise ValueError("Reader history window must be an integer from 1 to 32")
    return {"schema": HISTORY_WINDOW_SCHEMA, "keep_latest_runs": keep_latest_runs}


def _checked_history_window_policy(policy):
    if (type(policy) is not dict or set(policy) != {"schema", "keep_latest_runs"}
            or policy.get("schema") != HISTORY_WINDOW_SCHEMA):
        raise ValueError("Unsupported reader history window policy")
    expected = history_window_policy(policy["keep_latest_runs"])
    if expected is None or canonical(policy) != canonical(expected):
        raise ValueError("Invalid reader history window policy")
    return expected


def _memory(value):
    from .bezoar_learning import LearningMemory
    return value if isinstance(value, LearningMemory) else LearningMemory(value)


def _text(value, name, maximum=32768):
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(name + " must be bounded nonempty text")
    return value


def read_learning_policy(workspace):
    """Read an explicitly installed local policy; absence preserves old workflows."""
    path = Path(workspace) / POLICY_FILE
    if path.is_symlink():
        raise ValueError("Learning policy must not be a symlink")
    if not path.exists():
        return None
    raw = path.read_bytes()
    if len(raw) > 32768:
        raise ValueError("Learning policy exceeds its byte bound")
    policy = json.loads(raw)
    if (type(policy) is not dict or set(policy) != {
            "schema", "required", "memory_workspace", "purpose", "seed_handles"}
            or policy["schema"] != POLICY_SCHEMA or policy["required"] is not True):
        raise ValueError("Invalid required learning policy")
    _text(policy["purpose"], "Learning purpose", 4096)
    memory_path = Path(_text(policy["memory_workspace"], "Learning memory path", 4096))
    if not memory_path.is_absolute() or memory_path.resolve(strict=True) != memory_path:
        raise ValueError("Learning memory workspace must be an existing resolved absolute path")
    seeds = policy["seed_handles"]
    if (type(seeds) is not list or not seeds or len(seeds) > 16
            or any(type(h) is not str for h in seeds) or len(set(seeds)) != len(seeds)):
        raise ValueError("Learning policy requires 1..16 distinct candidate seeds")
    return policy


def validate_learning_context(memory, prepared, *, encounter=None, purpose=None, reader=None):
    """Reopen the native run and its full dependencies, not just a context hash."""
    checked = _memory(memory).validate_prepared(prepared)
    if checked != prepared:
        raise ValueError("Learning context differs from its retained preparation")
    for key, expected in (("encounter", encounter), ("purpose", purpose), ("reader", reader)):
        if expected is not None and checked["binding"][key] != expected:
            raise ValueError("Learning context belongs to another " + key)
    return deepcopy(checked)


def _fold_fields(value, folded_fields):
    """Keep what remains; name, without tracing, the fields let go from this view."""
    kept = {key: deepcopy(item) for key, item in value.items() if key not in folded_fields}
    kept["folded"] = sorted(field for field in folded_fields if field in value)
    return kept


def _fold_history_row(row):
    """Let go of one prior run's encounter, purpose and outcome; its handles, status, reader and reviews remain."""
    return _fold_fields(row, FOLDED_HISTORY_FIELDS)


def _fold_review(entry):
    """Keep a review record and its reviewer, verdict and correction; let the rest of its body go.

    A history review is ``{"handle", "review": {schema, attribution, authority, outcome_handle,
    "review": body}}``; only the body's other fields (checks, reason, evidence handles, response
    hash) fold.
    """
    record = entry.get("review") if type(entry) is dict else None
    body = record.get("review") if type(record) is dict else None
    if type(body) is not dict:
        raise ValueError("Unexpected history review structure; refusing to fold it")
    folded = tuple(sorted(key for key in body if key not in KEPT_REVIEW_FIELDS))
    return {**{key: deepcopy(value) for key, value in entry.items() if key != "review"},
            "review": {**{key: deepcopy(value) for key, value in record.items() if key != "review"},
                       "review": _fold_fields(body, folded)}}


def _folding_steps(history):
    """Steps that fold: one per prior run body, then one per review (oldest run first). The 18 Nov fold."""
    return len(history) + sum(len(row.get("reviews", [])) for row in history)


def _fold_steps(history):
    """All steps: folding, then one per whole prior run leaving the view (oldest first), never the newest."""
    return _folding_steps(history) + max(len(history) - 1, 0)


def _reader_projection(memory, prepared, folded=0):
    """Copy complete teaching/history; explicitly keep original evidence in Bezoar.

    This is a deterministic projection, not a summary or a learned compression.
    Every correction, condition, frame limit, pattern, applicability pair, actual
    prior response and review correction remains exact. Only original evidence
    bodies are excluded from this reader view; native preparation retains them.
    With ``folded`` > 0, fold steps let go of the oldest prior runs' encounter,
    purpose and outcome, then of older review details, then of whole oldest prior
    runs, each named by its run handle; what remains stays exact (see FOLD_RULE).
    Zero gives the exact projection.
    """
    checked = validate_learning_context(memory, prepared)
    snapshot = json.loads(checked["context"])
    if snapshot["schema"] == "bezoar-learning-context/v2":
        raise ValueError("Reference history requires an explicit reader window; inspect complete rows with history_page")
    return _reader_projection_from_snapshot(checked, snapshot, folded)


def _reader_projection_from_snapshot(checked, snapshot, folded):
    """Project one detached, validated preparation without reopening its records.

    Only the current synchronous builder shares this snapshot across fold
    candidates. Public calls and dispatch admission still validate independently;
    no checked record or current-history judgment survives the operation.
    """
    content = {key: deepcopy(value) for key, value in snapshot.items()
               if key not in ("schema", "evidence")}
    history = content["history"]
    if type(folded) is not int or not 0 <= folded <= _fold_steps(history):
        raise ValueError("Invalid count of fold steps")
    bodies = min(folded, len(history))
    reviews_left = min(folded - bodies, sum(len(row.get("reviews", [])) for row in history))
    departed = folded - bodies - reviews_left
    departed_reviews = {entry["handle"] for row in history[:departed] for entry in row.get("reviews", [])}
    folded_reviews = []
    rows = []
    for index, row in enumerate(history):
        row = _fold_history_row(row) if index < bodies else row
        if reviews_left and row.get("reviews"):
            reviews = []
            for entry in row["reviews"]:
                if reviews_left:
                    reviews.append(_fold_review(entry))
                    folded_reviews.append(entry["handle"])
                    reviews_left -= 1
                else:
                    reviews.append(entry)
            row = {**row, "reviews": reviews}
        rows.append(row)
    content["history"] = rows[departed:]
    evidence_index = [{"handle": row["handle"], "attribution": row["record"]["attribution"]}
                      for row in snapshot["evidence"]]
    projection = {"method": DEPARTED_METHOD if departed else FOLDED_METHOD if folded else EXACT_METHOD,
                  "omitted_evidence_bodies": [row["handle"] for row in snapshot["evidence"]],
                  "omitted_fields": ["evidence[].record.text", "evidence[].record.source",
                                     "evidence[].record.event_id", "evidence[].record.observed_at"],
                  "original_retention": "Complete original evidence and native preparation remain in Bezoar for independent review; this reader view is not the complete evidence archive.",
                  "teaching_or_history_truncated": False}
    if folded:
        projection["folded_history_runs"] = [row["run_handle"] for row in rows[departed:bodies]]
        projection["folded_reviews"] = [handle for handle in folded_reviews if handle not in departed_reviews]
        if departed:
            # Out of this view, never out of memory: each departed run stays complete in Bezoar.
            projection["departed_history_runs"] = [row["run_handle"] for row in history[:departed]]
            projection["departure_rule"] = DEPARTURE_RULE
        projection["fold_rule"] = FOLD_RULE
    return {"schema": READER_CONTEXT_SCHEMA, "run_id": checked["run_id"],
            "run_handle": checked["run_handle"], "binding": deepcopy(checked["binding"]),
            "content": content, "evidence_index": evidence_index,
            "projection": projection, "authority": "NONE"}


def history_window_projection(checked, snapshot, keep_latest_runs):
    """Pure projection of a detached snapshot, not native preparation evidence.

    Native builders independently validate the preparation before invoking this
    helper. Offline callers may estimate selection using a clearly labelled
    snapshot; only actual admitted packets establish provider exposure.
    """
    policy = history_window_policy(keep_latest_runs)
    if policy is None:
        raise ValueError("Explicit history projection requires a window")
    history = snapshot["history"]
    departed = max(len(history) - keep_latest_runs, 0)
    projected = _reader_projection_from_snapshot(
        checked, snapshot, _folding_steps(history) + departed)
    metadata = projected["projection"]
    metadata["method"] = DEPARTED_METHOD if departed else FOLDED_METHOD
    metadata["folded_history_runs"] = [row["run_handle"] for row in history[departed:]]
    metadata.setdefault("folded_reviews", [])
    metadata["reader_history_window"] = policy
    metadata["fold_rule"] = HISTORY_WINDOW_RULE
    if departed:
        metadata["departure_rule"] = HISTORY_WINDOW_RULE
    return projected



def _reference_history_window_projection(memory, checked, snapshot, keep_latest_runs):
    """Resolve only a declared window after every frozen source was validated.

    This avoids building a fresh aggregate of all historical response/review
    bodies. Native operation caches can still retain validated ancestry; this is
    not a constant-memory claim. Original immutable records remain complete.
    """
    policy = history_window_policy(keep_latest_runs)
    if policy is None or snapshot["schema"] != "bezoar-learning-context/v2":
        raise ValueError("Versioned history references require an explicit reader window")
    from .history_inventory import checked_inventory
    inventory = checked_inventory(snapshot["history"])
    entries = inventory["rows"]
    departed = max(len(entries) - keep_latest_runs, 0)
    projected = _reader_projection_from_snapshot(checked, {**snapshot, "history": []}, 0)
    rows, folded_reviews = [], []
    for item in entries[departed:]:
        prior = memory.read_prepared(item["run_handle"])
        row = {"run_handle": item["run_handle"], "run_id": prior["run_id"],
               "reader": prior["binding"]["reader"],
               "folded": sorted(FOLDED_HISTORY_FIELDS)}
        if item["outcome_handle"] is None:
            row.update(status="INCOMPLETE", reviews=[])
        else:
            outcome = memory.read_outcome(item["outcome_handle"])
            row.update(status=outcome["status"], outcome_handle=item["outcome_handle"],
                reviews=[_fold_review({"handle": h, "review": memory.read_review(h)})
                         for h in item["review_handles"]],
                lessons=[{"handle": h, "record": memory.read_seed(h)} for h in item["lesson_handles"]])
            folded_reviews.extend(item["review_handles"])
        rows.append(row)
    projected["content"]["history"] = rows
    meta = projected["projection"]
    meta.update(method=DEPARTED_METHOD if departed else FOLDED_METHOD,
                folded_history_runs=[row["run_handle"] for row in rows],
                folded_reviews=folded_reviews, reader_history_window=policy,
                fold_rule=HISTORY_WINDOW_RULE,
                native_history_representation=inventory["schema"],
                native_history_inventory_sha256=inventory["inventory_sha256"],
                native_history_reference_count=inventory["reference_count"])
    if departed:
        meta.update(departed_history_runs=[row["run_handle"] for row in entries[:departed]],
                    departure_rule=HISTORY_WINDOW_RULE)
    return projected

def _map_values(value, replace):
    if type(value) is dict:
        return {key: _map_values(item, replace) for key, item in value.items()}
    if type(value) is list:
        return [_map_values(item, replace) for item in value]
    return replace(value) if type(value) is str else value


def _map_handle_values(value, replace):
    if type(value) is dict:
        return {key: deepcopy(item) if key in PROSE_FIELDS else _map_handle_values(item, replace)
                for key, item in value.items()}
    if type(value) is list:
        return [_map_handle_values(item, replace) for item in value]
    return replace(value) if type(value) is str else value


def _intern_learning_projection(projection):
    strings = set()
    _map_values(projection, lambda value: strings.add(value) or value)
    identifiers = set()
    _map_handle_values(projection, lambda value: identifiers.add(value) or value)
    handles = sorted(value for value in identifiers if HANDLE.fullmatch(value))
    prefix = "H"
    # Do not reinterpret a literal piece of teaching that happens to say H1.
    while any(prefix + str(index + 1) in strings for index in range(len(handles))):
        prefix += "H"
    table = {prefix + str(index + 1): handle for index, handle in enumerate(handles)}
    by_handle = {handle: alias for alias, handle in table.items()}
    encoded = _map_handle_values(projection, lambda value: by_handle.get(value, value))
    return {**encoded, "schema": INTERNED_CONTEXT_SCHEMA,
            "handle_aliases": table, "handle_encoding": HANDLE_ENCODING}


def _history_codec(history):
    """Represent repeated object columns and exact strings once, visibly."""
    shapes, strings, literals = Counter(), Counter(), set()
    budget = [16384]

    def scan(value, depth=0):
        budget[0] -= 1
        if depth > 24 or budget[0] < 0:
            raise ValueError("Learning history exceeds structural encoding bound")
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("Learning history object keys must be strings")
            shapes[tuple(sorted(value))] += 1
            literals.update(value)
            for item in value.values():
                scan(item, depth + 1)
        elif type(value) is list:
            for item in value:
                scan(item, depth + 1)
        elif type(value) is str:
            strings[value] += 1
            literals.add(value)

    scan(history)

    def prefix(label, count):
        while any(label + str(index + 1) in literals for index in range(count)):
            label += "_"
        return label

    text_prefix, object_prefix = prefix("S", len(strings)), prefix("O", len(shapes))
    texts, columns = {}, {}
    for text, count in sorted(strings.items()):
        alias = text_prefix + str(len(texts) + 1)
        saving = count * (len(canonical(text)) - len(canonical(alias)))
        saving -= len(canonical(alias)) + len(canonical(text)) + 2
        if count >= 2 and saving > 0:
            texts[alias] = text
    for keys, count in sorted(shapes.items()):
        alias = object_prefix + str(len(columns) + 1)
        saving = count * (sum(len(canonical(key)) + 1 for key in keys) - len(canonical(alias)) - 3)
        saving -= len(canonical(alias)) + len(canonical(keys)) + 2
        if count >= 2 and saving > 0:
            columns[alias] = list(keys)
    text_alias = {text: alias for alias, text in texts.items()}
    object_alias = {tuple(keys): alias for alias, keys in columns.items()}

    def encode(value):
        if type(value) is dict:
            keys = tuple(sorted(value))
            alias = object_alias.get(keys)
            return ({alias: [encode(value[key]) for key in keys]} if alias else
                    {key: encode(item) for key, item in value.items()})
        if type(value) is list:
            return [encode(item) for item in value]
        return text_alias.get(value, value) if type(value) is str else value

    return encode(history), {"object_columns": columns, "string_aliases": texts,
        "history_sha256": hashlib.sha256(canonical(history)).hexdigest(), "legend": HISTORY_ENCODING}


def _compact_learning_projection(projected):
    """Use readable history transport only when large and actually smaller."""
    before = canonical(projected)
    if len(before) <= COMPACT_THRESHOLD_BYTES:
        return projected
    history = projected["content"]["history"]
    if type(history) is not list:
        raise ValueError("Learning history must be a complete list")
    # This is an optional transport optimisation. A larger native history must
    # still reach the existing, explicit adapter fold/departure selection rule;
    # it is not an invalid preparation merely because this codec cannot hold it.
    if len(canonical(history)) > MAX_PRACTICE_BYTES:
        return projected
    try:
        rows, encoding = _history_codec(history)
    except ValueError:
        return projected
    result = deepcopy(projected)
    result["schema"] = COMPACT_CONTEXT_SCHEMA
    result["content"]["history"] = rows
    result["history_encoding"] = encoding
    return result if len(canonical(result)) < len(before) else projected


def _expand_compact_history(projected):
    """Bound decoding, verify exact source bytes and reject ambiguous encodings."""
    encoding = projected.get("history_encoding")
    if (type(encoding) is not dict or set(encoding) != {
            "object_columns", "string_aliases", "history_sha256", "legend"}
            or encoding["legend"] != HISTORY_ENCODING):
        raise ValueError("Invalid compact learning history legend")
    columns, texts = encoding["object_columns"], encoding["string_aliases"]
    if (type(columns) is not dict or type(texts) is not dict
            or len(columns) > 2048 or len(texts) > 2048
            or any(type(alias) is not str or not alias for alias in list(columns) + list(texts))
            or set(columns) & set(texts)
            or any(type(keys) is not list or not keys or len(keys) > 256
                   or any(type(key) is not str for key in keys)
                   or keys != sorted(set(keys)) for keys in columns.values())
            or any(type(text) is not str for text in texts.values())):
        raise ValueError("Invalid compact learning history dictionaries")
    # Charge original scalar/key bytes while expanding. Repeated references
    # cannot create an unbounded history before the final canonical hash check.
    budget, nodes = [MAX_PRACTICE_BYTES], [16384]

    def charge(value):
        budget[0] -= len(canonical(value))
        if budget[0] < 0:
            raise ValueError("Learning history exceeds its complete expansion bound")
        return value

    def decode(value, depth=0):
        nodes[0] -= 1
        if depth > 24 or nodes[0] < 0:
            raise ValueError("Learning history exceeds structural decoding bound")
        if type(value) is dict:
            row_keys = set(value) & set(columns)
            if row_keys:
                if len(value) != 1:
                    raise ValueError("Compact history row has ambiguous extra keys")
                alias = next(iter(value))
                items = value[alias]
                if type(items) is not list or len(items) != len(columns[alias]):
                    raise ValueError("Compact history row differs from its columns")
                return {charge(key): decode(item, depth + 1)
                        for key, item in zip(columns[alias], items)}
            return {charge(key): decode(item, depth + 1) for key, item in value.items()}
        if type(value) is list:
            return [decode(item, depth + 1) for item in value]
        return charge(texts.get(value, value) if type(value) is str else value)

    body = {key: deepcopy(value) for key, value in projected.items() if key != "history_encoding"}
    if type(body.get("content")) is not dict or type(body["content"].get("history")) is not list:
        raise ValueError("Compact projection requires a complete history list")
    history = decode(body["content"]["history"])
    raw = canonical(history)
    if len(raw) > MAX_PRACTICE_BYTES or hashlib.sha256(raw).hexdigest() != encoding["history_sha256"]:
        raise ValueError("Compact history differs from its exact source digest")
    body["content"]["history"] = history
    body["schema"] = INTERNED_CONTEXT_SCHEMA
    if canonical(_compact_learning_projection(body)) != canonical(projected):
        raise ValueError("Learning history encoding is not exact and canonical")
    return body


def project_learning_context(memory, prepared, folded=0):
    """Return the exact reader projection with readable, reversible interning.

    The complete literal fold is outside this operation. Selected teaching stays
    ordinary JSON. Large histories may share exact object columns and repeated
    whole strings through explicit dictionaries; no prose is shortened.
    """
    return _compact_learning_projection(_intern_learning_projection(_reader_projection(memory, prepared, folded)))


def expand_learning_projection(projected):
    """Reverse supported history/handle transports to the exact reader projection."""
    if type(projected) is dict and projected.get("schema") == COMPACT_CONTEXT_SCHEMA:
        projected = _expand_compact_history(projected)
    if (type(projected) is not dict or projected.get("schema") != INTERNED_CONTEXT_SCHEMA
            or projected.get("handle_encoding") != HANDLE_ENCODING):
        raise ValueError("Unsupported interned learning projection")
    table = projected.get("handle_aliases")
    if (type(table) is not dict or len(table) > 2048
            or any(type(alias) is not str or type(handle) is not str or not HANDLE.fullmatch(handle)
                   for alias, handle in table.items())
            or len(set(table.values())) != len(table)):
        raise ValueError("Invalid native handle alias table")
    body = {key: value for key, value in projected.items() if key not in ("handle_aliases", "handle_encoding")}
    expanded = _map_values(body, lambda value: table.get(value, value))
    expanded["schema"] = READER_CONTEXT_SCHEMA
    if canonical(_intern_learning_projection(expanded)) != canonical(projected):
        raise ValueError("Learning handle encoding is not exact and canonical")
    return expanded


def practice_data_candidates(request):
    """Render only the versioned practice input without double-escaping its fold.

    Native evidence validation occurs in the builder and Conscience admission.
    This pure renderer checks the complete declared structure and uses the same
    literal sections for prediction and completion-only training.
    """
    try:
        value = json.loads(request["context"])
    except (ValueError, TypeError):
        return None
    if type(value) is not dict or value.get("schema") != PRACTICE_SCHEMA:
        return None
    fields = {"schema", "exposure", "literal_context", "learning_context", "use", "authority"}
    voxel_sections = None
    if "voxel" in value:
        from .loom_voxels import render_voxel_sections
        fields.add("voxel")
        voxel_sections = render_voxel_sections(value["voxel"], request["purpose"])
        if voxel_sections is None:
            raise ValueError("Practice voxel must be a canonical voxel context")
    window_policy = None
    if "reader_history_window" in value:
        fields.add("reader_history_window")
        window_policy = _checked_history_window_policy(value["reader_history_window"])
    if (set(value) != fields
            or canonical(value).decode() != request["context"] or value["exposure"] != EXPOSURE
            or value["authority"] != "NONE" or value["use"] != PRACTICE_USE
            or type(value["literal_context"]) is not str):
        raise ValueError("Invalid complete practice input for rendering")
    projected = value["learning_context"]
    expanded = expand_learning_projection(projected)
    if (type(expanded.get("projection")) is not dict
            or expanded["projection"].get("reader_history_window") != window_policy):
        raise ValueError("Rendered history window differs from its declared projection")
    if (set(expanded) != {"schema", "run_id", "run_handle", "binding", "content", "evidence_index", "projection", "authority"}
            or expanded["authority"] != "NONE" or type(expanded["run_handle"]) is not str
            or not HANDLE.fullmatch(expanded["run_handle"])):
        raise ValueError("Invalid complete teaching projection for rendering")
    binding, content = expanded["binding"], expanded["content"]
    if (type(binding) is not dict or set(binding) != {"encounter", "purpose", "reader", "seed_handles", "nacre_handles", "context_sha256"}
            or binding["encounter"] != request["encounter"] or binding["purpose"] != request["purpose"]
            or type(content) is not dict or set(content) != {"encounter", "purpose", "pairs", "seeds", "nacres", "history", "authority", "use"}
            or content["encounter"] != request["encounter"] or content["purpose"] != request["purpose"]
            or content["authority"] != "NONE" or type(content["history"]) is not list
            or any(type(content[field]) is not list or not content[field] for field in ("seeds", "nacres", "pairs"))
            or type(expanded["evidence_index"]) is not list or not expanded["evidence_index"]
            or type(expanded["projection"]) is not dict
            or expanded["projection"].get("teaching_or_history_truncated") is not False):
        raise ValueError("Practice rendering requires complete bound teaching and history")
    for key in ("seeds", "nacres"):
        for row in content[key]:
            if (type(row) is not dict or set(row) != {"handle", "record"}
                    or type(row["record"]) is not dict
                    or type(row["record"].get("conditions")) is not list or not row["record"]["conditions"]
                    or type(row["record"].get("correction" if key == "seeds" else "pattern")) is not str
                    or (key == "seeds" and (type(row["record"].get("frame_limits")) is not list or not row["record"]["frame_limits"]))):
                raise ValueError("Practice rendering lost usable teaching or pattern conditions")
    views = [projected]
    if projected["schema"] in (COMPACT_CONTEXT_SCHEMA, INTERNED_CONTEXT_SCHEMA):
        from .learning_reader import model_view
        try:
            candidate, _host_identity_table = model_view(projected)
        except ValueError:
            # This optional optimisation may not fit a valid projection.
            # Its transport was checked above; keep all original bytes.
            candidate = projected
        if candidate != projected:
            # Preserve the previous byte-selected representation on token ties.
            # The provider changes its view only for a strict token reduction.
            views = ([candidate, projected] if len(canonical(candidate)) < len(canonical(projected))
                     else [projected, candidate])
    from .learning_reader import section_view
    views += [section_view(view, encounter=request["encounter"], purpose=request["purpose"])
              for view in list(views)]
    candidates = []
    for rendered in views:
        sections = [("LITERAL_CONTEXT", value["literal_context"]),
                    ("LEARNING_CONTEXT_JSON", canonical(rendered).decode()),
                    ("ENCOUNTER", request["encounter"]), ("PURPOSE", request["purpose"])]
        if window_policy is not None:
            sections.insert(1, ("READER_HISTORY_WINDOW_JSON", canonical(window_policy).decode()))
        if voxel_sections is not None:
            sections.insert(2, ("VOXEL_TEACHING", voxel_sections))
        candidates.append(PRACTICE_SCHEMA + " | " + EXPOSURE + " | authority=NONE\n" + PRACTICE_USE + "\n"
            + "\n".join(name + " (UTF-8 bytes=" + str(len(text.encode())) + "):\n" + text
                        for name, text in sections))
    return candidates


def render_practice_data(request):
    """Standalone display without a tokenizer; inference compares full token cost.

    Every candidate preserves the same selected teaching/history. The adapter
    chooses by the actual tokenizer after applying the complete chat template.
    """
    candidates = practice_data_candidates(request)
    return None if candidates is None else min(candidates, key=lambda text: len(text.encode()))


def prepare_origami_learning(workspace, prepared, *, question, reader):
    """Automatically prepare required practice; unresolved patterns are allowed."""
    policy = read_learning_policy(workspace)
    if prepared is None and policy is None:
        return None
    reader = UNATTRIBUTED_READER if reader is None else reader
    memory = _memory(policy["memory_workspace"] if policy else workspace)
    if prepared is None and policy is not None:
        from lodestone_nurarihyon.learning import prepare
        prepared = prepare(memory, policy["seed_handles"], encounter=question,
                           purpose=policy["purpose"], reader=reader)
    if prepared is None:
        return None
    return check_origami_learning(workspace, prepared, question=question, reader=reader)


def check_origami_learning(workspace, prepared, *, question, reader, current_selection=True):
    policy = read_learning_policy(workspace)
    if prepared is None:
        if policy is not None:
            raise ValueError("Configured Origami learning run is missing its Bezoar preparation")
        return None
    reader = UNATTRIBUTED_READER if reader is None else reader
    memory = _memory(policy["memory_workspace"] if policy else workspace)
    checked = validate_learning_context(memory, prepared, encounter=question, reader=reader,
                                        purpose=policy["purpose"] if policy else None)
    if policy is not None and current_selection and checked["binding"]["seed_handles"] != memory.resolve_seeds(policy["seed_handles"]):
        raise ValueError("Learning context does not carry the configured active candidate seeds")
    return checked


def origami_learning_memory(workspace):
    policy = read_learning_policy(workspace)
    return _memory(policy["memory_workspace"] if policy else workspace)


def origami_learning_lock(workspace, prepared):
    if prepared is None and read_learning_policy(workspace) is None:
        return nullcontext()
    from lodestone_nurarihyon.learning import locked
    return locked(origami_learning_memory(workspace))


def admit_origami_learning(workspace, prepared, packet):
    """Require the common Conscience/Nurarihyon admission for actual reader input."""
    from lodestone_nurarihyon.learning import assess_prepared
    return assess_prepared(origami_learning_memory(workspace), prepared, packet)


def check_origami_admission(workspace, prepared, packet, admission):
    memory = origami_learning_memory(workspace)
    if type(admission) is not dict or type(admission.get("assessment_handle")) is not str:
        raise ValueError("Missing retained Origami learning admission")
    record = memory.store.relay("origami", admission["assessment_handle"],
                                owner="nurarihyon", kind="learning-admission")
    payload = record["payload"]
    raw = canonical(packet)
    if (admission != {**payload, "assessment_handle": admission["assessment_handle"]}
            or payload.get("verdict") != "ALLOW" or payload.get("run_handle") != prepared["run_handle"]
            or payload.get("input_sha256") != hashlib.sha256(raw).hexdigest()
            or payload.get("input_bytes") != len(raw)):
        raise ValueError("Origami learning admission does not bind this exact provider input")
    return admission


def begin_origami_learning(workspace, prepared, packet, *, call_limit, run_id):
    from lodestone_nurarihyon.learning import begin_external
    return begin_external(origami_learning_memory(workspace), prepared, packet, call_limit=call_limit,
                          source_workspace=str(Path(workspace).resolve()), source_run_id=run_id)


def complete_origami_learning(workspace, prepared, outcome_handle, dispatch_handle):
    from lodestone_nurarihyon.learning import complete_external
    return complete_external(origami_learning_memory(workspace), prepared, outcome_handle, dispatch_handle)


def check_origami_dispatch(workspace, prepared, dispatch, *, run_id, call_limit):
    """A local sidecar cannot substitute a different run's daemon lifecycle."""
    if type(dispatch) is not dict or set(dispatch) != {"dispatch_handle", "learning_run_handle"}:
        raise ValueError("Invalid retained Origami learning dispatch")
    memory = origami_learning_memory(workspace)
    record = memory.store.relay("origami", dispatch["dispatch_handle"],
                                owner="nurarihyon", kind="learning-external-dispatch")
    payload = record["payload"]
    source = payload["source"]
    intent = Path(workspace) / "origami" / run_id / "intent.json"
    if (dispatch["learning_run_handle"] != prepared["run_handle"]
            or payload["run_handle"] != prepared["run_handle"]
            or payload["provider_call_limit"] != call_limit
            or source["workspace"] != str(Path(workspace).resolve()) or source["run_id"] != run_id
            or source["intent_sha256"] != hashlib.sha256(intent.read_bytes()).hexdigest()):
        raise ValueError("Retained Origami dispatch differs from this learning encounter")
    return dispatch["dispatch_handle"]


def retain_origami_outcome(workspace, prepared, result, *, actual_response):
    """Retain every terminal attempt without inferring semantic learning."""
    memory = origami_learning_memory(workspace)
    status = result["status"]
    classification = ("COMPLETED" if status == "ANSWER" else
                      "ABSTAINED" if status in ("UNRESOLVED", "CONFLICTING") else "FAILED")
    response = actual_response
    handle = memory.finish(prepared, response=response, status=classification,
                           observation={"origami_run_id": result["run_id"], "question": result["question"],
                                        "provider_status": status, "reason": result["reason"]})
    return {"outcome_handle": handle, "learning": "NOT_DEMONSTRATED_BY_TERMINAL_STATUS",
            "response_sha256": hashlib.sha256(response.encode()).hexdigest() if response is not None else None}


def check_origami_outcome(workspace, result):
    """Reopen the actual response-bound outcome when a completed fold is consumed."""
    prepared = result["learning_context"]
    retained = result.get("learning_outcome")
    fields = {"outcome_handle", "learning", "response_sha256"}
    if type(retained) is not dict or not fields <= set(retained) <= fields | {"terminal"}:
        raise ValueError("Origami result lost its retained learning outcome")
    memory = origami_learning_memory(workspace)
    outcome = memory.read_outcome(retained["outcome_handle"])
    if "terminal" in retained:
        terminal = memory.store.relay("origami", retained["terminal"], owner="nurarihyon", kind="learning-terminal")
        if terminal["payload"]["outcome_handle"] != retained["outcome_handle"]:
            raise ValueError("Origami terminal receipt belongs to another learning outcome")
    if (outcome["run_handle"] != prepared["run_handle"]
            or outcome["response"] != canonical(result["fold"]).decode("utf-8")
            or outcome["response_sha256"] != retained["response_sha256"]
            or retained["learning"] != "NOT_DEMONSTRATED_BY_TERMINAL_STATUS"
            or outcome["observation"] != {"origami_run_id": result["run_id"], "question": result["question"],
                                           "provider_status": result["status"], "reason": result["reason"]}):
        raise ValueError("Origami learning outcome differs from its actual response or encounter")
    return outcome


def _learning_fold_payload(organ, run_id, outcome_handle):
    result = organ.status(run_id)
    if result.get("status") != "ANSWER" or "learning_context" not in result:
        raise ValueError("Learning fold promotion requires an actual learning encounter answer")
    prepared = result["learning_context"]
    if (result.get("roles", {}).get("provider") is None
            or prepared["binding"]["reader"] == UNATTRIBUTED_READER):
        raise ValueError("Learning fold promotion requires a declared actual provider; unknown reader independence cannot be inferred")
    checked = check_origami_learning(organ.workspace, prepared, question=result["question"],
                                     reader=result["roles"]["provider"], current_selection=False)
    memory = origami_learning_memory(organ.workspace)
    outcome = check_origami_outcome(organ.workspace, result)
    if (outcome["run_handle"] != checked["run_handle"]
            or outcome["response"] != canonical(result["fold"]).decode("utf-8")
            or result.get("learning_outcome", {}).get("outcome_handle") != outcome_handle):
        raise ValueError("Learning outcome does not bind this exact Origami fold")
    eligibility = memory.fold_eligibility(outcome_handle)
    if eligibility.get("eligible") is not True:
        raise ValueError("Learning fold requires a supported scoped resolution of all selected patterns")
    payload = {"schema": "origami-demonstrated-learning-fold/v1", "run_id": run_id,
               "fold": deepcopy(result["fold"]), "learning_context": checked,
               "outcome": outcome, "eligibility": eligibility,
               "learning_memory_workspace": str(memory.workspace),
               "resolution_reviews": [{"handle": h, "record": memory.read_review(h)}
                                      for h in eligibility["review_handles"]],
               "scope": eligibility["scope"], "authority": "NONE",
               "claim": "Attributed outcome review within its recorded scope, not general reliability."}
    return payload


def promote_origami_learning_fold(organ, run_id, outcome_handle):
    """Publish a scoped learned fold only from this exact outcome and its review."""
    from .records import RecordStore
    payload = _learning_fold_payload(organ, run_id, outcome_handle)
    # Learning and Origami workspaces may differ; native evidence remains in the
    # explicit memory workspace and is reopened above, never copied as authority.
    return RecordStore(organ.workspace).publish("origami", "learning_fold", payload)


def read_origami_learning_fold(organ, handle):
    """Reassess retained promotion against actual later outcomes before reuse."""
    from .records import RecordStore
    store = RecordStore(organ.workspace)
    record = store.relay("consumer", handle, owner="origami", kind="learning_fold")
    payload = record["payload"]
    if payload.get("schema") != "origami-demonstrated-learning-fold/v1":
        raise ValueError("Unknown learned-fold contract")
    # A new review, regression, changed context, or different memory binding
    # prevents stale certification. Readback does not republish or rewrite it.
    reopened = _learning_fold_payload(organ, payload["run_id"], payload["eligibility"]["outcome_handle"])
    if canonical(reopened) != canonical(payload):
        raise ValueError("Retained learned fold needs a fresh scoped review before reuse")
    return deepcopy(payload)


def _practice_reference(memory, prepared, literal_context, *, encounter=None, purpose=None,
                        max_bytes=MAX_PRACTICE_BYTES, fold=False, departures=True, _legacy_transport=False,
                        history_window=None, voxel=None):
    """Return the complete practice reference, exact whenever it fits ``max_bytes``.

    With ``fold`` (the adapter path only) a reference above ``target`` folds
    one step at a time under FOLD_RULE, then departs whole runs under
    DEPARTURE_RULE, and stops at the first that fits. If none reaches
    ``target``, nothing departs: the fully folded reference with every run in
    view is used when it fits ``max_bytes``, as before departure existed.
    ``departures=False`` gives exactly the 18 Nov fold. Builder and validator
    pass the same bounds, so both reach the same bytes. Without ``fold``, or
    above ``max_bytes`` even fully folded, it refuses: nothing is truncated.
    The private legacy transport switch is used only when reconstructing an
    immutable historical packet with the former handle-only encoder.
    """
    if type(literal_context) is not str:
        raise ValueError("Literal context must be a string")
    if voxel is not None:
        from .loom_voxels import render_voxel_sections
        # Preserve the original selection. Optional teaching must not silently
        # push earlier outcomes or corrections out of the reader's history.
        current_purpose = purpose if purpose is not None else prepared["binding"]["purpose"]
        if render_voxel_sections(voxel, current_purpose) is None:
            raise ValueError("Practice voxel must be a canonical voxel context")
        baseline = _practice_reference(
            memory, prepared, literal_context, encounter=encounter, purpose=purpose,
            max_bytes=max_bytes, fold=fold, departures=departures,
            _legacy_transport=_legacy_transport, history_window=history_window)
        value = json.loads(baseline)
        value["voxel"] = voxel
        raw = canonical(value)
        if len(raw) > max_bytes:
            raise ValueError("Practice voxel exceeds the combined context bound; retained history is unchanged")
        return raw.decode("utf-8")
    window_policy = history_window_policy(history_window)
    if window_policy is not None and (not fold or not departures or _legacy_transport):
        raise ValueError("Reader history window requires the current adapter projection")
    checked = validate_learning_context(memory, prepared, encounter=encounter, purpose=purpose)
    snapshot = json.loads(checked["context"])
    referenced = snapshot["schema"] == "bezoar-learning-context/v2"
    if referenced and window_policy is None:
        raise ValueError("Reference history requires an explicit reader window; no automatic departure")
    history = [] if referenced else snapshot["history"]
    folding = _folding_steps(history) if fold else 0
    steps = (_fold_steps(history) if departures else folding) if fold else 0
    target = min(max_bytes, ADAPTER_FOLD_TARGET_BYTES) if fold else max_bytes

    def reference(folded):
        plain = (_reference_history_window_projection(_memory(memory), checked, snapshot, history_window)
                 if referenced else history_window_projection(checked, snapshot, history_window)
                 if window_policy is not None else _reader_projection_from_snapshot(checked, snapshot, folded))
        projected = _intern_learning_projection(plain)
        if not _legacy_transport:
            projected = _compact_learning_projection(projected)
        value = {"schema": PRACTICE_SCHEMA, "exposure": EXPOSURE,
                          "literal_context": literal_context,
                          "learning_context": projected,
                          "use": PRACTICE_USE,
                          "authority": "NONE"}
        if window_policy is not None:
            value["reader_history_window"] = window_policy
        return canonical(value)
    if window_policy is not None:
        raw = reference(0)
        if len(raw) <= max_bytes:
            return raw.decode("utf-8")
        raise ValueError("Declared reader history window exceeds the existing context bound; no further departure")
    for folded in range(steps + 1):
        raw = reference(folded)
        if len(raw) <= target:
            return raw.decode("utf-8")
    if steps != folding:
        raw = reference(folding)  # Departure cannot reach the target: depart nothing, as before.
    if len(raw) <= max_bytes:
        return raw.decode("utf-8")
    if fold:
        raise ValueError("Complete practice context exceeds bound even with every prior run and review folded; "
                         "truncation is forbidden")
    raise ValueError("Complete practice context exceeds bound; truncation is forbidden")


def build_adapter_request(memory, prepared, *, request_id, context, encounter, purpose, history_window=None,
                          voxel=None):
    """Prepare the existing natural-language Loom adapter's exact input contract."""
    from .loom_adapter import MAX_CONTEXT_BYTES, REQUEST_SCHEMA, validate_request
    text = _practice_reference(memory, prepared, context, encounter=encounter, purpose=purpose,
                               max_bytes=MAX_CONTEXT_BYTES, fold=True, history_window=history_window, voxel=voxel)
    return validate_request({"schema": REQUEST_SCHEMA, "id": request_id,
                             "context": text, "context_sha256": hashlib.sha256(text.encode()).hexdigest(),
                             "encounter": encounter, "purpose": purpose})


def build_subscription_packet(memory, prepared, *, task, reference, response_schema):
    """Make an explicit practice payload compatible with the strict reader transport.

    This does not invoke or modify the frozen study serializer. Its reference
    labels the additional learning exposure, so it cannot be a fold-only control.
    """
    _text(task, "Practice task", 16384)
    if type(response_schema) is not dict or not response_schema:
        raise ValueError("Practice response schema must be a nonempty object")
    packet = {"task": task,
              "reference": _practice_reference(memory, prepared, reference, encounter=task),
              "response_schema": deepcopy(response_schema)}
    if len(canonical(packet)) > MAX_PRACTICE_BYTES:
        raise ValueError("Complete practice packet exceeds bound; truncation is forbidden")
    return packet


def validate_practice_packet(memory, prepared, packet, kind, *, historical=False):
    """Check exact selected delivery; historical readers may reverse old transports.

    Fresh requests retain the supported fold/departure rules. An already retained
    packet may use an earlier encoding or complete view only when its bytes equal
    a supported historical reconstruction from the same native preparation.
    """
    if type(historical) is not bool:
        raise ValueError("Historical practice validation requires a boolean")
    checked = validate_learning_context(memory, prepared)
    bound = {}
    if kind == "adapter":
        from .loom_adapter import MAX_CONTEXT_BYTES, validate_request
        packet = validate_request(packet)
        field = "context"
        encounter, purpose = packet["encounter"], packet["purpose"]
        bound = {"max_bytes": MAX_CONTEXT_BYTES, "fold": True}
    elif kind == "subscription":
        if type(packet) is not dict or set(packet) != {"task", "reference", "response_schema"}:
            raise ValueError("Practice reader payload has changed")
        field, encounter, purpose = "reference", packet["task"], None
    else:
        raise ValueError("Unknown practice packet kind")
    reference = json.loads(packet[field])
    if type(reference) is not dict or type(reference.get("literal_context")) is not str:
        raise ValueError("Missing complete literal practice context")
    if "voxel" in reference:
        if kind != "adapter":
            raise ValueError("Voxel teaching is supported only for adapter practice")
        bound["voxel"] = reference["voxel"]
    if "reader_history_window" in reference:
        if kind != "adapter":
            raise ValueError("Reader history window is supported only for adapter practice")
        policy = _checked_history_window_policy(reference["reader_history_window"])
        bound["history_window"] = policy["keep_latest_runs"]
    rebuilt = _practice_reference(memory, checked, reference["literal_context"],
                                  encounter=encounter, purpose=purpose, **bound)
    if packet[field] == rebuilt:
        return deepcopy(packet)
    if "voxel" in reference:
        raise ValueError("Voxel practice packet differs from its native projection")
    if "reader_history_window" in reference:
        raise ValueError("Declared reader history window differs from its native projection")
    # Preserve the declared pre-departure rule as a supported complete-run view.
    if bound and packet[field] == _practice_reference(
            memory, checked, reference["literal_context"], encounter=encounter, purpose=purpose,
            departures=False, **bound):
        return deepcopy(packet)
    if historical and canonical(reference).decode() == packet[field]:
        # Older immutable packets used only native-handle interning. Encoding
        # changes can alter the step at which the byte target is reached, so
        # rebuild each historical selection rule with its original transport.
        variants = [{**bound, "_legacy_transport": True}]
        if bound:
            variants.extend((
                {**bound, "departures": False, "_legacy_transport": True},
                {"max_bytes": bound["max_bytes"], "fold": False},
                {"max_bytes": bound["max_bytes"], "fold": False, "_legacy_transport": True},
            ))
        for variant in variants:
            try:
                previous = _practice_reference(memory, checked, reference["literal_context"],
                                               encounter=encounter, purpose=purpose, **variant)
            except ValueError:
                # A historical rule that cannot construct this complete input
                # within its original bound is not a matching candidate.
                continue
            if packet[field] == previous:
                return deepcopy(packet)
    raise ValueError("Practice packet changed or dropped its native learning context")
