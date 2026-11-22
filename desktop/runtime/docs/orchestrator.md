# Orchestrator: plans, handoffs and evidence

The public orchestrator packages the lab's deterministic Kernel 20 planner,
legacy Python planner and Kernel 21 attachment, transport proposal, return
verification and lifecycle-evidence helpers. All Python code uses the standard
library. No command calls a model, starts an agent, sends a packet, accesses a
network service or executes a proposed repository task.

## Try a fresh project

From the extracted Lodestone directory:

```sh
cp -R orchestrator/examples/project ./my-project
python3 -B orchestrator/bootstrap.py --root ./my-project --experiment 001 --goal "Audit the demonstration" --dry-test
python3 -B orchestrator/bootstrap.py --root ./my-project --experiment 001 --goal "Audit the demonstration"
```

`--root` is required. It names the project being observed and the record
workspace: new files go only under `ROOT/orchestrator/output/`. The installation
itself is not the default project. The example is synthetic and carries no
historical verdicts, credentials or host permissions. A dry test produces no
planning files. Ordinary planning writes a JSON decision and Markdown packet,
prints both paths and hashes, then stops. Existing packets are not overwritten.

Your own project should have `CURRENT_STATE.md` and exactly one directory named
`experiments/NNN-description` for the numeric selector. Optional source metadata
lives in `sources/experiment-NNN/{SOURCE_MANIFEST.json,SOURCE_DECLARATION.md,SHA256SUMS.txt}`;
receipts live under `receipts/`. Missing inputs remain explicit in the decision.
The planner observes metadata presence and filenames; it does **not** verify a
research result or validate the contents of those source manifests. Receipt
filenames and state keywords are routing heuristics, not proof of completion.

The default policy is [orchestrator.config.json](../orchestrator/config/orchestrator.config.json).
Supply `--config ./my-policy.json` to use your own. A saved `repositoryRoot`
setting is ignored: the explicit `--root` always controls the project. The
policy contains no inherited authorizations. Listed protections are advisory
routing checks, not an OS sandbox. Output symlinks escaping the project are
rejected. Use only projects you intend to inspect; this is not an untrusted-file
sandbox. Bounds limit recorded filenames, receipts and planning steps; directory
enumeration and reading the current-state file can still cost more than those
limits on large trees.

## Engines and bridge

| Component | Interface | Behavior |
|---|---|---|
| Kernel 20 | default `--engine kernel20` | Emits a deterministic proposal and decision identity; routes are BUILDER, AUDITOR, HOST_APPROVAL or STOP |
| Legacy Python | `--engine legacy` | Original read-plan-write packet format; explicit experiment selector, no task execution |
| Reconciliation | `--reconcile --packet PATH --receipt PATH` | Reads a prior decision in `orchestrator/output/` and a receipt in `receipts/`; reports CLOSE, RECALIBRATE, AUDIT, HOST_APPROVAL or UNKNOWN |
| Kernel 21 preflight | `--bridge-preflight` with experiment and goal | Writes attachment, transport-plan and task-envelope JSON; delivery remains a proposal |
| Return evidence | `--verify-return --envelope PATH --receipt PATH --attachment-contract PATH --transport-plan PATH` | Compares a returned receipt against the saved carrier and current project body |
| Lifecycle evidence | `--create-lifecycle-witness` / `--verify-lifecycle-witness` | Optional project-specific append-only custody records; no execution |

Run bridge preflight from inside the project to observe a matching execution
root. For example, substitute the absolute path to your installed script:

```sh
cd my-project
python3 -B /absolute/path/to/lodestone/orchestrator/bootstrap.py --root . --bridge-preflight --experiment 001 --goal "Audit the demonstration"
```

`DIRECT_FILESYSTEM`, `GIT`, `MCP` and `API` are proposed transport labels, not
implemented background connections. `UNKNOWN` capabilities are not upgraded by
installing this package. The bridge checks current paths and declared evidence;
its hashes detect changed bytes and do not authenticate a human's approval.
For verification and reconciliation, read the printed classification. A
successful CLI invocation alone is not a COMPLETE/CLOSE classification.

Lifecycle witnesses require a project's own `LABORATORY_CONSTITUTION.md`,
`INTERACTION_PROTOCOL.md`, an inert source containing the two lines
`AUTHORITY               = NONE` and `EXECUTABLE              = FALSE`, an exact
decision, and matching access-point and approval JSON records. No sample approval
or historical canon is shipped. This remains an advanced, explicitly configured
host integration. See [witness.py](../orchestrator/bridge/witness.py),
[the schemas](../orchestrator/schemas/lifecycle_witness.schema.json) and synthetic
[contract tests](../orchestrator/tests/test_kernel21_bridge.py) for exact fields.
"Commit" here means writing the manifest last, not a Git commit.

## Export inventory and limits

Included: `bootstrap.py`, the portable `run` wrapper, all Python `engine/` and
`bridge/` modules, all seven JSON schemas, neutral configuration, synthetic
examples and adapted regression tests. Schemas load from the installed package,
so an observed project does not need to copy implementation files into itself.

Excluded historical state: lab `output/`, `blink-tests/`, `state/`, receipts,
experiment results, private role instructions, canonical source documents and
host paths/grants. Two original tests asserted private role/canon document text;
those assertions are not public runtime tests. All runtime mechanism tests use
synthetic inputs; no historical trial is replayed.

The older Node/Responses API prototype (`agent.mjs`, Node tool helpers,
`package.json`, prompt) is not exported as a usable toolkit. It is not used by
any Python engine; its source referenced an absent `dry_test.mjs` and contained
a broken protected-path loop. Shipping it would require a separate provider
contract, repair and network-free API mocks. The public Python route covers its
local planning purpose without an API dependency. No Node package is installed.

This packaging does not establish scientific validity of score heuristics,
biological analogies, or model-assisted reasoning. It provides inspectable local
planning and evidence contracts.
