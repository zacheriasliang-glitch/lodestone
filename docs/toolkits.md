# Toolkit catalogue

Lodestone includes the public toolkit families plus the portable Nurarihyon/Pando
interfaces. The [31-component map](capability-map.md) distinguishes implemented
interfaces from experimental and unfinished work; inclusion is not whole-programme
completion.
Private records and laboratory service registrations are not part of the package.
`python3 -B lodestone.py toolkits` prints a machine-readable catalogue.
All CLI global options go before the command. Commands otherwise keep their own
arguments; append `--help` to inspect them.

| Toolkit | Entry | Configuration and contract |
|---|---|---|
| [Nurarihyon](nurarihyon.md) | `nurarihyon registry`, `nurarihyon --help` | Explicit state/policy, five fixed task contracts, separate exact approval and bounded execution; macOS/Linux. |
| [Pando](pando.md) | `pando --home PATH --help` | Standalone user-owned board, source captures, immutable artifact lineage and optional foreground loopback UI. |
| Bezoar / Yggdrasil reconciliation | `nurarihyon capture-corpus --help` | Explicit inline or captured source corpus; retained discrepancies and unresolved historical observations. No automatic repair. |
| Machinery | `machinery --help` | Source-backed jobs and records in an explicit workspace; included demo works offline. |
| Ecosystem / Conscience | `doctor`, `context`, `wake`, `conscience --help` | Local readiness, bounded documentation and advisory command checks. Waking does not start all tools. |
| [Corpus fold](corpus-fold.md) | `yggdrasil corpus-fold --help` | Select your corpus; reading maps preserve source identities and declared lineage. |
| [Local memory](../tools/local-memory/README.md) | `memory --help` | Initialize your own store; explicit notes, links and word-bounded retrieval. |
| [Origami re-entry fold](origami-fold.md) | `origami-fold --help` | Explicit source root and fold, with an invented example; no historic workstream state. |
| [Continuity / compact context](continuity.md) | `continuity --help` | Create checkpoints, verify pinned sources and compare retained evidence with `preserve`. Optional agent skill installation. |
| [Mailbox](mailbox.md) | `mailbox --help` | POSIX/Git transport, your remote, branch and local state. Transport is separate from worker execution. |
| [Repository export](../tools/repository/README.md) | `repository manifest`, `repository export`, `repository verify` | Explicit committed UTF-8 text selection, local private Git bundle and checked source archive. Git required. |
| [Orchestrator](orchestrator.md) | `orchestrator --help` | Python Kernel20/legacy planning and Kernel21 custody helpers; explicit project root; no automatic execution. |
| [Archives / Saṃsāra foundations](archives.md) | `origami archive --help` | Pack, verify and recover selected files. Cloud/account setup is user-owned. |
| Crystal / numerical modules | `origami crystal --help`, `origami transformer --help` | Existing bounded storage and numerical contracts; inclusion does not establish learned compression benefit. |
| [Supplied-candidate storage](supplied-candidates.md) (experimental) | `origami crystal latent --help` | Exact put/get using caller-supplied pinned candidate tables; no model run or learned-compression claim. |
| [Porcelain](porcelain.md) | `porcelain --help` | Overview needs no optional packages; subcommand help and runs require the numerical environment. Synthetic teaching examples. |
| Release packaging | `python3 -B tools/release/package.py --help` | Deterministic downloads built from committed public source. |

## Evidence and project checks

- `receipt --root PROJECT --draft EXTERNAL_DRAFT.md` preflights exact receipt
  bytes. Only explicit `--append` publishes; it refuses replacement and new
  lineage defects. It requires Git, POSIX and the project's own `receipts/`.
  Inspect `tools/machinery/append_receipt.py` for the required receipt fields.
- `brief-check BRIEF.md` checks prerequisite identity, bound quotations and
  declared additions under the brief format. `registry-check REGISTRY.md`
  validates a finding registry. Their `--help` describes the input contracts.
- `lineage-findings --root PROJECT` emits findings for that project's receipts;
  `--report REPORT.json` adapts an already-saved lineage report.
- `check --root PROJECT` checks local Markdown destinations. It does not claim
  the private lab's historical migration-map or external URL checks.
- `yggdrasil --root PROJECT --findings` inspects the supplied receipt corpus.
  `yggdrasil canon|pack|reenter --root PROJECT ...` requires that project's
  configured `docs/yggdrasil/canon.json` and governing source files. These
  advanced source contracts are implemented in `src/lodestone_machinery/yggdrasil_runtime.py`;
  a fresh project has no automatic canon adoption. Macro-voxel library tests
  use synthetic fixtures; no private corpus is needed for the basic tools.

## New task and retention interfaces

`nurarihyon configure` prepares a user-owned policy; `init` creates a separate
state. Neither starts a service. `submit`, exact `approve` and `run` remain distinct
steps. `stop`, `status`, `verify`, `recover` and explicit `retry` preserve the task
history. [Service startup](nurarihyon-service.md) is an additional manual opt-in.

`nurarihyon remember` and `context` verify profile outcomes and attach bounded
context; they do not choose skills, change priority or waive checks. Inline
reconciliation's optional reuse pins earlier same-corpus evidence for inert
routing. Existing retained records do not establish a measured cost reduction.
[Selected-state recovery](nurarihyon-recovery.md) preserves the chosen state and
runtime, with external configuration and separate memory exclusions disclosed.

`machinery bezoar --help` exposes explicit ingest/EAT/MUTATE/hold/export-profile
contracts. `machinery watcher --help` inspects declared record handles in bounded
polls. These use real source/record APIs; plain text cannot activate a candidate.

## What stays outside the download

Your own project must supply its sources, records, policy and credentials where
needed. The download contains no private memory, scientific corpus, transcript,
mailbox cache or historical worker admission. Old experiments and results remain
in their original custody. Preinstalled LaunchAgents, desktop sessions, Notion
bindings and cloud accounts are not portable tool configuration.

The original lab-only repository migration checker depends on its historical
path map and manifests; the public `check` is the local navigation contract
above. The unverified legacy Node orchestrator API prototype is not included;
the Python orchestration toolkit and its limitations are described separately.
Historical workstream recipes are replaced by explicit-user-root examples.

The default core still needs only Python 3.10+. Git-backed, POSIX and optional
numerical routes state their additional prerequisites. Installing the launcher
does not install dependencies, start a mailbox watcher, admit a worker, invoke a
model, compact an agent's native context, or run a scientific scene.
