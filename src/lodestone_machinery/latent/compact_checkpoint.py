"""Compact, independently recoverable crystal checkpoints; engineering only.

One binary directory replaces per-chunk JSON manifests and the repeated index.
Existing lifting/latent packets are unchanged. Literal, integer and neural arms
share the same source/recipe/artifact layout. Mandatory classification is caller
supplied, never inferred; mandatory sources always remain literal. Neural models
are retained and charged but never read or executed by the decoder.

The optional packed transport losslessly wraps individual logical artifacts.
Directory bytes and artifact identities do not change. Any artifact referenced
by a mandatory source stays physically raw; all other artifacts use zlib only
when shorter. Raw checkpoints remain the default and retain their old format.

All operations have cooperative deadlines, not process supervision. Appends and
imports are not multi-record transactions; failure leaves immutable evidence.
No retry, deletion, automatic corpus discovery, model fit or native-session
restoration is performed. A directory pin authenticates supplied declarations;
it does not establish authority, classification or source custody.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import time

from ..crystals import CrystalStore
from . import codec
from . import candidate_inputs
from . import lifting
from . import compact_layout as layout
from . import artifact_storage


SCHEMA = "crystal-compact-checkpoint/v1"
MAGIC = b"LTCPT002"
PACKED_SCHEMA = "crystal-packed-checkpoint/v1"
PACKED_MAGIC = b"LTCPT003"
HEADER = struct.Struct(">8sIQ32s")
MAX_CHECKPOINT_BYTES = 8388608
MAX_CACHE_BYTES = 2097152
CheckpointError = layout.Error


class CheckpointTimeout(CheckpointError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise CheckpointError("Invalid bounded integer")
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CheckpointError("Invalid SHA-256")
    return value


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise CheckpointError("Invalid object fields")


def _source_id(value):
    try:
        valid = type(value) is str and 1 <= len(value.encode("utf-8")) <= 512
    except UnicodeError:
        valid = False
    if not valid:
        raise CheckpointError("Invalid UTF-8 source identity")


class _Deadline:
    def __init__(self, seconds):
        try:
            valid = type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds <= 300
        except OverflowError:
            valid = False
        if not valid:
            raise CheckpointError("Deadline must be finite, positive and at most300s")
        self.start, self.cpu = time.monotonic(), time.process_time()
        self.end = self.start + seconds

    def remaining(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise CheckpointTimeout("Cooperative deadline reached")
        return remaining

    def check(self):
        self.remaining()

    def timing(self):
        return {"wall_seconds": time.monotonic() - self.start,
                "cpu_seconds": time.process_time() - self.cpu}


def _identifier(kind, digest):
    if kind not in (*layout.KINDS, "directory"):
        raise CheckpointError("Unknown artifact kind")
    return "compact:" + kind + ":" + _hash(digest)


def _pin(kind, raw):
    digest = _sha(raw)
    return {"id": _identifier(kind, digest), "sha256": digest, "bytes": len(raw)}


def _artifact_pin(record):
    return {"id": _identifier(record["kind"], record["sha256"]),
            "sha256": record["sha256"], "bytes": record["bytes"]}


def _validate_pin(pin):
    _keys(pin, ("id", "sha256", "bytes"))
    if type(pin["id"]) is not str or len(pin["id"].split(":")) != 3:
        raise CheckpointError("Invalid typed artifact identity")
    namespace, kind, digest = pin["id"].split(":")
    if namespace != "compact" or pin["id"] != _identifier(kind, digest) or digest != _hash(pin["sha256"]):
        raise CheckpointError("Artifact hash/identity differs")
    maximum = layout.MAX_DIRECTORY_BYTES if kind == "directory" else layout.MAXIMUMS[kind]
    _int(pin["bytes"], 0, maximum)
    return kind


def _get(provider, pin, budget):
    _validate_pin(pin)
    budget.check()
    raw = provider.get(pin["id"])
    budget.check()
    if type(raw) is not bytes or len(raw) != pin["bytes"] or _sha(raw) != pin["sha256"]:
        raise CheckpointError("Artifact differs from its pin")
    return raw


def _put(store, pin, raw, budget):
    kind = _validate_pin(pin)
    if len(raw) != pin["bytes"] or _sha(raw) != pin["sha256"]:
        raise CheckpointError("Append bytes differ from pin")
    metadata = {"schema": SCHEMA, "kind": kind, "authority": "NONE"}
    budget.check()
    outcome = store.put(pin["id"], raw, metadata, strategy="literal", report=False)
    if outcome["metadata"] != metadata or outcome["strategy"] != "literal":
        raise CheckpointError("Existing artifact storage contract differs")
    if _get(store, pin, budget) != raw:
        raise CheckpointError("Append failed exact readback")


def _directory(provider, pin, budget):
    if _validate_pin(pin) != "directory":
        raise CheckpointError("External directory pin required")
    return layout.decode(_get(provider, pin, budget))


def _recipe_key(recipe, identifiers):
    return (recipe["encoding"], tuple(identifiers[i] for i in recipe["models"]),
            tuple(identifiers[i] for i in recipe["tables"]))


def build_catalog(store, sources, *, encoding="literal", lifting_model=None,
                  prepared=(), fixed_code=False, previous_pin=None, max_seconds=60, supplied_candidates=None):
    """Append source identities; never overwrite or infer mandatory flags.

    supplied_candidates binds every new source and chunk to one prepared table;
    it bypasses model encoding and leaves the stored format unchanged.
    fixed_code uses q=(0,0,0) at EVERY neural layer, retaining exactly the supplied
    model/table provenance and charges. It is an intervention, not a learned fit.
    """
    budget = _Deadline(max_seconds)
    if type(store) is not CrystalStore or type(sources) is not list or not 1 <= len(sources) <= layout.MAX_SOURCES:
        raise CheckpointError("Explicit sources and CrystalStore required")
    if type(encoding) is not str or encoding not in ("literal", "lifting", "latent") or type(fixed_code) is not bool:
        raise CheckpointError("Invalid explicit encoding")
    if type(prepared) not in (list, tuple) or len(prepared) > 2:
        raise CheckpointError("Invalid prepared bundle list")
    if encoding == "literal" and (prepared or lifting_model is not None or fixed_code):
        raise CheckpointError("Literal control cannot carry encoding parameters")
    if encoding == "lifting" and (lifting_model is None or prepared or fixed_code):
        raise CheckpointError("Lifting requires only its explicit model")
    if encoding == "latent" and (not prepared or lifting_model is not None):
        raise CheckpointError("Latent encoding requires one or two bundles")
    if supplied_candidates is not None and (encoding != "latent" or len(prepared) != 1 or fixed_code):
        raise CheckpointError("Supplied candidates require one latent layer without fixed_code")
    if encoding == "lifting":
        lifting_model = lifting.validate(lifting_model)
    if encoding == "latent":
        from . import store_adapter
        for bundle in prepared:
            store_adapter._prepared(bundle)
            if supplied_candidates is None and bundle.candidate_only:
                raise CheckpointError("Candidate-only bundle requires an explicit supplied-candidate plan")
    previous = _directory(store, previous_pin, budget) if previous_pin is not None else None
    prior_sources = [] if previous is None else previous["sources"]
    ids = {s["id"] for s in prior_sources}
    total = sum(s["bytes"] for s in prior_sources)
    if len(prior_sources) + len(sources) > layout.MAX_SOURCES:
        raise CheckpointError("Too many sources")
    for source in sources:
        _keys(source, ("id", "data", "mandatory"))
        _source_id(source["id"])
        if source["id"] in ids:
            raise CheckpointError("Source identity already exists")
        ids.add(source["id"])
        if type(source["data"]) is not bytes or len(source["data"]) > layout.MAX_SOURCE_BYTES or type(source["mandatory"]) is not bool:
            raise CheckpointError("Invalid source bytes or mandatory flag")
        total += len(source["data"])
    if total > layout.MAX_TOTAL_SOURCE_BYTES:
        raise CheckpointError("Total source bytes exceed bound")
    candidate_sets, candidate_report = None, None
    candidate_chunks = []
    if supplied_candidates is not None:
        if any(source["mandatory"] for source in sources):
            raise CheckpointError("Supplied candidates cannot include mandatory sources")
        try:
            candidate_sets, candidate_report = candidate_inputs.validate(
                supplied_candidates, sources, prepared[0].table_bytes, layout.CHUNK_BYTES, budget.check)
        except CheckpointTimeout:
            raise
        except ValueError as exc:
            raise CheckpointError("Invalid supplied candidates: " + str(exc)) from exc
    artifacts, pending, draft_sources, recipe_keys = {}, {}, [], set()
    if previous:
        old_ids = [_artifact_pin(a)["id"] for a in previous["artifacts"]]
        artifacts.update(zip(old_ids, previous["artifacts"]))
        for source in prior_sources:
            entry = {**source, "chunks": []}
            for chunk in source["chunks"]:
                key = _recipe_key(previous["recipes"][chunk["recipe"]], old_ids)
                recipe_keys.add(key)
                entry["chunks"].append({**chunk, "recipe": key, "record": old_ids[chunk["record"]]})
            draft_sources.append(entry)

    def retain(kind, raw):
        pin = _pin(kind, raw)
        _validate_pin(pin)
        record = {"kind": kind, "sha256": pin["sha256"], "bytes": len(raw)}
        if pin["id"] in artifacts and artifacts[pin["id"]] != record:
            raise CheckpointError("Conflicting artifact pin")
        if pin["id"] in pending and pending[pin["id"]] != raw:
            raise CheckpointError("Hash collision in pending artifacts")
        artifacts[pin["id"]], pending[pin["id"]] = record, raw
        if len(artifacts) > layout.MAX_ARTIFACTS or sum(a["bytes"] for a in artifacts.values()) > layout.MAX_PAYLOAD_BYTES:
            raise CheckpointError("Artifact closure exceeds its bound")
        return pin["id"]

    encoder_calls, chunks_written = 0, 0
    for source in sources:
        entry = {"id": source["id"], "mandatory": source["mandatory"],
                 "bytes": len(source["data"]), "sha256": _sha(source["data"]), "chunks": []}
        for offset in range(0, max(1, len(source["data"])), layout.CHUNK_BYTES):
            budget.check()
            raw = source["data"][offset:offset + layout.CHUNK_BYTES]
            chunk = {"sha256": _sha(raw), "recipe": None, "record": None, "inner": None}
            if source["mandatory"] or encoding == "literal":
                key = ("literal", (), ())
                chunk["record"] = retain("literal", raw)
                recovered = raw
            elif encoding == "lifting":
                model_id = retain("lifting-model", lifting.model_bytes(lifting_model))
                key = ("lifting", (model_id,), ())
                packet = lifting.encode(raw, lifting_model)
                recovered = lifting.decode(packet, lifting_model, expected_sha256=_sha(raw), expected_size=len(raw))
                chunk["record"] = retain("lifting-packet", packet)
            else:
                models, tables, layers, current = [], [], [], raw
                for level, bundle in enumerate(prepared):
                    models.append(retain("model", bundle.model_bytes))
                    tables.append(retain("table", bundle.table_bytes))
                    codes = []
                    if candidate_sets is None:
                        for pos in range(0, len(current) - 7, 8):
                            budget.check()
                            codes.append((0, 0, 0) if fixed_code else bundle.encode(current[pos:pos + 8]))
                            if not fixed_code:
                                encoder_calls += 1
                        folded = codec.fold(current, codes, bundle.table_bytes, level=level)["packet"]
                    else:
                        selected = codec.fold_candidates(current, candidate_sets[source["id"], offset],
                                                         bundle.table_bytes, level=level)
                        budget.check()
                        candidate_chunks.append(dict(source_id=source["id"], offset=offset,
                            accounting=selected["accounting"], selection=selected["selection"]))
                        folded = selected["packet"]
                    if len(folded) > 8192:
                        raise CheckpointError("Folded chunk exceeds bound")
                    layers.append({"level": level, "input_sha256": _sha(current), "input_bytes": len(current),
                                   "decoder_sha256": bundle.table_sha256, "packet_sha256": _sha(folded),
                                   "packet_bytes": len(folded)})
                    if level == 0 and len(prepared) == 2:
                        chunk["inner"] = {"sha256": _sha(folded), "bytes": len(folded)}
                    current = folded
                key = ("latent", tuple(models), tuple(tables))
                recovered = codec.unfold_layers(current, [b.table_bytes for b in prepared], layers)
                chunk["record"] = retain("packet", current)
            if recovered != raw:
                raise CheckpointError("Encoded chunk failed exact recovery")
            chunk["recipe"] = key
            recipe_keys.add(key)
            entry["chunks"].append(chunk)
            chunks_written += 1
        draft_sources.append(entry)
    artifact_ids = sorted(artifacts, key=lambda name: (layout.KINDS.index(artifacts[name]["kind"]), artifacts[name]["sha256"]))
    indices = {name: i for i, name in enumerate(artifact_ids)}
    recipe_list = sorted(recipe_keys, key=lambda k: (("literal", "lifting", "latent").index(k[0]),
                                                    tuple(indices[n] for n in k[1]), tuple(indices[n] for n in k[2])))
    recipe_indices = {key: i for i, key in enumerate(recipe_list)}
    directory = {"previous": None if previous_pin is None else previous_pin["sha256"],
                 "artifacts": [artifacts[name] for name in artifact_ids],
                 "recipes": [{"encoding": k[0], "models": [indices[n] for n in k[1]],
                              "tables": [indices[n] for n in k[2]]} for k in recipe_list], "sources": []}
    for source in sorted(draft_sources, key=lambda s: s["id"].encode("utf-8")):
        directory["sources"].append({**source, "chunks": [
            {**c, "recipe": recipe_indices[c["recipe"]], "record": indices[c["record"]]} for c in source["chunks"]]})
    directory_raw = layout.encode(directory)
    directory_pin = _pin("directory", directory_raw)
    for name, raw in pending.items():
        _put(store, _artifact_pin(artifacts[name]), raw, budget)
    _put(store, directory_pin, directory_raw, budget)
    totals = {kind: sum(a["bytes"] for a in directory["artifacts"] if a["kind"] == kind) for kind in layout.KINDS}
    return {"directory": directory_pin, "source_bytes": total, "sources": len(directory["sources"]),
            "new_chunks": chunks_written, "encoder_calls": encoder_calls, "fixed_code": fixed_code,
            **({"supplied_candidates": {**candidate_report, "chunks": candidate_chunks}}
               if candidate_sets is not None else {}),
            **({"bundle_provenance": prepared[0].preparation_provenance,
                "preparation_cost": "Caller-pinned opaque bytes; prior preparation/generation work is unmeasured "
                                    "and separately chargeable; no compilation is performed or authenticated here"}
               if candidate_sets is not None and prepared[0].candidate_only else {}),
            "artifact_bytes_by_kind": totals, "artifact_bytes": sum(totals.values()),
            "directory_bytes": len(directory_raw), "prior_artifact_integrity": "UNCHECKED" if previous else "NOT_APPLICABLE",
            "authority": "NONE", **budget.timing()}


class ReadSession:
    def __init__(self, provider, directory_pin, *, max_seconds=60):
        budget = _Deadline(max_seconds)
        if not callable(getattr(provider, "get", None)) or _validate_pin(directory_pin) != "directory":
            raise CheckpointError("Provider and external directory pin required")
        self.provider = provider
        self.cache, self.cache_bytes = OrderedDict(), 0
        self.storage_index_bytes = provider.storage_index_bytes if isinstance(provider, CheckpointReader) else 0
        self.cache_limit = max(0, MAX_CACHE_BYTES - directory_pin["bytes"] - self.storage_index_bytes)
        self.counts = {"logical_get_bytes": 0, "logical_get_calls": 0, "get_bytes_by_kind": {},
                       "get_calls_by_kind": {}, "cache_hits_by_kind": {}, "chunks_decoded": 0,
                       "full_chunk_bytes_decoded": 0, "requested_bytes_returned": 0, "mandatory_bytes_returned": 0}
        raw = self._get(directory_pin, budget)
        self.directory = layout.decode(raw)
        self.directory_pin = dict(directory_pin)
        self.sources = {s["id"]: s for s in self.directory["sources"]}
        budget.check()
        self.initialization_timing = budget.timing()

    def _get(self, pin, budget):
        kind = _validate_pin(pin)
        budget.check()
        if pin["id"] in self.cache:
            old_pin, raw = self.cache.pop(pin["id"])
            if old_pin != pin:
                raise CheckpointError("Conflicting cached pin")
            self.cache[pin["id"]] = old_pin, raw
            self.counts["cache_hits_by_kind"][kind] = self.counts["cache_hits_by_kind"].get(kind, 0) + 1
            return raw
        raw = self.provider.get(pin["id"])
        self.counts["logical_get_calls"] += 1
        self.counts["get_calls_by_kind"][kind] = self.counts["get_calls_by_kind"].get(kind, 0) + 1
        if type(raw) is bytes:
            self.counts["logical_get_bytes"] += len(raw)
            self.counts["get_bytes_by_kind"][kind] = self.counts["get_bytes_by_kind"].get(kind, 0) + len(raw)
        budget.check()
        if type(raw) is not bytes or len(raw) != pin["bytes"] or _sha(raw) != pin["sha256"]:
            raise CheckpointError("Record differs from its pin")
        if kind in ("table", "lifting-model"):
            while self.cache and self.cache_bytes + len(raw) > self.cache_limit:
                _, (_, removed) = self.cache.popitem(last=False)
                self.cache_bytes -= len(removed)
            if len(raw) <= self.cache_limit:
                self.cache[pin["id"]] = dict(pin), raw
                self.cache_bytes += len(raw)
        return raw

    def _record(self, index, budget):
        return self._get(_artifact_pin(self.directory["artifacts"][index]), budget)

    def _chunk(self, source, ordinal, budget):
        chunk = source["chunks"][ordinal]
        size = min(layout.CHUNK_BYTES, source["bytes"] - ordinal * layout.CHUNK_BYTES)
        recipe = self.directory["recipes"][chunk["recipe"]]
        packet = self._record(chunk["record"], budget)
        if recipe["encoding"] == "literal":
            raw = packet
        elif recipe["encoding"] == "lifting":
            model_raw = self._record(recipe["models"][0], budget)
            try:
                model = lifting.validate(json.loads(model_raw))
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                raise CheckpointError("Invalid lifting model") from exc
            if lifting.model_bytes(model) != model_raw:
                raise CheckpointError("Noncanonical lifting model")
            try:
                raw = lifting.decode(packet, model, expected_sha256=chunk["sha256"], expected_size=size)
            except ValueError as exc:
                raise CheckpointError("Invalid lifting packet") from exc
        else:
            tables = [self._record(i, budget) for i in recipe["tables"]]
            outer = self.directory["artifacts"][chunk["record"]]
            inputs = [{"sha256": chunk["sha256"], "bytes": size}]
            outputs = [outer]
            if len(tables) == 2:
                inputs.append(chunk["inner"])
                outputs.insert(0, chunk["inner"])
            layers = [{"level": i, "input_sha256": inputs[i]["sha256"], "input_bytes": inputs[i]["bytes"],
                       "decoder_sha256": self.directory["artifacts"][recipe["tables"][i]]["sha256"],
                       "packet_sha256": outputs[i]["sha256"], "packet_bytes": outputs[i]["bytes"]} for i in range(len(tables))]
            try:
                raw = codec.unfold_layers(packet, tables, layers)
            except ValueError as exc:
                raise CheckpointError("Invalid latent packet") from exc
        budget.check()
        if len(raw) != size or _sha(raw) != chunk["sha256"]:
            raise CheckpointError("Recovered chunk differs from source pin")
        self.counts["chunks_decoded"] += 1
        self.counts["full_chunk_bytes_decoded"] += len(raw)
        return raw

    def read(self, source_id, start=0, stop=None, *, max_seconds=60):
        budget = _Deadline(max_seconds)
        _source_id(source_id)
        if source_id not in self.sources:
            raise KeyError(source_id)
        selected = self.sources[source_id]
        stop = selected["bytes"] if stop is None else stop
        _int(start, 0, selected["bytes"])
        _int(stop, start, selected["bytes"])
        mandatory, checked = {}, []
        for source in self.directory["sources"]:
            if source["mandatory"]:
                body = b"".join(self._chunk(source, i, budget) for i in range(len(source["chunks"])))
                if len(body) != source["bytes"] or _sha(body) != source["sha256"]:
                    raise CheckpointError("Mandatory source failed whole-source verification")
                mandatory[source["id"]] = body
                checked.append(source["id"])
        if selected["mandatory"]:
            data = mandatory[source_id][start:stop]
        else:
            pieces = []
            for i in range(len(selected["chunks"])):
                left = i * layout.CHUNK_BYTES
                right = min(left + layout.CHUNK_BYTES, selected["bytes"])
                if (start < stop and start < right and left < stop) or selected["bytes"] == 0:
                    raw = self._chunk(selected, i, budget)
                    pieces.append(raw[max(0, start - left):min(len(raw), stop - left)])
            data = b"".join(pieces)
            if start == 0 and stop == selected["bytes"]:
                if _sha(data) != selected["sha256"]:
                    raise CheckpointError("Selected whole source failed verification")
                checked.append(source_id)
        if len(data) != stop - start:
            raise CheckpointError("Range length mismatch")
        self.counts["requested_bytes_returned"] += len(data)
        self.counts["mandatory_bytes_returned"] += sum(map(len, mandatory.values()))
        budget.check()
        return {"source_id": source_id, "start": start, "stop": stop, "data": data, "mandatory": mandatory,
                "checked_whole_source_ids": checked, "selected_whole_source_integrity": "CHECKED" if source_id in checked else "UNCHECKED",
                "global_integrity": "UNCHECKED", "metrics": self.metrics(), "authority": "NONE", **budget.timing()}

    def metrics(self):
        result = json.loads(_canonical(self.counts))
        result["encoded_directory_bytes"] = self.directory_pin["bytes"]
        result["table_model_cache_bytes"] = self.cache_bytes
        result["metadata_table_cache_bytes"] = self.cache_bytes + self.directory_pin["bytes"] + self.storage_index_bytes
        if self.storage_index_bytes:
            result["encoded_storage_index_bytes"] = self.storage_index_bytes
        result["initialization_timing"] = dict(self.initialization_timing)
        result["scope"] = "Provider-returned bytes; cache counts serialized bytes, not Python object sizes or RSS; physical I/O not inferred; decoded bodies not cached"
        if isinstance(self.provider, CheckpointReader):
            result["checkpoint_io"] = self.provider.metrics()
        return result


def _descriptor(value):
    if type(value) is not dict or value.get("schema") not in (SCHEMA, PACKED_SCHEMA):
        raise CheckpointError("Invalid checkpoint descriptor")
    packed = value["schema"] == PACKED_SCHEMA
    names = ("schema", "directory", "file_bytes", "file_sha256", "artifacts")
    _keys(value, names + (("storage_index",) if packed else ()))
    if _validate_pin(value["directory"]) != "directory":
        raise CheckpointError("Invalid checkpoint descriptor")
    _hash(value["file_sha256"])
    _int(value["artifacts"], 1, layout.MAX_ARTIFACTS)
    index_bytes = 0
    if packed:
        _keys(value["storage_index"], ("bytes", "sha256"))
        index_bytes = artifact_storage.INDEX_ROW.size * value["artifacts"]
        _int(value["storage_index"]["bytes"], index_bytes, index_bytes)
        _hash(value["storage_index"]["sha256"])
    _int(value["file_bytes"], HEADER.size + value["directory"]["bytes"] + index_bytes,
         MAX_CHECKPOINT_BYTES)


def descriptor_bytes(descriptor):
    _descriptor(descriptor)
    return _canonical(descriptor)


def complete_cost(descriptor):
    size = len(descriptor_bytes(descriptor))
    return {"file_bytes": descriptor["file_bytes"], "descriptor_bytes": size,
            "total_bytes": descriptor["file_bytes"] + size}


def export_checkpoint(provider, directory_pin, path, *, max_seconds=60, transport="raw"):
    """Write one exclusive portable file, optionally wrapping each artifact.

    Packed export retains at most the bounded logical payload total in selected
    transport bytes while constructing the index. Compression/decompression
    work belongs to the caller's operation budget; the descriptor contains only
    portable integrity and cost fields. Partial files are never removed/reused.
    """
    budget = _Deadline(max_seconds)
    if type(transport) is not str or transport not in ("raw", artifact_storage.NAME):
        raise CheckpointError("Unknown artifact transport")
    _validate_pin(directory_pin)
    directory_pin = dict(directory_pin)
    directory = _directory(provider, directory_pin, budget)
    directory_raw = layout.encode(directory)
    packed = transport == artifact_storage.NAME
    storage_index_raw, stored_payloads = b"", []
    if packed:
        forced = artifact_storage.mandatory_refs(directory)
        entries = []
        for index, artifact in enumerate(directory["artifacts"]):
            raw = _get(provider, _artifact_pin(artifact), budget)
            budget.check()
            mode, stored = artifact_storage.select(raw, force_raw=index in forced)
            budget.check()
            entries.append({"mode": mode, "stored_bytes": len(stored)})
            stored_payloads.append(stored)
        storage_index_raw = artifact_storage.encode_index(entries, directory)
        payload = len(storage_index_raw) + sum(map(len, stored_payloads))
    else:
        payload = sum(a["bytes"] for a in directory["artifacts"])
    total = HEADER.size + len(directory_raw) + payload
    if total > MAX_CHECKPOINT_BYTES:
        raise CheckpointError("Complete checkpoint exceeds byte bound")
    header = HEADER.pack(PACKED_MAGIC if packed else MAGIC, len(directory_raw), payload,
                         bytes.fromhex(directory_pin["sha256"]))
    digest = hashlib.sha256()
    budget.check()
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb", buffering=0) as output:
        for raw in (header, directory_raw) + ((storage_index_raw,) if packed else ()):
            budget.check()
            if output.write(raw) != len(raw):
                raise CheckpointError("Short checkpoint write")
            digest.update(raw)
        for index, artifact in enumerate(directory["artifacts"]):
            budget.check()
            raw = stored_payloads[index] if packed else _get(provider, _artifact_pin(artifact), budget)
            if output.write(raw) != len(raw):
                raise CheckpointError("Short artifact write")
            digest.update(raw)
            budget.check()
        os.fsync(output.fileno())
        budget.check()
    budget.check()
    descriptor = {"schema": PACKED_SCHEMA if packed else SCHEMA, "directory": dict(directory_pin),
                  "file_bytes": total, "file_sha256": digest.hexdigest(),
                  "artifacts": len(directory["artifacts"])}
    if packed:
        descriptor["storage_index"] = {"bytes": len(storage_index_raw), "sha256": _sha(storage_index_raw)}
    budget.check()
    return descriptor


class CheckpointReader:
    def __init__(self, path, descriptor, *, max_seconds=60):
        budget = _Deadline(max_seconds)
        _descriptor(descriptor)
        self.descriptor = json.loads(descriptor_bytes(descriptor))
        descriptor = self.descriptor
        self.packed = descriptor["schema"] == PACKED_SCHEMA
        self.storage_index_bytes = descriptor["storage_index"]["bytes"] if self.packed else 0
        self.storage_index_raw = b""
        self.storage_entries = []
        self.fp = None
        self.full_file_integrity = "UNCHECKED"
        self.counts = {"header_bytes_read": 0, "directory_bytes_read": 0, "artifact_bytes_read": 0,
                       "artifact_gets": 0, "artifact_bytes_by_kind": {}, "full_scan_bytes_read": 0}
        if self.packed:
            self.counts.update(index_bytes_read=0, expanded_artifact_bytes=0, expanded_by_kind={},
                               decompressions=0, decompression_wall_seconds=0.0,
                               decompression_cpu_seconds=0.0)
        self._records, self._storage = {}, {}
        path = Path(path)
        if path.is_symlink():
            raise CheckpointError("Checkpoint cannot be a symlink")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        self.fp = os.fdopen(fd, "rb", buffering=0)
        try:
            info = os.fstat(self.fp.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != descriptor["file_bytes"]:
                raise CheckpointError("Checkpoint size/type differs")
            header = self.fp.read(HEADER.size)
            self.counts["header_bytes_read"] += len(header)
            if len(header) != HEADER.size:
                raise CheckpointError("Truncated header")
            magic, size, payload, digest = HEADER.unpack(header)
            if (magic != (PACKED_MAGIC if self.packed else MAGIC)
                    or size != descriptor["directory"]["bytes"] or digest.hex() != descriptor["directory"]["sha256"]
                    or HEADER.size + size + payload != info.st_size):
                raise CheckpointError("Header differs from descriptor")
            self.directory_raw = self.fp.read(size)
            self.counts["directory_bytes_read"] += len(self.directory_raw)
            if len(self.directory_raw) != size or _sha(self.directory_raw) != descriptor["directory"]["sha256"]:
                raise CheckpointError("Directory differs from its pin")
            self.directory = layout.decode(self.directory_raw)
            if len(self.directory["artifacts"]) != descriptor["artifacts"]:
                raise CheckpointError("Artifact count differs")
            if self.packed:
                self.storage_index_raw = self.fp.read(self.storage_index_bytes)
                self.counts["index_bytes_read"] += len(self.storage_index_raw)
                if (len(self.storage_index_raw) != self.storage_index_bytes
                        or _sha(self.storage_index_raw) != descriptor["storage_index"]["sha256"]):
                    raise CheckpointError("Storage index differs from its pin")
                try:
                    self.storage_entries = artifact_storage.decode_index(self.storage_index_raw, self.directory)
                except ValueError as exc:
                    raise CheckpointError("Invalid artifact storage index") from exc
            self.payload_start, offset = HEADER.size + size + self.storage_index_bytes, 0
            for index, record in enumerate(self.directory["artifacts"]):
                pin = _artifact_pin(record)
                entry = (self.storage_entries[index] if self.packed else
                         {"mode": 0, "stored_bytes": record["bytes"], "offset": offset})
                self._records[pin["id"]] = pin, record["kind"], offset
                self._storage[pin["id"]] = dict(entry)
                offset += entry["stored_bytes"]
            if offset + self.storage_index_bytes != payload:
                raise CheckpointError("Artifact lengths differ from payload")
            budget.check()
            self.initialization_timing = budget.timing()
        except BaseException:
            self.close()
            raise

    @property
    def directory_pin(self):
        return dict(self.descriptor["directory"])

    def get(self, identifier):
        if self.fp is None:
            raise CheckpointError("Checkpoint reader is closed")
        if identifier == self.directory_pin["id"]:
            return self.directory_raw
        if identifier not in self._records:
            raise KeyError(identifier)
        pin, kind, offset = self._records[identifier]
        entry = self._storage[identifier]
        self.fp.seek(self.payload_start + offset)
        stored = self.fp.read(entry["stored_bytes"])
        self.counts["artifact_gets"] += 1
        self.counts["artifact_bytes_read"] += len(stored)
        self.counts["artifact_bytes_by_kind"][kind] = self.counts["artifact_bytes_by_kind"].get(kind, 0) + len(stored)
        if len(stored) != entry["stored_bytes"]:
            self.full_file_integrity = "UNCHECKED"
            raise CheckpointError("Truncated artifact storage payload")
        if self.packed:
            compressed = entry["mode"] == 1
            if compressed:
                self.counts["decompressions"] += 1
                start, cpu = time.monotonic(), time.process_time()
            try:
                raw = artifact_storage.decode_payload(stored, entry["mode"], pin["bytes"])
            except ValueError as exc:
                self.full_file_integrity = "UNCHECKED"
                raise CheckpointError("Invalid artifact storage payload") from exc
            finally:
                if compressed:
                    self.counts["decompression_wall_seconds"] += time.monotonic() - start
                    self.counts["decompression_cpu_seconds"] += time.process_time() - cpu
            self.counts["expanded_artifact_bytes"] += len(raw)
            self.counts["expanded_by_kind"][kind] = self.counts["expanded_by_kind"].get(kind, 0) + len(raw)
        else:
            raw = stored
        if len(raw) != pin["bytes"] or _sha(raw) != pin["sha256"]:
            self.full_file_integrity = "UNCHECKED"
            raise CheckpointError("Artifact hash/size mismatch")
        return raw

    def verify_all(self, *, max_seconds=60):
        budget = _Deadline(max_seconds)
        if self.fp is None:
            raise CheckpointError("Checkpoint reader is closed")
        self.full_file_integrity = "UNCHECKED"
        self.fp.seek(0)
        digest, total = hashlib.sha256(), 0
        while True:
            budget.check()
            raw = self.fp.read(65536)
            if not raw:
                break
            total += len(raw)
            self.counts["full_scan_bytes_read"] += len(raw)
            if total > self.descriptor["file_bytes"]:
                raise CheckpointError("Checkpoint grew")
            digest.update(raw)
        if total != self.descriptor["file_bytes"] or digest.hexdigest() != self.descriptor["file_sha256"]:
            raise CheckpointError("Full file hash mismatch")
        for identifier in self._records:
            budget.check()
            self.get(identifier)
        budget.check()
        self.full_file_integrity = "CHECKED_AT_VERIFICATION"
        return {"records_checked": len(self._records), "file_sha256": digest.hexdigest(),
                "integrity": self.full_file_integrity, "metrics": self.metrics(), **budget.timing()}

    def metrics(self):
        result = json.loads(_canonical(self.counts))
        result["total_file_bytes_read"] = sum(self.counts[k] for k in (
            "header_bytes_read", "directory_bytes_read", "artifact_bytes_read", "full_scan_bytes_read"))
        if self.packed:
            result["total_file_bytes_read"] += self.counts["index_bytes_read"]
            result["storage_transport"] = artifact_storage.info()
            result["expansion_scope"] = ("Expanded bytes count completed transport decodes, before logical hash verification; "
                                         "decompression attempts/timing include failures; failed inflater output is unavailable")
        result["full_file_integrity"] = self.full_file_integrity
        result["initialization_timing"] = dict(self.initialization_timing)
        result["file_read_scope"] = "Unbuffered application reads, not physical disk I/O"
        return result

    def close(self):
        if self.fp is not None:
            self.fp.close()
            self.fp = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def import_checkpoint(reader, store, *, max_seconds=60):
    budget = _Deadline(max_seconds)
    if type(reader) is not CheckpointReader or type(store) is not CrystalStore:
        raise CheckpointError("Explicit checkpoint reader and CrystalStore required")
    before = store.inspect()
    if before["sources"] or before["unique_nodes"] or before["lexical_unique_terms"]:
        raise CheckpointError("Import requires an empty store")
    reader.verify_all(max_seconds=budget.remaining())
    directory = _directory(reader, reader.directory_pin, budget)
    for record in directory["artifacts"]:
        pin = _artifact_pin(record)
        _put(store, pin, _get(reader, pin, budget), budget)
    _put(store, reader.directory_pin, reader.directory_raw, budget)
    if _directory(store, reader.directory_pin, budget) != directory:
        raise CheckpointError("Imported directory differs")
    store_bytes = store.inspect()["sqlite_bytes"]
    budget.check()
    return {"directory": reader.directory_pin, "records_copied": len(directory["artifacts"]) + 1,
            "checkpoint_integrity": reader.full_file_integrity, "reader_metrics": reader.metrics(),
            "store_bytes": store_bytes, "authority": "NONE", **budget.timing()}
