# Nurarihyon: approved local toolkit execution

Nurarihyon provides a durable queue, exact-plan approval, a bounded worker and
an independent acceptance controller. Pando holds the published board, source
captures and artifacts. The worker writes a private stage; the controller checks
that stage against the source and previous generation before publishing it.
No model account, private laboratory corpus or Git connection is required.

Start with the [quickstart](quickstart.md). Python 3.10+ and macOS or Linux are
required for these POSIX process and locking controls. Native Windows fails
clearly; the older Windows doctor/demo/Conscience preview remains separate.

## Supported contracts

`python3 -B lodestone.py nurarihyon registry` lists these actual interfaces:

| Toolkit | Input and operation | Preserved boundary |
|---|---|---|
| `pando.profile/v1` | Exact baseline and one requested board-name or ordered-shelf change; source-backed brief and mutation history. | Changes one approved view operator; no arbitrary code or model-generated execution. |
| `bezoar.document/v1` | Bounded UTF-8 document envelope; captures source, EAT request and literal DIGEST parts. | Complete text reconstructs exactly; no semantic truth, instruction adoption or activation. |
| `bezoar.reconciliation/v1` | An inline captured corpus with receipt, registry and selected Origami-result bytes. | Recomputes Yggdrasil lineage and Bezoar findings; does not repair or erase disagreements. |
| `bezoar.reconciliation-capture/v1` | A manifest plus authenticated immutable record bundle for the declared complete corpus. | Verifies all declared physical dependencies and source bindings; strict missing-evidence refusal. |
| `bezoar.reconciliation-observation/v1` | Captured physical evidence plus explicitly selected historical correction declarations. | Missing historical references and disagreements remain unresolved; declarations do not become corrections or proof. |

Reconciliation checks saved Origami result records and their source bindings.
It does not run a semantic model, establish mechanistic interpretability, or
validate an Origami claim by repeating it. An observation can complete correctly
while reporting unresolved findings. `COMPLETED` means its declared operation
passed; it does not mean all findings are resolved or all programme goals met.
The [31-component map](capability-map.md) preserves the wider scope and gaps.

## User-owned policy

Plain `init` enables only the profile route. To enable other implemented routes,
prepare a new configuration and initialize a new dedicated state:

```sh
STATE="$HOME/lodestone-data/reconciliation-state"
CONFIG="$HOME/lodestone-data/reconciliation-config"
python3 -B lodestone.py nurarihyon configure --output "$CONFIG" \
  --state "$STATE" --profile all
python3 -B lodestone.py nurarihyon --home "$STATE" init --policy "$CONFIG/nurarihyon-policy.json"
```

The profiles are `profile`, `documents`, `reconciliation`, `capture`, `observation` and
`all`. Review the generated policy before initialization. Reconciliation profiles
include an explicit Conscience binding to the selected user paths and pinned
policy/modules. Imported data cannot register toolkits, change the policy or
approve itself. There are no inherited laboratory identities or grants.
Keep configuration, state, package, capture output and recovery output separate.
Policy identity is fixed when the state is initialized; editing it later makes
verification fail. There is no silent in-place policy migration.

