"""Append-only Yggdrasil storage for a bounded latent-codec artifact bundle.

Uses only CrystalStore's public put/get/inspect methods. All artifacts use the
literal strategy so their logical costs remain visible. Recovery needs the
externally pinned manifest, decoder table(s), and final packet; it does not
open an original source, encoder, or model record. Models are retained for
provenance and charged separately. Source classification is caller-declared.

Mandatory sources bypass all modeling and folding. Other sources may still
use the codec's explicit, charged literal blocks; no hidden original copy is
stored. Content-addressed IDs allow shared models/tables and idempotent writes.
Each put is atomic, but the multi-artifact append is NOT one transaction. A
failed append can retain immutable intermediate artifacts; manifest publication
is last and failures are never retried here. No existing source is overwritten.

Bounds are cooperative monotonic checks around individually bounded operations,
not a process watchdog. CrystalStore.get verifies its own bounded graph before
this adapter can enforce the narrower artifact limit. Its existing maximum
source/decode bounds therefore also apply to malformed stored dependencies.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from ..crystals import CrystalStore
from . import codec
from . import candidate_inputs
from . import latent_model


SCHEMA = "crystal-latent-store-manifest/v1"
MAX_SOURCE_BYTES = 4096
MAX_PACKET_BYTES = 8192
MAX_MANIFEST_BYTES = 16384
MAX_SECONDS = 300
_KINDS = ("model", "table", "packet", "literal", "manifest")
_DELTA_KEYS = ("sources", "unique_nodes", "raw_source_bytes", "node_payload_bytes",
               "source_metadata_bytes", "source_id_utf8_bytes", "lexical_logical_bytes",
               "sqlite_bytes", "database_file_bytes")


class AdapterError(ValueError):
    pass


class AdapterTimeout(AdapterError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AdapterError("Expected a lowercase SHA-256")
    return value


def _id(kind, digest):
    return "latent:" + kind + ":" + _hash(digest)


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise AdapterError("Invalid manifest fields")


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise AdapterError("Invalid bounded integer")
    return value


def _source_id(value):
    try:
        if type(value) is not str or not 1 <= len(value.encode("utf-8")) <= 512:
            raise AdapterError("source_id must contain 1 through 512 UTF-8 bytes")
    except UnicodeError as exc:
        raise AdapterError("source_id is not valid UTF-8") from exc


class _Deadline:
    def __init__(self, seconds):
        try:
            valid = type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds <= MAX_SECONDS
        except OverflowError:
            valid = False
        if not valid:
            raise AdapterError("max_seconds must be positive, finite, and at most 300")
        self.started = time.monotonic()
        self.until = self.started + seconds

    def remaining(self):
        remaining = self.until - time.monotonic()
        if remaining <= 0:
            raise AdapterTimeout("Adapter cooperative wall deadline reached")
        return remaining

    def check(self):
        self.remaining()


class PreparedModel:
    """Immutable model/table bytes reusable across sources.

    prepare_model enables the original encoder path. load_candidate_bundle
    instead retains opaque caller-pinned bytes for explicit candidate plans.

    Python object construction is not an authenticity/security boundary. The
    engineering packet must pin the preparation implementation. Encoders receive
    just one eight-byte block; arbitrary caller-supplied callbacks are rejected.
    """
    __slots__ = ("_model_bytes", "_table_bytes", "_model_sha256", "_table_sha256", "_preparation_wall_seconds",
                 "_candidate_only")

    def __init__(self):
        raise AdapterError("Use prepare_model")

    def __setattr__(self, name, value):
        raise AdapterError("PreparedModel bytes are immutable")

    def __delattr__(self, name):
        raise AdapterError("PreparedModel bytes are immutable")

    @property
    def model_bytes(self):
        return self._model_bytes

    @property
    def table_bytes(self):
        return self._table_bytes

    @property
    def model_sha256(self):
        return self._model_sha256

    @property
    def table_sha256(self):
        return self._table_sha256

    @property
    def preparation_wall_seconds(self):
        return self._preparation_wall_seconds

    @property
    def candidate_only(self):
        # Older engineering fixtures predate this slot. Their original behavior
        # remains unchanged; object construction is not an authenticity boundary.
        return getattr(self, "_candidate_only", False)

    @property
    def preparation_provenance(self):
        return ("CALLER_PINNED_BYTES_NOT_COMPILE_AUTHENTICATED" if self.candidate_only
                else "PREPARED_MODEL_API")

    def encode(self, raw8):
        if self.candidate_only:
            raise AdapterError("Candidate-only pinned bytes cannot execute a model; supply explicit candidates")
        return latent_model.encode_block(json.loads(self._model_bytes), raw8)


def prepare_model(model, *, max_seconds=60):
    """Validate and compile once; return a reusable, charged model/table bundle."""
    budget = _Deadline(max_seconds)
    model = latent_model.validate(model)
    raw = _canonical(model)
    table = latent_model.compile_decoder(model, max_seconds=budget.remaining())
    if type(table) is not bytes or len(table) != codec.TABLE_BYTES:
        raise AdapterError("Compiler returned an invalid decoder table")
    budget.check()
    bundle = object.__new__(PreparedModel)
    for key, value in (("_model_bytes", raw), ("_table_bytes", table),
                       ("_model_sha256", _sha(raw)), ("_table_sha256", _sha(table)),
                       ("_preparation_wall_seconds", time.monotonic() - budget.started),
                       ("_candidate_only", False)):
        object.__setattr__(bundle, key, value)
    return bundle


def load_candidate_bundle(model_bytes, table_bytes, *, expected_model_sha256, expected_table_sha256):
    """Load caller-pinned provenance bytes for explicit candidate plans only.

    This checks byte identity and size, not model schema, scientific provenance
    or whether these table bytes were compiled from these model bytes. It never
    parses, executes or compiles a model. The opaque model bytes are retained and
    charged by the storage callers; default neural encoding is forbidden.
    """
    _hash(expected_model_sha256)
    _hash(expected_table_sha256)
    if type(model_bytes) is not bytes or not 1 <= len(model_bytes) <= latent_model.MAX_MODEL_BYTES:
        raise AdapterError("Candidate model provenance must be nonempty bounded bytes")
    if type(table_bytes) is not bytes or len(table_bytes) != codec.TABLE_BYTES:
        raise AdapterError("Candidate table must contain exactly 4096 eight-byte rows")
    if _sha(model_bytes) != expected_model_sha256 or _sha(table_bytes) != expected_table_sha256:
        raise AdapterError("Candidate model/table bytes differ from expected hashes")
    bundle = object.__new__(PreparedModel)
    for key, value in (("_model_bytes", model_bytes), ("_table_bytes", table_bytes),
                       ("_model_sha256", expected_model_sha256), ("_table_sha256", expected_table_sha256),
                       ("_preparation_wall_seconds", 0.0), ("_candidate_only", True)):
        object.__setattr__(bundle, key, value)
    return bundle


def _prepared(bundle):
    if type(bundle) is not PreparedModel:
        raise AdapterError("Only prepared model bundles are accepted")
    if (type(bundle.candidate_only) is not bool
            or type(bundle.model_bytes) is not bytes or len(bundle.model_bytes) > latent_model.MAX_MODEL_BYTES
            or _sha(bundle.model_bytes) != bundle.model_sha256
            or type(bundle.table_bytes) is not bytes or len(bundle.table_bytes) != codec.TABLE_BYTES
            or _sha(bundle.table_bytes) != bundle.table_sha256):
        raise AdapterError("Prepared model/table integrity failed")


def _candidate_source(source_id, raw, bundle, plan, budget):
    _source_id(source_id)
    if type(raw) is not bytes or len(raw) > MAX_SOURCE_BYTES:
        raise AdapterError("Source must be at most 4096 bytes")
    _prepared(bundle)
    try:
        return candidate_inputs.validate(
            plan, [dict(id=source_id, data=raw, mandatory=False)],
            bundle.table_bytes, MAX_SOURCE_BYTES, budget.check)
    except AdapterTimeout:
        raise
    except ValueError as exc:
        raise AdapterError("Invalid supplied candidates: " + str(exc)) from exc


def validate_candidate_source(source_id, raw, bundle, plan, *, max_seconds=60):
    """Validate one supplied-plan input without opening or writing a store.

    The returned digest/size report concerns the canonical caller-owned plan.
    Storage revalidates the input; validation grants no authority or provenance.
    """
    _, report = _candidate_source(source_id, raw, bundle, plan, _Deadline(max_seconds))
    return report


def _pin(kind, raw):
    digest = _sha(raw)
    return {"id": _id(kind, digest), "sha256": digest, "bytes": len(raw)}


def _validate_pin(pin, kind, maximum, exact=None):
    _keys(pin, ("id", "sha256", "bytes"))
    if pin["id"] != _id(kind, pin["sha256"]):
        raise AdapterError("Artifact ID does not match its content hash")
    _integer(pin["bytes"], 0, maximum)
    if exact is not None and pin["bytes"] != exact:
        raise AdapterError("Artifact size differs from the fixed format")


def _validate_manifest(manifest):
    _keys(manifest, ("schema", "authority", "classification", "source", "mode", "models",
                     "decoders", "packet", "literal", "layers"))
    if (manifest["schema"] != SCHEMA or manifest["authority"] != "NONE"
            or manifest["classification"] != "CALLER_DECLARED"):
        raise AdapterError("Unsupported manifest schema, authority or classification")
    source = manifest["source"]
    _keys(source, ("id", "sha256", "bytes", "mandatory"))
    _source_id(source["id"])
    _hash(source["sha256"])
    _integer(source["bytes"], 0, MAX_SOURCE_BYTES)
    if type(source["mandatory"]) is not bool:
        raise AdapterError("mandatory must be an explicit boolean")
    for field in ("models", "decoders", "layers"):
        if type(manifest[field]) is not list or len(manifest[field]) > 2:
            raise AdapterError("Invalid bounded layer list")
    if source["mandatory"]:
        if (manifest["mode"] != "MANDATORY_LITERAL" or manifest["models"] or manifest["decoders"]
                or manifest["layers"] or manifest["packet"] is not None):
            raise AdapterError("Mandatory context cannot use the learned path")
        _validate_pin(manifest["literal"], "literal", MAX_SOURCE_BYTES)
        if any(manifest["literal"][field] != source[field] for field in ("bytes", "sha256")):
            raise AdapterError("Mandatory literal does not match the source")
        return
    if (manifest["mode"] != "LATENT_RESIDUAL" or manifest["literal"] is not None
            or not 1 <= len(manifest["layers"]) <= 2
            or len(manifest["models"]) != len(manifest["layers"])
            or len(manifest["decoders"]) != len(manifest["layers"])):
        raise AdapterError("Invalid latent layer configuration")
    _validate_pin(manifest["packet"], "packet", MAX_PACKET_BYTES)
    previous_hash, previous_size = source["sha256"], source["bytes"]
    for level, layer in enumerate(manifest["layers"]):
        _keys(layer, ("level", "input_sha256", "input_bytes", "decoder_sha256", "packet_sha256", "packet_bytes"))
        _integer(layer["level"], level, level)
        for field in ("input_sha256", "decoder_sha256", "packet_sha256"):
            _hash(layer[field])
        _integer(layer["input_bytes"], 0, MAX_PACKET_BYTES)
        _integer(layer["packet_bytes"], codec.HEADER.size, MAX_PACKET_BYTES)
        if layer["input_sha256"] != previous_hash or layer["input_bytes"] != previous_size:
            raise AdapterError("Layer chain does not bind the preceding bytes")
        _validate_pin(manifest["models"][level], "model", latent_model.MAX_MODEL_BYTES)
        _validate_pin(manifest["decoders"][level], "table", codec.TABLE_BYTES, codec.TABLE_BYTES)
        if manifest["decoders"][level]["sha256"] != layer["decoder_sha256"]:
            raise AdapterError("Layer decoder pin differs")
        previous_hash, previous_size = layer["packet_sha256"], layer["packet_bytes"]
    if manifest["packet"]["sha256"] != previous_hash or manifest["packet"]["bytes"] != previous_size:
        raise AdapterError("Final packet does not match the layer chain")


def _store(store):
    if type(store) is not CrystalStore:
        raise AdapterError("Use the existing CrystalStore implementation")


def _load(store, pin, budget):
    budget.check()
    raw = store.get(pin["id"])
    budget.check()
    if type(raw) is not bytes or len(raw) != pin["bytes"] or _sha(raw) != pin["sha256"]:
        raise AdapterError("Stored artifact does not match its manifest pin")
    return raw


def store_source(store, source_id, raw, *, mandatory, prepared=(), max_seconds=60, supplied_candidates=None):
    """Append a bounded source representation and return its external manifest pin.

    supplied_candidates is an optional candidate_inputs plan bound to this
    source and one prepared table. It bypasses encoding, not provenance costs.
    """
    budget = _Deadline(max_seconds)
    _store(store)
    _source_id(source_id)
    if type(raw) is not bytes or len(raw) > MAX_SOURCE_BYTES:
        raise AdapterError("Source must be at most 4096 bytes")
    if type(mandatory) is not bool or type(prepared) not in (tuple, list):
        raise AdapterError("Explicit mandatory flag and prepared list/tuple required")
    if mandatory and prepared:
        raise AdapterError("Mandatory context must not receive model bundles")
    if not mandatory and not 1 <= len(prepared) <= 2:
        raise AdapterError("Nonmandatory sources need one or two prepared bundles")
    if supplied_candidates is None and any(type(bundle) is PreparedModel and bundle.candidate_only
                                            for bundle in prepared):
        raise AdapterError("Candidate-only bundle requires an explicit supplied-candidate plan")
    candidate_sets, candidate_report = None, None
    if supplied_candidates is not None:
        if mandatory or len(prepared) != 1:
            raise AdapterError("Supplied candidates require one nonmandatory prepared layer")
        candidate_sets, candidate_report = _candidate_source(
            source_id, raw, prepared[0], supplied_candidates, budget)
    manifest = {"schema": SCHEMA, "authority": "NONE", "classification": "CALLER_DECLARED",
                "source": {"id": source_id, "sha256": _sha(raw), "bytes": len(raw), "mandatory": mandatory},
                "mode": "MANDATORY_LITERAL" if mandatory else "LATENT_RESIDUAL",
                "models": [], "decoders": [], "packet": None, "literal": None, "layers": []}
    artifacts, layer_accounting = {}, []

    def retain(kind, data):
        pin = _pin(kind, data)
        artifacts[pin["id"]] = (kind, data, pin)
        return pin

    if mandatory:
        manifest["literal"] = retain("literal", raw)
    else:
        current = raw
        for level, bundle in enumerate(prepared):
            budget.check()
            _prepared(bundle)
            manifest["models"].append(retain("model", bundle.model_bytes))
            manifest["decoders"].append(retain("table", bundle.table_bytes))
            codes = []
            if candidate_sets is None:
                for offset in range(0, len(current) - 7, 8):
                    budget.check()
                    codes.append(bundle.encode(current[offset:offset + 8]))
                    budget.check()
                folded = codec.fold(current, codes, bundle.table_bytes, level=level)
            else:
                folded = codec.fold_candidates(current, candidate_sets[source_id, 0], bundle.table_bytes, level=level)
                budget.check()
            if len(folded["packet"]) > MAX_PACKET_BYTES:
                raise AdapterError("Folded representation exceeds its bound")
            manifest["layers"].append({"level": level, "input_sha256": _sha(current),
                                        "input_bytes": len(current), "decoder_sha256": bundle.table_sha256,
                                        "packet_sha256": _sha(folded["packet"]), "packet_bytes": len(folded["packet"])})
            layer_accounting.append({"encoder_calls": len(codes), **folded["accounting"]})
            if candidate_sets is not None:
                layer_accounting[-1]["selection"] = folded["selection"]
            current = folded["packet"]
        # Only the outer packet is stored. The inner packet is reconstructed by
        # the second layer; it is not retained as an uncharged recovery copy.
        manifest["packet"] = retain("packet", current)
        if codec.unfold_layers(current, [item.table_bytes for item in prepared], manifest["layers"]) != raw:
            raise AdapterError("Pre-append exact recovery failed")
    _validate_manifest(manifest)
    manifest_raw = _canonical(manifest)
    if len(manifest_raw) > MAX_MANIFEST_BYTES:
        raise AdapterError("Manifest exceeds its byte bound")
    manifest_pin = retain("manifest", manifest_raw)  # Always published last.
    budget.check()
    before = store.inspect()
    logical = {kind: 0 for kind in _KINDS}
    newly_appended = {kind: 0 for kind in _KINDS}
    for kind, data, pin in artifacts.values():
        budget.check()
        logical[kind] += len(data)
        metadata = {"schema": SCHEMA, "kind": kind, "authority": "NONE"}
        outcome = store.put(pin["id"], data, metadata, strategy="literal", report=False)
        # Existing immutable IDs retain their original storage contract. Exact
        # bytes alone must not hide a conventional codec or different provenance.
        if outcome["strategy"] != "literal" or outcome["metadata"] != metadata:
            raise AdapterError("Existing artifact has a different storage contract")
        if not outcome["idempotent"]:
            newly_appended[kind] += len(data)
        _load(store, pin, budget)
    after = store.inspect()
    recovered = restore_source(store, manifest_pin["id"], expected_manifest_sha256=manifest_pin["sha256"],
                               max_seconds=budget.remaining())
    if recovered != raw:
        raise AdapterError("Post-append recovery differs")
    budget.check()
    return {"schema": "crystal-latent-store-append/v1", "manifest": manifest_pin,
            "source_sha256": _sha(raw), "source_bytes": len(raw), "mandatory": mandatory,
            "logical_artifact_bytes_by_kind": logical, "logical_artifact_bytes_total": sum(logical.values()),
            "new_source_record_payload_bytes_by_kind": newly_appended,
            "store_delta": {key: after[key] - before[key] for key in _DELTA_KEYS},
            "layer_accounting": layer_accounting, "exact_readback": True,
            **({"supplied_candidates": candidate_report} if candidate_sets is not None else {}),
            **({"bundle_provenance": prepared[0].preparation_provenance}
               if candidate_sets is not None and prepared[0].candidate_only else {}),
            "wall_seconds": time.monotonic() - budget.started,
            "preparation_cost": ("Caller-pinned opaque bytes; prior preparation/generation work is unmeasured "
                                 "and separately chargeable; no compilation is performed or authenticated here"
                                 if candidate_sets is not None and prepared[0].candidate_only else
                                 "Separate prepare_model work; reused bundles are not recompiled here"),
            "cost_note": "Logical artifact bytes and SQLite bytes are distinct; neither proves savings",
            "authority": "NONE"}


def restore_source(store, manifest_id, *, expected_manifest_sha256, max_seconds=60):
    """Recover without originals or models, anchored by an external manifest hash."""
    budget = _Deadline(max_seconds)
    _store(store)
    _hash(expected_manifest_sha256)
    if manifest_id != _id("manifest", expected_manifest_sha256):
        raise AdapterError("Manifest ID disagrees with the external hash")
    budget.check()
    raw = store.get(manifest_id)
    budget.check()
    if type(raw) is not bytes or len(raw) > MAX_MANIFEST_BYTES or _sha(raw) != expected_manifest_sha256:
        raise AdapterError("Manifest bytes do not match the external pin or bound")
    try:
        manifest = json.loads(raw)
        if _canonical(manifest) != raw:
            raise AdapterError("Manifest is not canonical JSON")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AdapterError("Malformed manifest JSON") from exc
    _validate_manifest(manifest)
    if manifest["source"]["mandatory"]:
        recovered = _load(store, manifest["literal"], budget)
    else:
        packet = _load(store, manifest["packet"], budget)
        # Do not read the model pins: recovery is deliberately independent of
        # the encoder and its parameters. Reuse duplicate table reads locally.
        tables = {}
        decoders = []
        for pin in manifest["decoders"]:
            if pin["id"] not in tables:
                tables[pin["id"]] = _load(store, pin, budget)
            decoders.append(tables[pin["id"]])
        recovered = codec.unfold_layers(packet, decoders, manifest["layers"])
    budget.check()
    if len(recovered) != manifest["source"]["bytes"] or _sha(recovered) != manifest["source"]["sha256"]:
        raise AdapterError("Original source integrity failed")
    return recovered
