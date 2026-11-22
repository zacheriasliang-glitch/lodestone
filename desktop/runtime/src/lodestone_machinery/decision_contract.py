"""Pure, opt-in checks for a source-bound structured decision.

This checks a declared task contract; it is not a natural-language interpreter.
The schema fixes latest verified observation for the named target, one decisive
citation, and HOLD iff the proposed action equals the recorded prior action.
Those conventions are selected by opting into this schema, not inferred from
arbitrary prose in the case's rules.
An optional author-supplied policy maps quoted conditions to actions. Matching
those quotes establishes source identity, not the truth of the interpretation.
The actual response is never repaired or replaced. No model, file, native memory,
permission, or promotion operation is performed here.
"""
import hashlib
import json
import math


CONTRACT_SCHEMA = "lodestone-decision-contract/v1"
REVIEW_SCHEMA = "lodestone-decision-review/v1"
MAX_ENCOUNTER_BYTES = 32768
MAX_CASE_BYTES = 16384
MAX_RESPONSE_BYTES = 8192
MAX_CONTRACT_BYTES = 65536
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_ROWS = 128
MAX_ORDER = 2**53 - 1


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _utf8(value):
    _require(type(value) is str, "Expected text")
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Text is not valid UTF-8") from exc


def _text(value, name, maximum, *, empty=False):
    raw = _utf8(value)
    _require((empty or bool(value.strip())) and len(raw) <= maximum,
             name + " must be nonempty bounded UTF-8 text")
    return value


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _inspect_json(value, *, depth=0, budget=None):
    budget = [MAX_NODES] if budget is None else budget
    budget[0] -= 1
    _require(budget[0] >= 0 and depth <= MAX_DEPTH, "JSON structural bound exceeded")
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            _utf8(key)
            _inspect_json(item, depth=depth + 1, budget=budget)
    elif kind is list:
        for item in value:
            _inspect_json(item, depth=depth + 1, budget=budget)
    elif kind is str:
        _utf8(value)
    elif kind is float:
        _require(math.isfinite(value), "Nonfinite JSON number")
    else:
        _require(value is None or kind in (int, bool), "Non-JSON value")


def _copy_json(value, maximum):
    _inspect_json(value)
    try:
        raw = _canonical(value)
    except (ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid bounded JSON value") from exc
    _require(len(raw) <= maximum, "JSON byte bound exceeded")
    return json.loads(raw)


def _parse_json(text, maximum):
    raw = _utf8(text)
    _require(len(raw) <= maximum, "JSON byte bound exceeded")
    # Bound nesting before the decoder allocates the nested object. Brackets in
    # strings and escaped quotes do not change structural depth.
    quoted = escaped = False
    depth = 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            _require(depth <= MAX_DEPTH, "JSON nesting bound exceeded")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite JSON number")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid strict JSON: " + str(exc)) from exc
    _inspect_json(value)
    return value


def _keys(value, expected, name):
    _require(type(value) is dict and set(value) == set(expected),
             name + " fields differ from the contract")


def _rows(value, name, *, minimum=0):
    _require(type(value) is list and minimum <= len(value) <= MAX_ROWS,
             name + " must be a bounded list")
    return value


def _case(case_json):
    case = _parse_json(case_json, MAX_CASE_BYTES)
    _keys(case, ("case_id", "target", "recorded_prior_action", "rules",
                 "observations", "allowed_actions"), "Case")
    for name in ("case_id", "target", "recorded_prior_action"):
        _text(case[name], name, 256)
    actions = _rows(case["allowed_actions"], "allowed_actions", minimum=1)
    for action in actions:
        _text(action, "Allowed action", 256)
    _require(len(actions) == len(set(actions)), "Duplicate allowed action")
    _require(case["recorded_prior_action"] in actions, "Prior action is not allowed")
    rules = _rows(case["rules"], "rules", minimum=1)
    for rule in rules:
        _text(rule, "Rule", 4096)
    _require(len(rules) == len(set(rules)), "Duplicate rule text")
    seen = set()
    for row in _rows(case["observations"], "observations"):
        _keys(row, ("id", "order", "target", "verified", "text"), "Observation")
        for name in ("id", "target"):
            _text(row[name], "Observation " + name, 256)
        _require(row["id"] not in seen, "Duplicate observation ID")
        seen.add(row["id"])
        _require(type(row["order"]) is int and 0 <= row["order"] <= MAX_ORDER,
                 "Observation order must be a bounded nonnegative integer")
        _require(type(row["verified"]) is bool, "Observation verification must be Boolean")
        _text(row["text"], "Observation text", 4096)
    return case


