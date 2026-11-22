"""Could, would and should voxels for Loom practice; pure records, no model.

* could  -- readings offered as a tale: a balance over candidate readings, never
            a single label. The balance is kept so a tentative reading and a
            well-supported one stay distinguishable later.
* would  -- an expectation sealed before the outcome is shown: what each reading
            predicts would be observed. The seal (SHA-256 over canonical JSON)
            stops the record from being retold to fit the outcome afterwards.
* should -- a move derived from the *current* purpose: intention, act and an
            ``unless``. A should is never carried across a changed purpose.

When the outcome arrives, ``reveal`` checks the seal and either holds or names
the knot: what was expected, how strongly, and what came instead. A named knot
can be unpicked; an unnamed one gets woven over. An outcome outside every
offered reading is kept as ``UNLISTED`` rather than forced into one.

The voxel context renders the next encounter as readable sections (teaching,
named knots) instead of a JSON dump. It describes the situation; it never
commands an answer format. Records are evidence/advice, not authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone

from .records import canonical

WOULD_SCHEMA = "loom-would/v1"
SHOULD_SCHEMA = "loom-should/v1"
REVEAL_SCHEMA = "loom-reveal/v1"
CONTEXT_SCHEMA = "loom-voxel-context/v1"
UNLISTED = "UNLISTED"
MIN_READINGS, MAX_READINGS = 2, 8
MAX_TEXT_BYTES = 4096
MAX_KNOTS = 16
WEIGHT_TOLERANCE = 1e-6


def _text(value, name, limit=MAX_TEXT_BYTES):
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError(name + " must be bounded nonempty text")
    return value


def _id(value, name):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value) is None:
        raise ValueError(name + " must be a bounded stable ID")
    if value == UNLISTED:
        raise ValueError(name + " may not use the reserved UNLISTED identifier")
    return value


def _sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _purpose_key(purpose):
    return " ".join(_text(purpose, "purpose").split())


def could(readings):
    """Validate a balance over 2..8 readings; weights are positive and sum to one."""
    if type(readings) is not list or not MIN_READINGS <= len(readings) <= MAX_READINGS:
        raise ValueError("could holds 2..8 readings; holding everything is not a reading")
    seen, total, kept = set(), 0.0, []
    for row in readings:
        if type(row) is not dict or set(row) != {"id", "text", "weight"}:
            raise ValueError("each reading needs exactly id, text and weight")
        _id(row["id"], "reading.id")
        _text(row["text"], "reading.text")
        weight = row["weight"]
        if type(weight) not in (int, float) or isinstance(weight, bool) or not 0 < weight <= 1 \
                or not math.isfinite(weight):
            raise ValueError("reading weight must be in (0, 1]")
        if row["id"] in seen:
            raise ValueError("duplicate reading id")
        seen.add(row["id"]); total += weight
        kept.append({"id": row["id"], "text": row["text"], "weight": float(weight)})
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        raise ValueError("reading weights must sum to one: the balance is the point")
    return kept


def _leading(readings):
    top = max(row["weight"] for row in readings)
    leaders = [row["id"] for row in readings if row["weight"] == top]
    return leaders[0] if len(leaders) == 1 else None  # a tie leads nowhere


def seal_would(*, readings, predictions, purpose, sealed_at=None):
    """Seal what each reading predicts would be observed, before the outcome."""
    kept = could(readings)
    if type(predictions) is not dict or set(predictions) != {row["id"] for row in kept}:
        raise ValueError("every reading needs exactly one predicted observation")
    for key, value in predictions.items():
        _text(value, "prediction for " + key)
    body = {"schema": WOULD_SCHEMA, "readings": kept, "predictions": dict(predictions),
            "purpose": _text(purpose, "purpose"), "leading": _leading(kept),
            "sealed_at": sealed_at or _now()}
    return {**body, "seal": _sha(body)}


def verify_seal(would):
    if type(would) is not dict or would.get("schema") != WOULD_SCHEMA or "seal" not in would:
        raise ValueError("not a sealed would")
    body = {key: value for key, value in would.items() if key != "seal"}
    if _sha(body) != would["seal"]:
        raise ValueError("would was changed after sealing")
    could(body["readings"])
    return True


def reveal(would, *, observed, observation, revealed_at=None):
    """Hold, or name the knot. ``observed`` is a reading id or UNLISTED."""
    verify_seal(would)
    _text(observation, "observation")
    weights = {row["id"]: row["weight"] for row in would["readings"]}
    if observed != UNLISTED and observed not in weights:
        raise ValueError("observed must be an offered reading id or UNLISTED")
    leading = would["leading"]
    if observed == UNLISTED:
        status, weight, surprise = "KNOT", None, None
    else:
        weight = weights[observed]
        surprise = -math.log2(weight)
        status = "HELD" if leading is not None and observed == leading else "KNOT"
    record = {"schema": REVEAL_SCHEMA, "would_seal": would["seal"], "status": status,
              "observed": observed, "observation": observation,
              "observed_weight": weight, "surprise_bits": surprise,
              "revealed_at": revealed_at or _now()}
    if status == "KNOT":
        expected = leading if leading is not None else "TIE"
        record["knot"] = {
            "expected": expected,
            "expected_weight": weights.get(leading) if leading else None,
            "expected_observation": would["predictions"].get(leading) if leading else None,
            "came": observation,
            "outside_offered_readings": observed == UNLISTED}
    return record


def should(*, intention, act, unless, purpose, would_seal=None):
    """A move tied to the purpose it serves; ``unless`` names when to let go."""
    body = {"schema": SHOULD_SCHEMA, "intention": _text(intention, "intention"),
            "act": _text(act, "act"), "unless": _text(unless, "unless"),
            "purpose": _text(purpose, "purpose"), "would_seal": would_seal}
    if would_seal is not None and (type(would_seal) is not str
                                   or re.fullmatch(r"[0-9a-f]{64}", would_seal) is None):
        raise ValueError("would_seal must be a SHA-256 digest")
    return {**body, "should_sha256": _sha(body)}


def carry_should(prior, current_purpose):
    """Never copy a should across a changed purpose; return CURRENT or STALE."""
    if type(prior) is not dict or prior.get("schema") != SHOULD_SCHEMA:
        raise ValueError("not a should record")
    body = {key: value for key, value in prior.items() if key != "should_sha256"}
    if _sha(body) != prior.get("should_sha256"):
        raise ValueError("should record was changed")
    if _purpose_key(prior["purpose"]) == _purpose_key(current_purpose):
        return {"status": "CURRENT", "should": prior}
    return {"status": "STALE", "reason": "purpose changed; derive a new should from the current purpose",
            "prior_purpose": prior["purpose"], "current_purpose": current_purpose,
            "prior_should_sha256": prior["should_sha256"]}


def _knot_line(reveal_record):
    knot = reveal_record.get("knot")
    if reveal_record.get("schema") != REVEAL_SCHEMA or knot is None:
        raise ValueError("only named knots enter the next encounter")
    if knot["outside_offered_readings"]:
        return "Expected one of the offered readings; what came was outside them: " + knot["came"]
    if knot["expected"] == "TIE":
        return "Held a tie between readings; what came: " + knot["came"]
    return ("Expected: " + knot["expected_observation"] +
            " (weight " + format(knot["expected_weight"], ".2f") + "). What came: " + knot["came"])


def voxel_context(*, teaching, knots=(), shoulds=()):
    """Canonical context string for the next encounter: teaching, named knots and
    earlier shoulds. Whether an earlier should is current is decided at render time
    against the request's own purpose, never here."""
    _text(teaching, "teaching", 16384)
    knots, shoulds = list(knots), list(shoulds)
    if len(knots) > MAX_KNOTS or len(shoulds) > MAX_KNOTS:
        raise ValueError("too many knots or shoulds for one encounter")
    lines = [_knot_line(row) for row in knots]
    for row in shoulds:
        _check_should(row)
    return canonical({"schema": CONTEXT_SCHEMA, "teaching": teaching, "named_knots": lines,
                      "shoulds": shoulds}).decode("utf-8")


