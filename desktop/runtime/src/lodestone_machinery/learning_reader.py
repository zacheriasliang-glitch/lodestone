"""A reader view of validated practice, with opaque identities kept by the host.

The complete packet is still admitted and retained. This view preserves every
field of its selected projection, but does not ask a model to read digest bytes.
Explicit upstream fold/departure omissions are preserved and are not recovered.
It is not a replacement archive, a summary, or a semantic-learning claim.
"""
from copy import deepcopy
import json
import re

from .records import HANDLE, canonical

MODEL_VIEW_SCHEMA = "bezoar-learning-model-view/v1"
SECTION_VIEW_SCHEMA = "bezoar-learning-section-view/v1"
SECTION_REFERENCE_NOTE = (
    "Only binding/content encounter and purpose use section_ref objects: each names "
    "the exact UTF-8 string in the ENCOUNTER or PURPOSE section of this input. "
    "Expand those four fields to recover the original view. Other fields remain exact; "
    "literal reference-looking content is not a reference."
)
JSON_RESPONSE_FORMAT = "canonical-json-with-optional-final-newline/v1"
IDENTITY_NOTE = (
    "Whole identity reference values name exact digests in the validated host packet. "
    "Equal references mean equal identities; digest bytes are not shown here. "
    "All fields of this selected projection remain; declared fold/departure omissions still apply. "
    "This is a reader view, not the archive."
)


def section_view(view, *, encounter, purpose):
    """Refer to exact current sections without dropping any selected-view field.

    References exist only at four explicitly named structural paths. A matching
    object/string in historical prose, an outcome or a correction stays literal.
    Native packets and their retained evidence are never modified by this view.
    """
    if (type(view) is not dict or type(view.get("schema")) is not str
            or view["schema"] == SECTION_VIEW_SCHEMA or "section_references" in view
            or type(encounter) is not str or type(purpose) is not str):
        raise ValueError("Invalid source for exact section references")
    result = deepcopy(view)
    for parent in ("binding", "content"):
        if type(result.get(parent)) is not dict:
            raise ValueError("Section references need explicit binding and content")
        for field, text in (("encounter", encounter), ("purpose", purpose)):
            if result[parent].get(field) != text:
                raise ValueError("Section reference differs from the bound current text")
            result[parent][field] = {"section_ref": field.upper()}
    result["schema"] = SECTION_VIEW_SCHEMA
    result["section_references"] = {"source_schema": view["schema"], "use": SECTION_REFERENCE_NOTE}
    if expand_section_view(result, encounter=encounter, purpose=purpose) != view:
        raise ValueError("Exact section reference roundtrip differs")
    return result


def expand_section_view(view, *, encounter, purpose):
    """Reconstruct exactly the declared four fields; never recurse into prose."""
    if (type(view) is not dict or view.get("schema") != SECTION_VIEW_SCHEMA
            or type(encounter) is not str or type(purpose) is not str):
        raise ValueError("Invalid section-reference view")
    metadata = view.get("section_references")
    if (type(metadata) is not dict or set(metadata) != {"source_schema", "use"}
            or type(metadata["source_schema"]) is not str
            or metadata["source_schema"] == SECTION_VIEW_SCHEMA
            or metadata["use"] != SECTION_REFERENCE_NOTE):
        raise ValueError("Invalid section-reference declaration")
    restored = deepcopy(view)
    restored.pop("section_references")
    restored["schema"] = metadata["source_schema"]
    for parent in ("binding", "content"):
        if type(restored.get(parent)) is not dict:
            raise ValueError("Missing section-reference parent")
        for field, text in (("encounter", encounter), ("purpose", purpose)):
            if restored[parent].get(field) != {"section_ref": field.upper()}:
                raise ValueError("Unknown or misplaced current-section reference")
            restored[parent][field] = text
    return restored


def model_view(projected, *, _parse_responses=True):
    """Return the model view and its host-only identity table deterministically.

    Only supported, canonical, fully reversible JSON response strings become
    objects, with an explicit encoding declaration. Other responses stay exact
    strings. Neither literal caller context nor prose is rewritten. The table
    remains derivable from the exact admitted packet and is not model input.
    """
    from .learning_context import PROSE_FIELDS, _history_codec, expand_learning_projection
    expanded = expand_learning_projection(projected)
    transformed = deepcopy(expanded)
    structured_responses = set()
    for index, row in enumerate(transformed["content"]["history"]):
        outcome = row.get("outcome", {})
        response = outcome.get("response")
        if type(response) is not str or not _parse_responses:
            continue
        try:
            parsed = json.loads(response)
            raw = canonical(parsed).decode("utf-8")
        except (ValueError, TypeError, RecursionError):
            continue
        # Exact regeneration rejects duplicate keys, lossy numeric/whitespace
        # normalization, trailing data and unsupported JSON string formatting.
        if type(parsed) is dict and response in (raw, raw + "\n"):
            outcome["response"] = {
                "encoding": JSON_RESPONSE_FORMAT,
                "final_newline": response.endswith("\n"),
                "value": parsed,
            }
            structured_responses.add(("content", "history", index, "outcome", "response"))

    literals, identities = set(), set()

    def scan(value, *, prose=False, path=()):
        if path in structured_responses:
            prose = False
        if type(value) is dict:
            literals.update(value)
            for key, item in value.items():
                scan(item, prose=prose or key in PROSE_FIELDS, path=path + (key,))
        elif type(value) is list:
            for index, item in enumerate(value):
                scan(item, prose=prose, path=path + (index,))
        elif type(value) is str:
            literals.add(value)
            digest_field = path and type(path[-1]) is str and path[-1].endswith("_sha256")
            if not prose and (HANDLE.fullmatch(value) or
                              digest_field and re.fullmatch(r"[0-9a-f]{64}", value)):
                identities.add(value)

    scan(transformed)
    prefix = "R"
    while any(prefix + str(index + 1) in literals for index in range(len(identities))):
        prefix += "R"
    table = {prefix + str(index + 1): identity for index, identity in enumerate(sorted(identities))}
    aliases = {identity: alias for alias, identity in table.items()}

    def replace(value, *, prose=False, path=()):
        if path in structured_responses:
            prose = False
        if type(value) is dict:
            return {key: replace(item, prose=prose or key in PROSE_FIELDS, path=path + (key,))
                    for key, item in value.items()}
        if type(value) is list:
            return [replace(item, prose=prose, path=path + (index,)) for index, item in enumerate(value)]
        digest_field = path and type(path[-1]) is str and path[-1].endswith("_sha256")
        return (aliases.get(value, value) if type(value) is str and not prose and
                (HANDLE.fullmatch(value) or digest_field) else value)

    view = replace(transformed)
    view["schema"] = MODEL_VIEW_SCHEMA
    view["identity_references"] = {"prefix": prefix, "count": len(table), "use": IDENTITY_NOTE}
    try:
        view["content"]["history"], view["history_encoding"] = _history_codec(view["content"]["history"])
    except ValueError:
        # A valid response string may contain JSON deeper/larger than the
        # structured history bound. Keep its original bytes in that case.
        if structured_responses:
            return model_view(projected, _parse_responses=False)
        raise
    return view, table
