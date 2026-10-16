"""Two-pass provider transport for the new pressure-pair contract v0.1.0.

No model is configured here. Caller-supplied providers implement role reasoning;
this module isolates payloads and preserves their outputs. Historical White v3
and Black v2.1 pair identifiers are incompatible and are not adopted implicitly.
Providers are trusted Python callables, not sandboxed code. The budget bounds
payload bytes and call count (at most four), not provider execution time.
"""
from __future__ import annotations

from typing import Any, Callable

from .nodes import MAX_PAYLOAD_BYTES, _json_copy


CONTRACT_VERSION = "0.1.0"
Provider = Callable[..., Any]


def run_pair(board: dict, black_provider: Provider | None, white_provider: Provider | None,
             *, max_payload_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """Call independent initial passes, then cross-read actual initial outputs.

    Provider keyword protocol::

        provider(phase, board, own, counterpart, role, contract_version)

    For ``phase='initial'``, own and counterpart are None. For ``cross_read``,
    each is the captured initial response from that role or its opposite. Every
    argument is an isolated JSON copy. Invalid input raises ValueError. Missing,
    throwing, or non-JSON providers produce UNKNOWN with captured diagnostics.

    COMPLETE means all four exchanges completed, never that either reasoning
    output is true. Text/structure differences are retained without adjudication.
    """
    if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 4_194_304:
        raise ValueError("max_payload_bytes must be an integer from 1024 to 4194304")
    frozen = _json_copy(board, max_payload_bytes)
    if type(frozen) is not dict:
        raise ValueError("board must be a JSON object")
    providers = {"black": black_provider, "white": white_provider}
    result = {
        "contract_version": CONTRACT_VERSION,
        "status": "UNKNOWN",
        "semantic_verdict": "UNRESOLVED",
        "board": frozen,
        "initial": {},
        "cross_read": {},
        "response_differences": [],
        "limits": {"max_payload_bytes": max_payload_bytes, "max_provider_calls": 4,
                   "provider_timeout_enforced": False},
        "interpretation": "Transport completion only; provider reasoning and disagreements remain unadjudicated.",
    }

    def invoke(role: str, phase: str, own: Any = None, counterpart: Any = None) -> dict:
        provider = providers[role]
        if not callable(provider):
            return {"status": "UNKNOWN", "error": "PROVIDER_NOT_CONFIGURED"}
        try:
            payload = _json_copy({"phase": phase, "board": frozen, "own": own,
                                  "counterpart": counterpart, "role": role,
                                  "contract_version": CONTRACT_VERSION}, max_payload_bytes)
            response = _json_copy(provider(**payload), max_payload_bytes)
            return {"status": "RETURNED", "response": response}
        except Exception as exc:
            # Exception text can contain provider secrets; preserve type, not raw text.
            return {"status": "UNKNOWN", "error": "PROVIDER_OR_PAYLOAD_FAILED",
                    "exception_type": type(exc).__name__[:128]}

    # Both first-pass contexts are frozen before any cross-read is possible.
    for role in ("black", "white"):
        result["initial"][role] = invoke(role, "initial")
    if all(item["status"] == "RETURNED" for item in result["initial"].values()):
        black = result["initial"]["black"]["response"]
        white = result["initial"]["white"]["response"]
        result["cross_read"]["black"] = invoke("black", "cross_read", black, white)
        result["cross_read"]["white"] = invoke("white", "cross_read", white, black)
        if all(item["status"] == "RETURNED" for item in result["cross_read"].values()):
            result["status"] = "COMPLETE"
    else:
        result["cross_read"] = {role: {"status": "UNKNOWN", "error": "INITIAL_COUNTERPART_UNAVAILABLE"}
                                for role in ("black", "white")}

    for phase in ("initial", "cross_read"):
        sides = result[phase]
        if all(side["status"] == "RETURNED" for side in sides.values()):
            # Canonical JSON comparison distinguishes e.g. true from numeric 1.
            import json
            left = json.dumps(sides["black"]["response"], sort_keys=True, allow_nan=False)
            right = json.dumps(sides["white"]["response"], sort_keys=True, allow_nan=False)
            if left != right:
                result["response_differences"].append({
                    "phase": phase, "status": "UNRESOLVED",
                    "kind": "STRUCTURAL_OR_TEXT_DIFFERENCE",
                    "black_response_path": f"{phase}.black.response",
                    "white_response_path": f"{phase}.white.response",
                    "interpretation": "Different outputs; no automatic semantic disagreement or resolution inferred.",
                })
    # The aggregate deliberately has a larger budget than each exchange.
    return _json_copy(result, max_payload_bytes * 6 + 8192)
