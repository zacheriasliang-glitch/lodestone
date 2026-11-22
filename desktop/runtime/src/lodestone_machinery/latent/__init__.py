"""Opt-in latent packet storage with explicitly supplied candidate rows.

Runtime modules were ported from Lodestone's existing crystal-latent research
implementation at SOURCE_REVISION, without importing its results or artifacts.
The packet, directory and checkpoint formats remain those of that source.
Package-relative imports and an explicit candidate-only pinned-byte loader are
the integration changes; no candidate generator or training job runs on import.
"""

SOURCE_REPOSITORY = "https://github.com/zacheriasliang-glitch/lodestone-lab"
SOURCE_REVISION = "5cd4cf8aa0211b0264607b0e17c9d91346bb22cc"
SOURCE_DIRECTORY = "experiments/001-rule30/Genepool/crystal-latent"

# Original bytes before package import and scoped loader adaptation.
SOURCE_SHA256 = {'artifact_storage.py': '4de59bb22685f58998589271613c433acb9eeccd664ddc5d05bc1b0fbf217e8e',
 'candidate_inputs.py': '494a6152438009d5cabe57c6fd85c6b72827a6a90fea6bc27cff132d7938dcf5',
 'codec.py': '43f2877358b8243531e0be2f2af27ec4fd0e5d219bdedc475a59f46dd685b2a5',
 'compact_checkpoint.py': '39834edda5f7ca306adfb9e71419af43db4b94eb5c11ce8a6989f57205ef98d8',
 'compact_layout.py': '6bd0f5c255d00fa10dc03cc3ae15a090cdb08995a4c806edfd9a1eb0345bd1ef',
 'latent_model.py': '8f538397940be2a38d54410f9c042010687abac822d3ae1b310bd5eb5ab2e5f8',
 'lifting.py': 'ee76457d0faf71597aa2eb6c896d01036208f2e43d2365d02c4f498db10a7410',
 'store_adapter.py': '6ed55614db8e85eb5266f6353f15383846afc9a77fb667f4f75d0ffa9ea671af'}
