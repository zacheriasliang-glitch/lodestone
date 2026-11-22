"""Fixed declared local toolkit composition; registration is code-reviewed."""

TOOLKITS = {
    "bezoar.reconciliation/v1": {
        "input": "bezoar-reconciliation-source/v1; complete declared UTF-8 captured corpus, at most 65,536 encoded bytes",
        "interfaces": ["Board.ingest/eat/reconcile -> Plumber.reconcile_captured",
                       "Yggdrasil.check -> Bezoar registry coverage; independent retained-byte recomputation"],
        "output": "Inert reconciliation artifact, original input/outcomes, exact lineage and discrepancy coverage",
        "retained_routing": "Explicit --reuse-reconciliation pins latest same-corpus history in the approved plan; Plumber independently rechecks it and stores a routing proposal. No automatic dispatch or finding removal.",
        "dependencies": "Python>=3.10 standard library; local Bezoar/Yggdrasil/Pando source",
        "external_required": False,
        "side_effect_scope": "Private staging then controller publication; no view/posts/registry-state changes",
        "api_mapping": {"EAT": "ingestion/request", "RECONCILE": "captured-source interpretation/validation"},
        "activation": "Explicit host policy and exact task approval permit evidence storage only; activation NONE",
        "limits": "No arbitrary repository capture, missing predecessor invention, saved-verdict trust, repair or learning",
    },
    "bezoar.document/v1": {
        "input": "nurarihyon-document-source/v1 (bounded UTF-8 text and title)",
        "interfaces": ["Board.ingest/eat/digest -> Bezoar Plumber.ingest/eat_request/eat"],
        "output": "Exact literal parts, source-backed inert candidate and immutable lineage",
        "dependencies": "Python>=3.10 standard library; local machinery and Pando source",
        "external_required": False,
        "side_effect_scope": "Private staging; controller publication; existing view/posts unchanged",
        "api_mapping": {"EAT": "ingestion/request", "DIGEST": "structural interpretation/validation"},
        "activation": "Task approval permits storage only; candidate activation remains NONE",
    },
    "pando.profile/v1": {
        "input": "nurarihyon-profile-source/v1 (one explicit board-view change)",
        "interfaces": ["Engine.submit/run: literal_extract + mechanical audit",
                       "Board.ingest/eat/digest/mutate", "Board.apply_profile/verify"],
        "output": "Staged Pando generation, cited literal brief, immutable lineage and validation",
        "dependencies": "Python>=3.10 standard library; local machinery and Pando source",
        "external_required": False,
        "side_effect_scope": "Private staging directory only; publication belongs to controller",
        "api_mapping": {"EAT": "ingestion/request", "DIGEST": "interpretation/validation",
                        "MUTATE": "versioned transformation"},
        "activation": "Explicit host approval plus independent controller acceptance",
    }
}


TOOLKITS["bezoar.reconciliation-capture/v1"] = {
    "input": "bezoar-reconciliation-capture/v1 manifest and its verified sibling records; all declared physical files must be present",
    "interfaces": ["bezoar_capture.prepare", "Board.reconcile -> bezoar_capture.publish",
                   "Yggdrasil.check and Bezoar coverage recomputed independently from captured bytes"],
    "output": "Retained complete capture, exact lineage and discrepancy report; no registry repair",
    "dependencies": "Python>=3.10 standard library; user-owned Conscience binding; supplied source/spec",
    "external_required": False,
    "side_effect_scope": "Private staging then controller publication; board view and posts unchanged",
    "activation": "Explicit policy and exact task approval; evidence storage only",
    "limits": "Missing required physical history is refused; complete capture does not make historical claims true",
}
TOOLKITS["bezoar.reconciliation-observation/v1"] = {
    "input": "bezoar-reconciliation-observation-source/v1 manifest and verified sibling records; declared missing historical references stay unresolved",
    "interfaces": ["bezoar_observation.prepare", "Board.reconcile -> bezoar_capture.publish_observation",
                   "Independent physical integrity, Yggdrasil lineage and Bezoar discrepancy recomputation"],
    "output": "Immutable observation preserving discrepancies, unresolved references and explicit correction evidence",
    "dependencies": "Python>=3.10 standard library; user-owned Conscience binding; supplied source/spec",
    "external_required": False,
    "side_effect_scope": "Private staging then controller publication; no original, history, view or registry alteration",
    "activation": "Explicit policy and exact task approval; observation only, no repair or candidate activation",
    "limits": "Historical missing references are findings; missing declared physical files, corrupt bytes or invalid intent are refused",
}


def describe():
    return {"schema": "nurarihyon-registry/v1", "toolkits": TOOLKITS,
            "external_adapters": "Disabled; no cloud model, GitHub or Notion required",
            "executor_contract": "A replacement must consume pinned tasks and return independently validated staging; it cannot bypass publication policy."}
