# Lodestone

A local toolkit for working with source-backed records, exact file archives,
explicit command checks, and approved toolkit tasks. The core runs with **Python 3.10+**, using only the
standard library. No account, API key, Git installation or background service is
needed to try it.

> This branch prepares **0.3.0**. It is a review candidate, not a published release;
> the latest-release link below continues to serve the last approved version.

## Download and run

1. [Download Lodestone](https://github.com/zacheriasliang-glitch/lodestone/releases/latest)
   and choose the versioned `lodestone-*.zip` asset. Extract it.
2. Open a terminal in the extracted versioned folder containing `lodestone.py`.
3. Run:

```sh
python3 -B verify_download.py
python3 -B lodestone.py doctor
python3 -B lodestone.py --workspace ./my-workspace machinery demo
```

The full documented core is supported on **macOS and Linux**. Native Windows has
an initial doctor/demo/Conscience route; Origami, archive recovery and corpus
navigation require POSIX filesystem features (use macOS/Linux, or Linux in WSL).
On Windows, use `py -3` in place of `python3`. If Python is not installed,
get [Python 3.10 or later](https://www.python.org/downloads/). The demo creates
its own workspace and produces a short brief from two included example sources.
Its JSON has `state: COMPLETED` and `result.combined_verdict: PASS` when the demonstration succeeds.
That is a check of the demo's source contract, not a claim of general reasoning
or scientific validity.

For development, you can also clone this public repository:

```sh
git clone https://github.com/zacheriasliang-glitch/lodestone.git
cd lodestone
python3 -B lodestone.py --help
```

## What you can use

The public toolkit families are available in this distribution, with new
Nurarihyon and Pando interfaces for user-owned state. The
[quickstart](docs/quickstart.md) walks through an approved board change and document
ingestion. The [31-component coverage map](docs/capability-map.md) keeps supported,
experimental and unfinished programme capabilities visible.
Run `python3 -B lodestone.py toolkits` or read the [toolkit catalogue](docs/toolkits.md)
for configuration requirements and the exact boundary between code and private state.
The [compact-context guide and skill](docs/continuity.md) cover saving task
checkpoints, verifying their sources and comparing what survives a handoff.
The skill can be installed separately.

| Component | Start here | Purpose |
|---|---|---|
| Nurarihyon | [Approved tasks](docs/nurarihyon.md) | Durable queue, bounded workers, independent result acceptance, STOP and selected-state recovery |
| Pando | [Local board](docs/pando.md) | Sources, posts and EAT → DIGEST → MUTATE artifacts with user-owned attribution |
| Bezoar / Yggdrasil reconciliation | [Captured evidence](docs/nurarihyon.md#supported-contracts) | Preserve discrepancies, lineage and historical observations without automatic repair |
| Machinery | `python3 lodestone.py machinery --help` | Source-backed jobs, records, inspection and explicit workspaces |
| Conscience | [Command checks and waking](docs/conscience.md) | Advisory command assessment and linked local wake records |
| Continuity | [Checkpoint, verify and preserve](docs/continuity.md) | Compare declared evidence across checkpoints within one source revision |
| Archives / Saṃsāra foundations | [Pack, verify and restore](docs/archives.md) | Deduplicated file preservation with verified recovery |
| Origami | `python3 lodestone.py origami --help` | Prepare source-bound questions; connect your own provider explicitly |
| Corpus navigation | `python3 lodestone.py yggdrasil corpus-fold --help` | Build and inspect bounded source maps |
| Supplied-candidate storage (experimental) | [Exact candidate-table storage](docs/supplied-candidates.md) | Explicit caller-pinned put/get; no candidate generation or learned-benefit claim |
| Porcelain simulator (optional) | [Install and use](docs/porcelain.md) | Synthetic numerical teaching examples; separate scientific dependencies |

Standalone Conscience is advisory. Nurarihyon can bind its assessment as an
additional worker-admission condition; neither mode is an OS security sandbox or
a permission source. It does not start services. The public policy belongs to the local user and can be reviewed
before use. There are no inherited host grants or private mailbox connections.

The archive tools are the local preservation layer. This download includes no
personal Saṃsāra snapshots and does not configure cloud storage or transfer keys.
Origami can prepare requests without an AI account; an actual model-backed run
requires your own explicit provider command and any credentials that provider
needs. Nothing contacts a model automatically.

Nurarihyon supports five fixed toolkit contracts and explicit profile-outcome
retention. It does not infer new operations or prove useful retained learning.
Reconciliation checks source-bound records rather than supplying semantic Origami
interpretation or mechanistic model explanations. Rejected selectors and frozen
studies remain excluded from runtime features.

Checkpoint preservation checks required source identities, notes and unresolved
questions. It does not guarantee that a summary contains everything a future
question needs, or run automatically when an agent compacts its context.

Provider commands are trusted code. The launcher filters environment variables;
filesystem and network restrictions require an available kernel sandbox and are
reported as declarations when one is unavailable. Explicitly readable source,
workspace, program/system and authentication paths can still contain credentials.

## Optional short command on Mac/Linux

```sh
python3 -B tools/ecosystem/install.py
~/.local/bin/lodestone --help
```

This installs only a launcher tied to this extracted folder and its Python.
Keep that folder in place. It does not edit your shell profile, install packages
or start services. The `python3 lodestone.py` route always remains available.
Nurarihyon [graphical-login startup](docs/nurarihyon-service.md) is a separate,
explicit macOS opt-in. Installation never approves a task or clears STOP.

## Verification and limitations

`verify_download.py` checks the included file hashes. Release assets also have
external SHA-256 checksums. These detect changed bytes; they are not a digital
signature or proof of who published them.

This distribution adapts selected implemented laboratory capabilities for fresh
user configuration. The proposed 0.3.0 candidate preserves public history and
keeps laboratory provenance separate from release validation. See the candidate
PR and CI for the exact reviewed commit and executed checks. Experimental learned
compression has no demonstrated general advantage. Porcelain coefficients are
illustrative and uncalibrated. Private research corpora, conversations, histories,
credentials, live services and host-specific policies are not part of this release.

Yggdrasil canon, pack and reenter commands require your own explicitly selected
root and canon configuration. No laboratory adoption, receipts or source corpus
is inherited. See the toolkit catalogue for configuration-dependent commands.

Selected private state can be exported and restored using the
[recovery guide](docs/nurarihyon-recovery.md). Runtime source publication is not
a backup of your board, captures, outcomes or external configuration.

See [release notes](CHANGELOG.md) for the exact support and validation scope.
Report reproducible problems through [GitHub issues](https://github.com/zacheriasliang-glitch/lodestone/issues).

## License

MIT. See [LICENSE](LICENSE). Optional dependencies retain their own licenses.
