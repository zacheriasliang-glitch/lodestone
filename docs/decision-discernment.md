# Source-bound decision review

The ordinary Unified Loom response path records whether generation completed. A
completed response can still cite a different target, invent an action label or
contradict its own HOLD/CHANGE transition. `decision_contract.py` adds an opt-in
function to check those distinctions and return a separately attributed decision.
It does not change a model's answer or claim that a model learned the correction.

This is a function within existing Loom, Origami and Bezoar responsibilities, not
a 32nd organ. Existing Conscience checks practice delivery/admission; existing
Nurarihyon captures actual outcomes and interruptions. Bezoar retains this check
as an unresolved review. Garden can test this candidate with a fixed external
checker; a passing candidate is not automatically installed. Origami's typed
claims and separate support review remain appropriate for broader source-based
answers and competing interpretations. Maxwell's operational service-health
checks do not establish answer correctness.

## Contract and interpretation

`bind_decision_contract(encounter, case_json=..., author=..., policy=...)` binds
one exact JSON substring in the supplied encounter, with UTF-8 byte offsets and
source hashes. The case has exactly these fields:

- `case_id`, `target`, `recorded_prior_action` and `allowed_actions`;
- `rules`, a list of supplied rule strings;
- `observations`, each with `id`, integer `order`, `target`, boolean `verified`, and `text`.

This version fixes one decision convention: use the uniquely latest verified
observation for the named target and cite exactly that one observation. It does
not infer this convention from arbitrary prose. HOLD means the selected action
matches the supplied prior action; CHANGE means it differs. It is not a general
planner for multi-source evidence or unstructured requests.

The optional policy declares condition-to-action rules and observed conditions.
Each rule cites a complete supplied rule string; each observed condition cites
the complete text of its named observation. These exact quotes establish source
identity, **not** the truth of their interpretation. The contract author remains
explicitly responsible for the `DECLARED_INTERPRETATION`. No keywords, case IDs,
vessel names or concealed grading keys determine the action.

Without a policy, the function can detect structural mistakes, but cannot certify
which action the prose calls for. Missing or competing applicable conditions and
tied or absent latest observations stay unresolved. No fallback guesses an action.

```python
import json
from lodestone_machinery.decision_contract import bind_decision_contract, review_decision

case = {
    "case_id": "example", "target": "unit-A", "recorded_prior_action": "KEEP",
    "allowed_actions": ["KEEP", "REDIRECT"],
    "rules": ["Under condition alpha choose KEEP; under beta choose REDIRECT."],
    "observations": [
        {"id": "A1", "order": 1, "target": "unit-A", "verified": True,
         "text": "Condition alpha was observed for unit-A."},
        {"id": "B2", "order": 2, "target": "unit-B", "verified": True,
         "text": "Condition beta was observed for unit-B."},
    ],
}
case_json = json.dumps(case)
encounter = "Decide this supplied case:\n" + case_json
policy = {
    "rules": [
        {"condition": "alpha", "action": "KEEP", "source_quote": case["rules"][0]},
        {"condition": "beta", "action": "REDIRECT", "source_quote": case["rules"][0]},
    ],
    "observed_conditions": [
        {"observation_id": "A1", "condition": "alpha", "source_quote": case["observations"][0]["text"]},
        {"observation_id": "B2", "condition": "beta", "source_quote": case["observations"][1]["text"]},
    ],
}
contract = bind_decision_contract(encounter, case_json=case_json,
                                 author="Example curator", policy=policy)
raw_answer = '{"case_id":"example","action":"KEEP","transition":"CHANGE","evidence_ids":["B2"]}'
review = review_decision(encounter, raw_answer, contract)
assert review["status"] == "REJECTED"
assert review["derived_decision"] == {
    "case_id": "example", "action": "KEEP", "transition": "HOLD", "evidence_ids": ["A1"]
}
assert review["raw_response"] == raw_answer
```

The pure function performs no file, provider or native-memory operations. The
result separates `structural_consistency`, `condition_action_support`, reason
codes and the derived decision. PASS is only a scoped contract check under the
declared policy. An incomplete provider outcome always remains UNRESOLVED.

## Retain a check in the configured practice lifecycle

For an actual completed or interrupted native outcome, use
`UnifiedLoom.review_decision(outcome_handle, contract=contract, author=reviewer)`.
`LoomBridge.review_decision` forwards to its explicitly configured native memory.
The method reads the retained encounter and response itself, preventing a caller
from passing a replacement answer. It preserves the complete contract and review,
response identity and checker source digest through the existing Bezoar correction
path. An identical repeat reuses the review; it does not invent another event.

The native review remains UNRESOLVED even if the code check passes. No fold is
promoted, weight updated, model called, or default memory selection changed.
The full native feedback must fit the existing 8,192-byte correction bound;
oversized feedback is rejected before writing rather than silently shortened.

From a source checkout containing this change, save the bound contract to a JSON
file and supply the intended native configuration explicitly:

```sh
PYTHONPATH=desktop/runtime/src python3 -B -m lodestone_machinery.learning_cli \
  --config /path/to/learning/ACTIVE.json review-decision bezoar:OUTCOME_SHA256 \
  --contract ./decision-contract.json --author 'Local reviewer'
```

After a versioned runtime containing this change is installed, its `loom-practice`
launcher exposes the same subcommand. This change alone does not update installed
runtimes or add an automatic GUI response filter. Inspect the returned assessment;
a zero CLI exit means the review was retained, not that the original answer passed.

## Checks and limits

```sh
python3 -B -m unittest discover -s tests/desktop -p 'test_decision_contract.py' -v
python3 -B -m unittest discover -s tests/desktop -p 'test_decision_retention.py' -v
```

The tests include renamed labels, reordered observations, another-target
distractors, same-target reversals, ambiguous conditions, source drift, malformed
JSON, native outcome preservation, next-practice delivery and retry idempotency.
Providers in lifecycle fixtures are explicitly scripted. Testing old exposed
answers demonstrates detection of those mistakes, not held-out model improvement,
probability calibration, semantic learning or a weight update.
