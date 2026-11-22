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
shutdown. The public desktop adaptation limits provider selection to the local
Loom route, uses a generic correction author, and supports version display from
an archive without Git metadata. `SOURCE_PROVENANCE.json` identifies the source
revision and exported file hashes; later changes remain in public Git history.

Python packages, the local inference engine and model assets are separate
downloads. Their upstream licences and notices remain applicable; installing
them does not change the licences of this source payload. The dependency
manifest records their pinned download identities.