Conscience's command and typed-effect assessment is an additional admission
check for reconciliation. Non-ALLOW decisions hold the task; they do not start
its worker. The controller still enforces exact approval, identity, STOP, locks,
limits and independent acceptance. This is application control, not a universal
OS sandbox or protection against trusted account code. See
[Conscience](conscience.md#nurarihyon-binding).

## Propose, inspect, approve and execute

The quickstart generates synthetic profile, document and reconciliation inputs.
For inline reconciliation, use that generated directory:

```sh
python3 -B lodestone.py nurarihyon --home "$STATE" submit \
  --source "$HOME/lodestone-data/example-inputs/reconciliation.json" \
  --toolkit bezoar.reconciliation/v1 --key first-reconciliation
python3 -B lodestone.py nurarihyon --home "$STATE" status --task TASK_ID
python3 -B lodestone.py nurarihyon --home "$STATE" approve TASK_ID --plan-sha PLAN_SHA256
python3 -B lodestone.py nurarihyon --home "$STATE" run
python3 -B lodestone.py nurarihyon --home "$STATE" status --task TASK_ID
python3 -B lodestone.py nurarihyon --home "$STATE" verify
```

Replace task/hash placeholders only after inspecting the exact returned plan.
For a second inline reconciliation, `submit --reuse-reconciliation` explicitly
pins the latest retained result from the same named corpus and records inert
routing information. It still needs a new exact approval and recomputation.
No route runs automatically; this fixed selection is not learned skill choice.

For the captured contracts, prepare the synthetic corpus with an explicit root
and manifest. These commands call the existing `bezoar_capture.prepare` and
`prepare_observation` implementations and create inert source bundles only:

```sh
INPUTS="$HOME/lodestone-data/example-inputs"
python3 -B lodestone.py nurarihyon capture-corpus --root "$INPUTS/corpus" \
  --spec "$INPUTS/capture-spec.json" --output "$HOME/lodestone-data/capture.json"
python3 -B lodestone.py nurarihyon capture-corpus --root "$INPUTS/corpus" \
  --spec "$INPUTS/observation-spec.json" --output "$HOME/lodestone-data/observation.json" \
  --observation
```

Submit `capture.json` with `--toolkit bezoar.reconciliation-capture/v1`, or
`observation.json` with `--toolkit bezoar.reconciliation-observation/v1`. Use a
new key, inspect the returned plan and approve that exact task before `run`.
Each preparation destination must be new. Its JSON and sibling record bundle
travel together. Every selected path in the specification has an exact byte count
and SHA-256; `receipt_inventory` names the entire declared captured receipt set.
Observation also declares `correction_evidence_paths`. A historical reference can
remain missing and unresolved, while a missing physical captured file is an error.
The controller verifies the captured dependency closure; it never searches an
unselected repository to fill a missing file. See
`src/lodestone_machinery/bezoar_capture.py` for the full bounded format.

Profile source is strict JSON, at most 4,096 bytes, and must change exactly one
operator. Document envelopes are at most 65,536 bytes, including JSON expansion,
with at most 32 literal parts. Inline reconciliation has its own 65,536-byte
contract. Complete capture admits at most 192 files, 160 receipts and 4 MiB of raw
UTF-8 source, subject to all additional stage/state limits. These are bounded
operations, not unlimited corpus ingestion.

`run` attempts at most one approved queued task. Priority is the explicit integer
0–9, with stable submission order for ties; it is not a learned ranking. Approval
binds source, plan, base generation, code and policy. The same key with the same
input returns the existing task; changed intent under that key refuses. Stale
plans require a new key and approval, never an automatic reinterpretation.

## Retained outcomes and context

Completed operations retain artifacts and event/generation history in Pando.
The separate outcome-memory bridge currently supports **profile tasks only**:

```sh
STATE="$HOME/lodestone-data/profile-state"
python3 -B lodestone.py nurarihyon --home "$STATE" remember PROFILE_TASK_ID \
  --memory "$HOME/lodestone-data/profile-outcomes"
python3 -B lodestone.py nurarihyon --home "$STATE" context \
  --source /absolute/path/to/next-profile.json \
  --memory "$HOME/lodestone-data/profile-outcomes"
```

The later profile source must use the current board baseline. Use an absolute,
dedicated memory path outside the package, controller state and other stores.
The bridge independently verifies terminal history and matching captured sources.
Selection is fixed by toolkit/operator/source and event order. `submit --memory`
can attach verified context to a profile proposal; it does not change the plan,
priority, approval, limits or execution. The catalogue is a separate backup
dependency. Neither storage nor retrieval demonstrates useful retained learning.
The previous tied and negative learning evaluations remain negative; rejected
selectors and frozen studies are not production features.

## STOP, failures and limits

```sh
python3 -B lodestone.py nurarihyon --home "$STATE" stop
python3 -B lodestone.py nurarihyon --home "$STATE" status
python3 -B lodestone.py nurarihyon --home "$STATE" verify
```

STOP prevents new publication; it does not undo completed work. A running worker
is cancelled under the controller's existing checks. Inspect its terminal state
before recovery or export. Integrity/capacity refusals leave admission unconsumed;
interrupted attempts and failed stages remain history. `recover` verifies state
and marks unfinished tasks interrupted, without replaying them.

`clear-stop` and `retry TASK_ID` are separate explicit controls. They are never
called during installation, startup preparation or restore. Inspect and resolve
the reason for a stop before choosing them. A changed source, policy, code or
baseline requires a new valid plan; retry is not a way to override those checks.

Default policy limits include a 10-second worker CPU budget, 20-second worker
wall budget, 20-second supervisor wall budget, 256 MiB sampled worker memory,
8 MiB stage, 64 MiB state, 128 tasks and three explicitly requested attempts.
Captured-evidence profiles can declare the existing 16 MiB stage allowance.
The actual initialized policy is authoritative. Memory sampling can miss brief
peaks; process/account checks are not a hard system-wide memory sandbox.
The state has finite capacity and no automatic evidence deletion or compaction.

Durability covers accepted generation/history and process-interruption handling.
It is not a certification of sudden power-loss or all-filesystem recovery.
See [selected-state export/recovery](nurarihyon-recovery.md) and
[optional service startup](nurarihyon-service.md).