def _check_should(row):
    if type(row) is not dict or row.get("schema") != SHOULD_SCHEMA:
        raise ValueError("not a should record")
    body = {key: value for key, value in row.items() if key != "should_sha256"}
    if set(body) != {"schema", "intention", "act", "unless", "purpose", "would_seal"} \
            or _sha(body) != row.get("should_sha256"):
        raise ValueError("should record was changed")
    return row


def render_voxel_request(request):
    """Readable sections for a voxel context; None for any other context.

    Earlier shoulds pass through ``carry_should`` against this request's purpose:
    a CURRENT one is shown as current advice; a STALE one stays visible only as
    history, labelled as not current. Its act never enters the room as advice.
    """
    try:
        value = json.loads(request["context"])
    except (ValueError, TypeError, KeyError):
        return None
    if type(value) is not dict or value.get("schema") != CONTEXT_SCHEMA:
        return None
    # Contexts frozen before shoulds existed (no "shoulds" key) stay readable as
    # carrying no earlier advice, so their rendering does not change.
    keys = set(value)
    if (keys not in ({"schema", "teaching", "named_knots", "shoulds"}, {"schema", "teaching", "named_knots"})
            or type(value["named_knots"]) is not list or type(value.get("shoulds", [])) is not list
            or canonical(value).decode("utf-8") != request["context"]):
        raise ValueError("invalid voxel context")
    _text(value["teaching"], "teaching", 16384)
    current, history = [], []
    for row in value.get("shoulds", []):
        carried = carry_should(_check_should(row), request["purpose"])
        if carried["status"] == "CURRENT":
            current.append("- " + row["act"] + " (to " + row["intention"] + "; unless " + row["unless"] + ")")
        else:
            history.append("- For the earlier purpose \"" + row["purpose"].strip() + "\": " + row["act"])
    sections = ["Teaching:\n" + value["teaching"].strip()]
    if value["named_knots"]:
        sections.append("Named knots from earlier attempts:\n" +
                        "\n".join("- " + _text(line, "knot") for line in value["named_knots"]))
    if current:
        sections.append("Current advice for this purpose:\n" + "\n".join(current))
    if history:
        sections.append("Earlier advice, kept as history; its purpose has changed, so it is not "
                        "current advice:\n" + "\n".join(history))
    sections.append("Encounter:\n" + request["encounter"].strip())
    sections.append("Purpose:\n" + request["purpose"].strip())
    return "\n\n".join(sections)
