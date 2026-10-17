# Toolkit catalogue

Lodestone includes the reusable tools from all eight laboratory `tools/` families.
Private records and this lab's running services are not part of the package.
`python3 -B lodestone.py toolkits` prints a machine-readable catalogue.
All CLI global options go before the command. Commands otherwise keep their own
arguments; append `--help` to inspect them.

| Toolkit | Entry | Configuration and contract |
|---|---|---|
| Machinery | `machinery --help` | Source-backed jobs and records in an explicit workspace; included demo works offline. |
| Ecosystem / Conscience | `doctor`, `context`, `wake`, `conscience --help` | Local readiness, bounded documentation and advisory command checks. Waking does not start all tools. |
| [Corpus fold](corpus-fold.md) | `yggdrasil corpus-fold --help` | Select your corpus; reading maps preserve source identities and declared lineage. |
| [Local memory](../tools/local-memory/README.md) | `memory --help` | Initialize your own store; explicit notes, links and word-bounded retrieval. |
| [Origami re-entry fold](origami-fold.md) | `origami-fold --help` | Explicit source root and fold, with an invented example; no historic workstream state. |
| [Continuity / compact context](continuity.md) | `continuity --help` | Create immutable-by-contract checkpoints, verify source drift and reopen evidence. Optional agent skill installation. |
| [Mailbox](mailbox.md) | `mailbox --help` | POSIX/Git transport, your remote, branch and local state. Transport is separate from worker execution. |
| [Repository export](../tools/repository/README.md) | `repository manifest`, `repository export`, `repository verify` | Explicit committed UTF-8 text selection, local private Git bundle and checked source archive. Git required. |
| [Orchestrator](orchestrator.md) | `orchestrator --help` | Python Kernel20/legacy planning and Kernel21 custody helpers; explicit project root; no automatic execution. |
| [Archives / Saṃsāra foundations](archives.md) | `origami archive --help` | Pack, verify and recover selected files. Cloud/account setup is user-owned. |
| Crystal / numerical modules | `origami crystal --help`, `origami transformer --help` | Existing bounded storage and numerical contracts; inclusion does not establish learned compression benefit. |
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

## What stays outside the download

Your own project must supply its sources, records, policy and credentials where
needed. The download contains no private memory, scientific corpus, transcript,
mailbox cache or historical worker admission. Old experiments and results remain
in their original custody. Host-specific LaunchAgents, desktop sessions, Notion
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