def _policy(policy, case):
    if policy is None:
        return None
    policy = _copy_json(policy, MAX_CONTRACT_BYTES)
    _keys(policy, ("rules", "observed_conditions"), "Policy")
    seen = set()
    for row in _rows(policy["rules"], "Policy rules"):
        _keys(row, ("condition", "action", "source_quote"), "Policy rule")
        _text(row["condition"], "Condition", 256)
        _require(type(row["action"]) is str and row["action"] in case["allowed_actions"],
                 "Policy action is not allowed")
        _require(type(row["source_quote"]) is str and row["source_quote"] in case["rules"],
                 "Policy rule quote must equal an actual complete case rule")
        identity = (row["condition"], row["action"], row["source_quote"])
        _require(identity not in seen, "Duplicate policy rule")
        seen.add(identity)
    observations = {row["id"]: row for row in case["observations"]}
    seen = set()
    for row in _rows(policy["observed_conditions"], "Observed conditions"):
        _keys(row, ("observation_id", "condition", "source_quote"), "Observed condition")
        _text(row["condition"], "Observed condition", 256)
        ident = row["observation_id"]
        _require(type(ident) is str and ident in observations,
                 "Policy cites an unknown observation")
        _require(row["source_quote"] == observations[ident]["text"],
                 "Observed condition quote must equal that observation's text")
        identity = (ident, row["condition"])
        _require(identity not in seen, "Duplicate observed condition")
        seen.add(identity)
    return policy


def bind_decision_contract(encounter, *, case_json, author, policy=None):
    """Bind one exact, unique JSON span in an encounter and an explicit policy.

    ``policy`` is either None or ``{rules: [{condition, action, source_quote}],
    observed_conditions: [{observation_id, condition, source_quote}]}``. Quotes
    must equal complete supplied rule/observation strings. The declared mapping
    is never inferred from those strings or treated as authenticated authority.
    """
    _text(encounter, "Encounter", MAX_ENCOUNTER_BYTES)
    _text(author, "Author", 512)
    case = _case(case_json)
    raw, selected = _utf8(encounter), _utf8(case_json)
    start = raw.find(selected)
    _require(start >= 0, "Case JSON is not an exact span of the encounter")
    _require(raw.find(selected, start + 1) < 0, "Case JSON span is not unique")
    contract = {
        "schema": CONTRACT_SCHEMA, "author": author,
        "encounter_sha256": _sha(raw), "encounter_bytes": len(raw),
        "case_span": {"start_byte": start, "end_byte": start + len(selected),
                      "sha256": _sha(selected)},
        "case_json": case_json, "policy": _policy(policy, case),
        "policy_attribution": "DECLARED_INTERPRETATION" if policy is not None else "NOT_SUPPLIED",
        "authority": "NONE", "semantic_learning": "NOT_ESTABLISHED",
    }
    contract["contract_sha256"] = _sha(_canonical(contract))
    return _copy_json(contract, MAX_CONTRACT_BYTES)


def _checked_contract(encounter, contract):
    saved = _copy_json(contract, MAX_CONTRACT_BYTES)
    _keys(saved, ("schema", "author", "encounter_sha256", "encounter_bytes", "case_span",
                  "case_json", "policy", "policy_attribution", "authority",
                  "semantic_learning", "contract_sha256"), "Contract")
    expected = bind_decision_contract(encounter, case_json=saved["case_json"],
                                      author=saved["author"], policy=saved["policy"])
    # Compare canonical bytes, not Python equality (where True equals 1).
    _require(_canonical(expected) == _canonical(saved),
             "Contract identity or exact encounter binding differs")
    return saved, _case(saved["case_json"])


