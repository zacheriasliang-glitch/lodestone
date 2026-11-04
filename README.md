# Lodestone

**Keep useful project knowledge between AI sessions, recover your files, and run
local toolkit tasks with a record of what happened.**

[Download v0.3.0](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.0)
· [Quickstart](docs/quickstart.md) · [All tools](docs/toolkits.md)

The release is available now. Python3.10+ is all you need for the core; your
notes and files stay on your computer. Use macOS or Linux for all workflows
below. Local memory also works on native Windows, using `py -3` for `python3`.

## Save a decision and hand it to your next AI session

Download the named `lodestone-0.3.0.zip` asset, extract it, and open a terminal
inside its versioned folder. Verify the download:

```sh
python3 -B verify_download.py
```

Save a project decision, find it again, and make a short handoff:

```sh
python3 -B lodestone.py memory add ./my-notebook \
  --title "Website launch decision" \
  --body "Use a static website. Launch the contact page first. Keep the existing domain. The launch date is still undecided." \
  --source "Project planning note" --tags website,decision
python3 -B lodestone.py memory search ./my-notebook "website"
python3 -B lodestone.py memory packet ./my-notebook "website" \
  --max-words 300 --output ./website-handoff.md
python3 -B lodestone.py memory verify ./my-notebook
```

**You get:** a searchable local notebook and `website-handoff.md`, ready to give
to your next Codex, Claude or other AI session. The packet contains the saved
note and its attribution. Replace the example decision with your own; repeating
an identical entry with the same source reuses the record. Search matches stored
words and links. You choose what to retain and what to send to an AI.

## Make a verified copy of your notebook

Keep the original notebook and restore a checked copy into a new directory:

```sh
python3 -B lodestone.py yggdrasil archive pack \
  --root notebook=./my-notebook --destination ./notebook-archive \
  --epoch first-copy --authority-ref local-user-request
python3 -B lodestone.py yggdrasil archive verify --archive ./notebook-archive
python3 -B lodestone.py yggdrasil archive restore \
  --archive ./notebook-archive --destination ./notebook-restored
python3 -B lodestone.py memory search ./notebook-restored/notebook "website"
```

**You get:** an archive with per-file integrity checks and a restored copy of
your notebook. Both output directories must be new. To protect against losing
the computer, keep a verified copy on another device or storage destination you
control. The [archive guide](docs/archives.md) covers pins and preserved metadata.

## Let Nurarihyon execute an approved local task

[Nurarihyon's quickstart](docs/quickstart.md#capture-a-document-as-inert-evidence)
walks through capturing your UTF-8 document into Pando, approving the exact task,
and letting the worker perform it. **You get** retained source text, a completed
task record, an independently checked result and persistent history. The same
queue also supports board changes and checks of captured discrepancy/lineage
records. [Task guide](docs/nurarihyon.md) · [Recovery](docs/nurarihyon-recovery.md)
· [Optional macOS startup](docs/nurarihyon-service.md).

For an immediate source-backed job with included example inputs:

```sh
python3 -B lodestone.py --workspace ./my-workspace machinery demo
```

It produces a brief from two sources and reports `COMPLETED` with
`combined_verdict: PASS`. Use the [toolkit catalogue](docs/toolkits.md) to choose
other operations, including explicit provider-backed Origami requests and
project checkpoints.

## Choose a tool by the job

| I want to… | Use |
|---|---|
| Remember decisions and prepare an AI handoff | [Local memory](tools/local-memory/README.md) |
| Check that a saved task's source files still match | [Continuity checkpoints](docs/continuity.md) |
| Archive and recover selected files | [Archives](docs/archives.md) |
| Queue and execute a supported local task | [Nurarihyon](docs/nurarihyon.md) |
| Keep sources, discussions and derived artifacts together | [Pando](docs/pando.md) |
| Inspect discrepancies and lineage in captured records | [Reconciliation](docs/nurarihyon.md#supported-contracts) |
| Prepare a source-bound request for my own model provider | [Origami](docs/toolkits.md) |
| Assess a proposed command against my policy | [Conscience](docs/conscience.md) |

## Install a short command (optional)

On macOS/Linux, run `python3 -B tools/ecosystem/install.py`, then
`~/.local/bin/lodestone --help`. This installs a launcher tied to this folder;
keep the folder in place. Background startup is a separate opt-in in the service
guide. You can always use `python3 lodestone.py` directly.

## Release details

v0.3.0 is [published](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.0).
The release page provides the exact ZIP/tar.gz, checksums, build provenance and
passed verification. Its immutable archives preserve the reviewed source
snapshot, including older candidate labels; publication status is recorded on
the release page. [Release history](CHANGELOG.md) ·
[31-component programme map](docs/capability-map.md) ·
[Report a problem](https://github.com/zacheriasliang-glitch/lodestone/issues).

<details>
<summary>Platform, provider and research details</summary>

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
interpretation or mechanistic model explanations. The rejected passage-budget skill selector and frozen
studies remain excluded from runtime features.

Checkpoint preservation checks required source identities, notes and unresolved
questions. It does not guarantee that a summary contains everything a future
question needs, or run automatically when an agent compacts its context.

Provider commands are trusted code. The launcher filters environment variables;
filesystem and network restrictions require an available kernel sandbox and are
reported as declarations when one is unavailable. Explicitly readable source,
workspace, program/system and authentication paths can still contain credentials.


## Verification and limitations

`verify_download.py` checks the included file hashes. Release assets also have
external SHA-256 checksums. These detect changed bytes; they are not a digital
signature or proof of who published them.

This distribution adapts selected implemented laboratory capabilities for fresh
user configuration. The published 0.3.0 release preserves public history and
keeps laboratory provenance separate from release validation. See [PR #5](https://github.com/zacheriasliang-glitch/lodestone/pull/5),
[reviewed-build CI](https://github.com/zacheriasliang-glitch/lodestone/actions/runs/41334839775)
and the [published verification record](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.0)
for the exact reviewed commit, executed checks and asset hashes. Experimental learned
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


</details>

## License

MIT. See [LICENSE](LICENSE). Optional dependencies retain their own licenses.
