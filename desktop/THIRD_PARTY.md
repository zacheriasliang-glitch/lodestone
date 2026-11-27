# Desktop source attribution

The desktop payload contains separately attributed components:

| Directory | Source | Licence |
|---|---|---|
| `runtime/` | Lodestone contributors, including the Unified Loom runtime | MIT, reproduced in `runtime/LICENSE` |
| `gui/` | Modified [BasiliskLLM](https://github.com/SigmaNight/basiliskLLM), by its credited contributors | GPL-2.0-only, reproduced in `gui/LICENSE` |

The combined desktop distribution, including its installer and launcher, is
distributed under GPL-2.0-only; the complete licence is in `LICENSE`.
The runtime component retains its MIT notice and may also be used separately
under that licence. The repository's top-level MIT notice does not replace the
GUI's GPL licence or the licence of the combined desktop distribution.
The complete GUI source included here accompanies the desktop distribution.
Its upstream contributor credits remain in `gui/pyproject.toml` and
`gui/basilisk/consts.py`.

Lodestone modifications include the local Loom provider, retained-outcome and
correction controls, Origami handoff and toolkit controls, and coordinated
shutdown. The native desktop adaptation adds per-user macOS, Ubuntu and Windows
launchers, recovery transactions and platform-specific worker supervision. These
changes are part of the GPL-2.0-only desktop distribution. The public desktop
adaptation limits provider selection to the local Loom route, uses a generic correction author, and supports version display from
an archive without Git metadata. `SOURCE_PROVENANCE.json` identifies the source
revision and exported file hashes; later changes remain in public Git history.

Python packages, the local inference engine and model assets are separate
downloads. Their upstream licences and notices remain applicable; installing
them does not change the licences of this source payload. The dependency
manifest records their pinned download identities.

## Target dependency records

The source tree carries separate pinned dependency sets for Intel macOS 12 or
later, Ubuntu 24.04 x86_64 and Windows 11 / Windows Server 2022 x86_64:

- `dependencies.lock.json`: macOS source lock.
- `dependencies.ubuntu-24.04-x86_64.lock.json`: Ubuntu source lock.
- `dependencies.windows-x86_64.lock.json`: Windows source lock.

Each built target archive places its selected set at `dependencies.lock.json`.
`gui-dependencies.json` declares the direct dependencies of the shipped Loom-only
GUI. Its exact transitive closure is pinned in each target lock. Unregistered
provider SDKs are omitted; their upstream engine source, `gui/pyproject.toml` and
`gui/uv.lock` remain as source and project metadata. The desktop installs its
pinned wheels directly and does not install the upstream `basiliskllm` package.
Its payload manifest identifies the target and checksums that selected file.
The lock records the upstream URLs, versions, sizes and SHA-256 identities of
Python/uv, Python wheels, the Ollama inference engine and the Qwen3 model and
tokenizer assets. The Qwen3 model and tokenizer entries retain their Apache-2.0
licence declaration. The other components retain the licences and notices
provided in their upstream distributions and installed package metadata; a
checksum is an integrity record, not a substitute licence or a digital signature.

Ubuntu GUI, audio and isolation prerequisites are obtained separately through
the operating system package manager. They are not incorporated into this source
payload, and their installed package licences and notices remain applicable.
The optional `accessible-output3` speech/braille plugin is not included in the
pinned target environments because compatible pinned wheels are unavailable.
The native wx controls remain part of the GUI, without a claim that screen-reader
or braille accessibility has been verified.

These dependency and source records describe the candidate build inputs. They
do not establish that a target has passed native validation or that a release
has been published. Consult the exact release's verification evidence for that
status. Installation performs no model training or weight update.