def _latest(case):
    rows = [row for row in case["observations"]
            if row["target"] == case["target"] and row["verified"]]
    if not rows:
        return None, "NO_VERIFIED_TARGET_OBSERVATION"
    order = max(row["order"] for row in rows)
    latest = [row for row in rows if row["order"] == order]
    if len(latest) != 1:
        return None, "AMBIGUOUS_LATEST_TARGET_OBSERVATION"
    return latest[0], None


def _derive(case, contract, latest):
    policy = contract["policy"]
    if policy is None:
        return None, None, "CONDITION_POLICY_NOT_SUPPLIED"
    if latest is None:
        return None, None, None
    observed = [row for row in policy["observed_conditions"]
                if row["observation_id"] == latest["id"]]
    if not observed:
        return None, None, "OBSERVATION_CONDITION_UNDECLARED"
    if len(observed) != 1:
        return None, None, "OBSERVATION_CONDITION_AMBIGUOUS"
    rules = [row for row in policy["rules"] if row["condition"] == observed[0]["condition"]]
    if not rules:
        return None, None, "CONDITION_RULE_UNDECLARED"
    if len(rules) != 1:
        return None, None, "CONDITION_RULE_AMBIGUOUS"
    action = rules[0]["action"]
    decision = {"case_id": case["case_id"], "action": action,
                "transition": "HOLD" if action == case["recorded_prior_action"] else "CHANGE",
                "evidence_ids": [latest["id"]]}
    derivation = {"target": case["target"], "observation_id": latest["id"],
                  "condition": observed[0]["condition"], "action": action,
                  "rule_source_quote": rules[0]["source_quote"],
                  "observation_source_quote": observed[0]["source_quote"],
                  "author": contract["author"], "attribution": "DECLARED_INTERPRETATION"}
    return decision, derivation, None


def _answer(response, case):
    original = _parse_json(response, MAX_RESPONSE_BYTES)
    answer = original
    nested = type(original) is dict and "basis" in original
    if nested:
        _keys(original, ("basis", "case_id", "decision"), "Evidence-first response")
        basis = original["basis"]
        _keys(basis, ("id", "order", "target", "verified", "text"), "Response basis")
        _keys(original["decision"], ("action", "transition"), "Response decision")
        # Compare complete canonical row bytes, not dict equality: True/1 and
        # integer/float substitutions must not impersonate an exact source row.
        _require(any(_canonical(basis) == _canonical(row) for row in case["observations"]),
                 "Response basis differs from every exact source observation")
        answer = {"case_id": original["case_id"],
                  "action": original["decision"]["action"],
                  "transition": original["decision"]["transition"],
                  "evidence_ids": [basis["id"]]}
    _keys(answer, ("case_id", "action", "transition", "evidence_ids"), "Response")
    for name in ("case_id", "action", "transition"):
        _text(answer[name], "Response " + name, 256)
    ids = answer["evidence_ids"]
    _require(type(ids) is list and len(ids) == 1, "Exactly one evidence ID is required")
    _text(ids[0], "Response evidence ID", 256)
    return answer, original, nested


