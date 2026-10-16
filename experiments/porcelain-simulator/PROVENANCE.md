# Public source provenance

This optional module is a reviewed, self-contained public export of Lodestone's
numerical baseline, prepared on 17 October 2026. It contains nine Python modules, five synthetic configuration
examples, and two dependency declarations. Public documentation was written for
this release.

The initial continuum baseline was developed for Lodestone with ChatGPT. The
hollow-wall and hand-contact extension was developed with Claude Code, with
Codex review. These identify implementation contributions; they do not supply
independent physical validation. Project authorship and licensing follow the
root [license](../../LICENSE).

Finite-element assembly uses the installed scikit-fem library. Numerical arrays
and linear algebra use NumPy and SciPy; rendering uses Matplotlib and Pillow.
These dependencies are installed separately and retain their own licenses.

The export removes personal and internal review references from comments and
docstrings. Example descriptions were made self-contained; the one-versus-two
case is explicitly described as an equivalence check. Executable Python syntax
trees, after removing docstrings, were compared with the source revision and
are unchanged. Numerical configuration fields are unchanged. The original
revision mapping is retained in the project's private release evidence.

Comments and descriptions participate in byte hashes, so the public source and
some configuration digests differ from earlier artifacts. The public export's
five-module simulation digest is
`110de0be0ada72e2fa060c8ca83cb283a2334e159397b928df4af084ceb03a1c`.
The [source manifest](SOURCE_MANIFEST.json) records per-file public export
hashes and the preservation checks. A historical checkpoint is not resumable under
a different source or configuration digest.

Historical numerical verification concerns synthetic, uncalibrated scenarios.
The release checks cover source preservation, input structure, imports and
command help; no numerical experiment was rerun for this export. The legacy
serialized module version remains `0.1.0`; source hashes distinguish the
included hand-contact extension and subsequent corrections.

Saved episodes, images, research papers, original transfer material, historical
notes and private operational records are outside this export.
