"""Conscience's input obligation for explicitly configured learning practice.

This check grants no permission and does not claim that a reader understood its
input. It establishes that retained corrections, patterns and outcomes reach the
actual provider boundary, either in the complete preparation or its exact native
reader projection. On the adapter path a declared fold may let fields go, and past
that a declared departure may let whole older runs leave the view; both are named
in the input and in the admission record, which says when departed runs'
corrections are not present. Only explicitly listed original evidence bodies may
remain in the archive. The broker enforces the verdict before calling the provider.
"""
import hashlib

from lodestone_machinery.records import canonical
from .common import strict_json

MAX_PACKET_BYTES = 2 * 1024 * 1024


def _contains(value, expected, *, depth=0, budget=None):
    """Find an exact native preparation, including inside a JSON reference."""
    budget = [16384] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 24:
        raise ValueError("Learning packet exceeds structural inspection bound")
    if type(value) is dict:
        if canonical(value) == canonical(expected):
            return True
        return any(_contains(item, expected, depth=depth + 1, budget=budget)
                   for item in value.values())
    if type(value) is list:
        return any(_contains(item, expected, depth=depth + 1, budget=budget)
                   for item in value)
    if type(value) is str and value.lstrip().startswith(("{", "[")):
        try:
            parsed = strict_json(value.encode(), MAX_PACKET_BYTES)
        except (ValueError, UnicodeError):
            return False
        return _contains(parsed, expected, depth=depth + 1, budget=budget)
    return False


def _rule_folded_adapter_request(memory, checked, packet):
    """Return the rule's projection if an adapter request is exactly the deterministic fold rule's output.

    The fold (learning_context.FOLD_RULE) keeps every correction, verdict, condition and pattern
    exact and lets older run bodies and review details go from the view, naming them. Past that,
    DEPARTURE_RULE lets whole older runs (never the newest) leave the view, naming each; their
    corrections are then not in the input. Default requests admit the automatic rule's step
    and the 18 Nov fold (no departure), which keeps every run. An explicit history_window
    selects its declared number of newest fully folded runs instead. Each supported policy
    is independently rebuilt from the native records; arbitrary omissions or changed bytes
    are refused. Returns the expanded projection, or None.
    """
    import json
    from lodestone_machinery.learning_context import (DEPARTED_METHOD, FOLDED_METHOD, expand_learning_projection,
                                                       validate_practice_packet)
    from lodestone_machinery.loom_adapter import REQUEST_SCHEMA
    if packet.get("schema") != REQUEST_SCHEMA:
        return None
    try:
        validate_practice_packet(memory, checked, packet, "adapter")
        projection = expand_learning_projection(json.loads(packet["context"])["learning_context"])["projection"]
    except (ValueError, TypeError, KeyError):
        return None
    return projection if type(projection) is dict and projection.get("method") in (FOLDED_METHOD, DEPARTED_METHOD) else None


def assess(memory, prepared, packet):
    """Return a checked ALLOW/REFUSE contract verdict; never call a provider."""
    try:
        checked = memory.validate_for_dispatch(prepared)
        if canonical(checked) != canonical(prepared):
            raise ValueError("Preparation differs from its retained source")
        if type(packet) is not dict:
            raise ValueError("Provider input must be a JSON object")
        raw = canonical(packet)
        if len(raw) > MAX_PACKET_BYTES:
            raise ValueError("Learning provider packet exceeds 2 MiB")
        # An explicitly declared reader window is a contract even if literal
        # caller text also contains the complete preparation. Do not let the
        # generic full-delivery fast path bypass its exact source reconstruction.
        from lodestone_machinery.loom_adapter import REQUEST_SCHEMA
        saved_context = checked.get("context")
        saved_snapshot = strict_json(saved_context.encode(), MAX_PACKET_BYTES) if type(saved_context) is str else {}
        reference_history = type(saved_snapshot) is dict and saved_snapshot.get("schema") == "bezoar-learning-context/v2"
        reference = None
        if packet.get("schema") == REQUEST_SCHEMA and type(packet.get("context")) is str:
            try:
                reference = strict_json(packet["context"].encode(), MAX_PACKET_BYTES)
            except (ValueError, UnicodeError):
                reference = None
            if type(reference) is dict and "reader_history_window" in reference:
                from lodestone_machinery.learning_context import validate_practice_packet
                validate_practice_packet(memory, checked, packet, "adapter")
        if reference_history and not (type(reference) is dict and "reader_history_window" in reference):
            raise ValueError("Reference history requires an explicit validated adapter reader window")
        delivery, departed = "FULL_NATIVE_PREPARATION", []
        if reference_history or not _contains(packet, checked):
            # Local import keeps the projection builder independent of broker
            # admission. Recompute from the native immutable records; a caller
            # cannot make arbitrary omissions. Default selection responds to
            # literal-context size; an explicit reader window has its own fixed,
            # independently recomputed newest-run selection.
            from lodestone_machinery.learning_context import project_learning_context
            # A v2 inventory is not an embedded full-history snapshot. Its
            # explicit window was validated above and must be recomputed below;
            # do not try the legacy whole-history projection first.
            projected = None if reference_history else project_learning_context(memory, checked)
            if projected is not None and _contains(packet, projected):
                delivery = "EXACT_NATIVE_READER_PROJECTION"
            else:
                folded = _rule_folded_adapter_request(memory, checked, packet)
                if folded is None:
                    raise ValueError("Exact retained SEED/NACRE preparation or reader projection is absent from provider input")
                if folded.get("departed_history_runs"):
                    delivery, departed = "RULE_DEPARTED_NATIVE_READER_PROJECTION", list(folded["departed_history_runs"])
                else:
                    delivery = "RULE_FOLDED_NATIVE_READER_PROJECTION"
        # Reopen after packet inspection so a builder cannot replace records.
        if canonical(memory.validate_for_dispatch(prepared)) != canonical(checked):
            raise ValueError("Learning preparation changed before dispatch")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return {"schema": "conscience-learning-assessment/v1", "verdict": "REFUSE",
                "reason": str(exc), "authority": "NONE", "executed": False}
    return {"schema": "conscience-learning-assessment/v1", "verdict": "ALLOW",
            "run_handle": checked["run_handle"], "input_sha256": hashlib.sha256(raw).hexdigest(),
            "input_bytes": len(raw), "authority": "NONE", "executed": False,
            "context_delivery": delivery,
            "obligation": ("Source-bound corrections, verdicts, conditions and patterns are present; older prior-run bodies and review "
                           "details are let go from this view under the declared fold rule, named there, and recomputed from the native records"
                           if delivery == "RULE_FOLDED_NATIVE_READER_PROJECTION" else
                           (f"Source-bound seeds, NACREs, conditions and patterns are present, with every verdict and correction of the "
                            f"prior runs still in view; {len(departed)} older prior run(s) left this view under the declared departure "
                            f"rule and their verdicts and corrections are NOT in this input ({', '.join(departed)}); the native "
                            f"records keep them, and the fold is recomputed from them")
                           if delivery == "RULE_DEPARTED_NATIVE_READER_PROJECTION" else
                           "Source-bound corrections, conditions, patterns and prior outcomes are present; any archived evidence-body omission is explicit and exactly recomputed"),
            "scope": "Input delivery requirement; not permission, comprehension, semantic success or weight training"}