def review_decision(encounter, response, contract, *, completed=True):
    """Check an actual return; never generate or substitute an answer.

    Invalid contracts/changed encounter bindings raise ValueError. Invalid
    response text is REJECTED. An incomplete provider return is always UNRESOLVED.
    Missing or tied latest target evidence also remains UNRESOLVED; any definite
    response inconsistencies are still recorded in the checks and reason codes.
    PASS requires a unique explicitly declared condition/action derivation as
    well as structural consistency; it is only relative to that interpretation.
    An evidence-first response must copy one complete original observation. Its
    decision projection only selects supplied fields; it never derives an answer
    or treats an exact row copy as condition/action understanding.
    """
    _require(type(completed) is bool, "completed must be Boolean")
    contract, case = _checked_contract(encounter, contract)
    reasons = []
    latest, selection_error = _latest(case)
    if selection_error:
        reasons.append(selection_error)
    derived, derivation, policy_error = _derive(case, contract, latest)
    if policy_error:
        reasons.append(policy_error)

    # Native outcomes hold text or null. For malformed nontext JSON callers,
    # retain the value with an explicitly different digest encoding. Oversized
    # text is identified by digest/length instead of pretending it was embedded.
    capture_encoding = "UTF8_TEXT"
    if response is None:
        raw, saved_response, retention = None, None, "NULL"
    elif type(response) is str:
        try:
            raw = _utf8(response)
            saved_response = response if len(raw) <= MAX_RESPONSE_BYTES else None
            retention = "COMPLETE" if saved_response is not None else "OVERSIZE_HASH_ONLY"
        except ValueError:
            raw, saved_response, retention = None, None, "INVALID_UTF8_NOT_EMBEDDED"
    else:
        capture_encoding = "CANONICAL_NON_TEXT_JSON"
        try:
            saved_response = _copy_json(response, MAX_RESPONSE_BYTES)
            raw, retention = _canonical(saved_response), "NON_TEXT_JSON"
        except ValueError:
            raw, saved_response, retention = None, None, "INVALID_NON_TEXT_NOT_EMBEDDED"
    try:
        answer, parsed_response, nested = _answer(response, case)
    except ValueError:
        answer = None
        parsed_response, nested = None, False
        reasons.append("INVALID_RESPONSE_FORMAT")

    checks = {}
    if answer is not None:
        evidence = {row["id"]: row for row in case["observations"]}
        cited = evidence.get(answer["evidence_ids"][0])
        checks = {
            "case_id_matches": answer["case_id"] == case["case_id"],
            "action_allowed": answer["action"] in case["allowed_actions"],
            "transition_consistent": answer["transition"] == (
                "HOLD" if answer["action"] == case["recorded_prior_action"] else "CHANGE"),
            "citation_exists": cited is not None,
            "citation_verified": cited["verified"] if cited is not None else None,
            "citation_target_matches": cited["target"] == case["target"] if cited is not None else None,
            "citation_is_latest": cited["id"] == latest["id"] if cited is not None and latest is not None else None,
        }
        codes = ("CASE_ID_MISMATCH", "ACTION_NOT_ALLOWED", "TRANSITION_INCONSISTENT",
                 "UNKNOWN_EVIDENCE_ID", "UNVERIFIED_EVIDENCE", "EVIDENCE_TARGET_MISMATCH",
                 "EVIDENCE_NOT_LATEST")
        reasons.extend(code for key, code in zip(checks, codes) if checks[key] is False)
        structural = False if False in checks.values() else (
            None if None in checks.values() else True)
    else:
        structural = False
    support = None if answer is None or derived is None else answer["action"] == derived["action"]
    if support is False:
        reasons.append("ACTION_CONTRADICTS_DECLARED_CONDITION")
    if not completed:
        reasons.append("PROVIDER_INCOMPLETE")
        status = "UNRESOLVED"
    elif answer is None:
        status = "REJECTED"
    elif selection_error:
        # Missing/ambiguous target evidence cannot establish a unique decision.
        # Definite response inconsistencies remain visible in checks/reasons.
        status = "UNRESOLVED"
    elif structural is False or support is False:
        status = "REJECTED"
    elif structural is True and support is True:
        status = "PASS"
    else:
        status = "UNRESOLVED"
    result = {
        "schema": REVIEW_SCHEMA, "status": status, "reason_codes": reasons,
        "contract_sha256": contract["contract_sha256"],
        "encounter_sha256": contract["encounter_sha256"], "case_span": contract["case_span"],
        "case_id": case["case_id"], "author": contract["author"], "provider_completed": completed,
        "response_sha256": _sha(raw) if raw is not None else None,
        "response_bytes": len(raw) if raw is not None else None,
        "response_digest_encoding": capture_encoding, "response_retention": retention,
        "raw_response": saved_response, "parsed_response": parsed_response,
        "response_format_valid": answer is not None,
        "structural_consistency": structural, "structural_checks": checks,
        "condition_action_support": support, "policy_attribution": contract["policy_attribution"],
        "selected_evidence_id": latest["id"] if latest is not None else None,
        "derived_decision": derived, "derivation": derivation,
        "authority": "NONE", "semantic_learning": "NOT_ESTABLISHED",
        "scope": "Contract consistency under an attributed declared interpretation; not verified world truth, authenticated authorship, model learning or permission.",
    }
    if nested:
        result["response_format"] = "evidence-first/v1"
        result["decision_response"] = answer
    return result
