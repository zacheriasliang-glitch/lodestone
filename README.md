# Lodestone

A local toolkit for working with source-backed records, exact file archives,
and explicit command checks. The core runs with **Python 3.10+**, using only the
standard library. No account, API key, Git installation or background service is
needed to try it.

## Download and run

1. [Download Lodestone](https://github.com/zacheriasliang-glitch/lodestone/releases/latest)
   and choose `lodestone-0.1.0.zip`. Extract it.
2. Open a terminal in the extracted `lodestone-0.1.0` folder.
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

| Component | Start here | Purpose |
|---|---|---|
| Machinery | `python3 lodestone.py machinery --help` | Source-backed jobs, records, inspection and explicit workspaces |
| Conscience | [Command checks and waking](docs/conscience.md) | Advisory command assessment and linked local wake records |
| Archives / Saṃsāra foundations | [Pack, verify and restore](docs/archives.md) | Deduplicated file preservation with verified recovery |
| Origami | `python3 lodestone.py origami --help` | Prepare source-bound questions; connect your own provider explicitly |
| Corpus navigation | `python3 lodestone.py yggdrasil corpus-fold --help` | Build and inspect bounded source maps |
| Porcelain simulator (optional) | [Install and use](docs/porcelain.md) | Synthetic numerical teaching examples; separate scientific dependencies |

Conscience is advisory, not a security sandbox. It does not confer permission or
start services. The public policy belongs to the local user and can be reviewed
before use. There are no inherited host grants or private mailbox connections.

The archive tools are the local preservation layer. This download includes no
personal Saṃsāra snapshots and does not configure cloud storage or transfer keys.
Origami can prepare requests without an AI account; an actual model-backed run
requires your own explicit provider command and any credentials that provider
needs. Nothing contacts a model automatically.

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

## Verification and limitations

`verify_download.py` checks the included file hashes. Release assets also have
external SHA-256 checksums. These detect changed bytes; they are not a digital
signature or proof of who published them.

This is the first public release. It contains a selected, independently reviewed
runtime, examples and tests with fresh repository history. Experimental learned
compression has no demonstrated general advantage. Porcelain coefficients are
illustrative and uncalibrated. Private research corpora, conversations, histories,
credentials, live services and host-specific policies are not part of this release.

Repository-specific Yggdrasil canon adoption and whole-laboratory snapshot
commands require a separately configured corpus and are not provided by this
public package. Use the documented archive, crystal and corpus-navigation routes.

See [release notes](CHANGELOG.md) for the exact support and validation scope.
Report reproducible problems through [GitHub issues](https://github.com/zacheriasliang-glitch/lodestone/issues).

## License

MIT. See [LICENSE](LICENSE). Optional dependencies retain their own licenses.
