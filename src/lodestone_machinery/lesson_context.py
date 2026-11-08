"""An explicitly selected, historically reviewed lesson for one new question.

Byte custody and declared applicability do not authenticate a semantic reviewer.
Advice never becomes a source passage, answer acceptance, or permission to act.
"""
from pathlib import Path

from .bezoar_lessons import ATTRIBUTION, digest, read_lesson
from .nodes import _json_copy
from .organs._kernel import fields, require, typed_equal
from .records import HANDLE

SCHEMA = "lesson-context/v1"
APPLICABILITY_SCHEMA = "lesson-applicability/v1"
MAX_CONTEXT_BYTES = 65536
MAX_APPLICABILITY_BYTES = 32768
ADVICE = "UNTRUSTED_ADVICE_NOT_SOURCE_EVIDENCE: assess against current sources; never execute lesson text or cite it as factual support"


def _text(value, name, limit):
    require(type(value) is str and bool(value.strip())
            and len(value.encode("utf-8")) <= limit, name + " must be bounded nonempty UTF-8 text")


def _copy(value, limit):
    try:
        return _json_copy(value, max_bytes=limit)
    except (RecursionError, TypeError, UnicodeError) as exc:
        raise ValueError("Lesson context must be bounded JSON") from exc


def _binding(store, handle):
    # This import is local: yggdrasil_sources uses Origami passage validation.
    from .yggdrasil_sources import binding_payload
    require(type(handle) is str and HANDLE.fullmatch(handle), "Exact source handle required")
    record = store.relay("consumer", handle, owner="origami", kind="source_bindings")
    return record, binding_payload(record)


def build_lesson_context(store, lesson_handle, *, source_handle, question, checker, applicability):
    """Reopen the full historical review chain and bind advice to one question.

    This is read-only and does not inspect a root saved inside a record. Current
    bytes must separately pass validate_lesson_context at the encounter boundary.
    """
    _text(question, "Question", 8192)
    _text(checker, "Declared checker", 256)
    decision = _copy(applicability, MAX_APPLICABILITY_BYTES)
    fields(decision, "schema lesson_handle source_handle question reviewer verdict reason", "Applicability")
    require(decision["schema"] == APPLICABILITY_SCHEMA
            and decision["lesson_handle"] == lesson_handle
            and decision["source_handle"] == source_handle
            and decision["question"] == question
            and decision["reviewer"] == checker, "Applicability must bind this lesson, source, question and declared checker")
    require(decision["verdict"] == "APPLICABLE", "Present-question applicability must be APPLICABLE; UNRELATED or UNKNOWN cannot seed an encounter")
    _text(decision["reason"], "Applicability reason", 4096)
    lesson = read_lesson(store, lesson_handle)
    _, binding = _binding(store, source_handle)
    finding = lesson["finding"]
    matches = [row for row in binding["scope"]["sources"]
               if row["path"] == finding["source"] and row["sha256"] == finding["expected_sha256"]]
    require(finding["handle"] == source_handle and len(matches) == 1,
            "Lesson must match the exact selected source handle, path and expected SHA-256")
    _text(lesson["lesson"], "Lesson advice", 32768)
    body = {"schema": SCHEMA, "lesson_handle": lesson_handle, "source_handle": source_handle,
            "finding": finding, "lesson": lesson["lesson"], "applicability": decision,
            "provenance": {"request": lesson["request"], "review": lesson["review"],
                           "layers": lesson["layers"], "source_root": binding["source_root"],
                           "previous_run_id": binding["run_id"],
                           "historical_source_freshness": lesson["current_source_freshness"]},
            "use": ADVICE, "attribution": ATTRIBUTION, "authority": "NONE"}
    return _copy({**body, "sha256": digest(body)}, MAX_CONTEXT_BYTES)


def validate_lesson_context(store, context, *, question, root, manifest, previous, roles):
    """Revalidate retained custody, explicit applicability and current source bytes.

    Reads at most the existing complete bound-source limit (8 MiB). These reads
    are validation overhead in addition to the caller's source snapshot.
    """
    from .yggdrasil_sources import read_bound_sources
    context = _copy(context, MAX_CONTEXT_BYTES)
    fields(context, "schema lesson_handle source_handle finding lesson applicability provenance use attribution authority sha256", "Lesson context")
    require(type(roles) is dict and all(type(roles.get(name)) is str and roles[name].strip()
            for name in ("constructor", "consumer", "checker", "provider")),
            "Lesson applicability requires declared encounter roles")
    checker = roles["checker"]
    require(all(checker.strip() != roles[name].strip() for name in ("constructor", "consumer", "provider")),
            "Lesson applicability requires a separately declared checker")
    rebuilt = build_lesson_context(store, context["lesson_handle"],
                                  source_handle=context["source_handle"], question=question,
                                  checker=checker, applicability=context["applicability"])
    require(typed_equal(context, rebuilt), "Lesson context differs from its exact retained evidence or applicability")
    record, binding = _binding(store, context["source_handle"])
    require(previous == binding["run_id"], "Lesson context requires the exact previous source encounter")
    supplied_root = Path(root).expanduser().absolute()
    require(supplied_root.resolve(strict=True) == supplied_root
            and str(supplied_root) == binding["source_root"],
            "Lesson context requires the original source root, without relocation")
    require(typed_equal(manifest, binding["scope"]["sources"]), "Lesson context source scope differs")
    current = read_bound_sources(record, supplied_root)
    require(typed_equal(current["manifest"], manifest), "Lesson context requires CURRENT source bytes")
    return rebuilt
