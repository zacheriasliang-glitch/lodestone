# Re-enter a task from bound sources

The Origami fold stores a small, explicit reading map: claims, uncertainty,
restrictions and exact source passages. Each passage is bound to a path, line
range, byte range and SHA-256. It helps a returning reader reopen evidence; it
does not automatically understand documents or compact a model's context window.

The public package contains an **invented tutorial**, not the laboratory's saved
workstream or authorizations. From the extracted package:

```sh
python3 -B tools/origami-fold/src/origami_fold.py verify
python3 -B tools/origami-fold/src/origami_fold.py orient
python3 -B tools/origami-fold/src/origami_fold.py expand sample.observation
```

These commands only read. `expand sample.observation` uses the saved passage
binding. An ad-hoc locator such as `R:L3-3` identifies current lines and says its
slice hash was computed now; it is not evidence of a previously bound slice.

Create a disposable, self-contained copy to try edits:

```sh
python3 -B tools/origami-fold/src/build_example.py --output ./fold-demo
python3 -B ./fold-demo/tools/origami-fold/src/origami_fold.py verify
```

The destination must not exist and its parent must exist. The example includes
its two source scripts and invented sources. Change `three blue tiles` in
`fold-demo/tools/origami-fold/examples/demo/RESULT.md`, then run `expand
sample.observation` again: it reports the changed passage and shows the current
bytes. It preserves the claim and its original binding for review.

## Your own mapping

Use `build_example.py` as a small construction recipe: replace the invented
sources, question, claims and exact line ranges with your own reviewed mapping.
The engine's `bind_source` and `bind_support` functions compute byte identities
and reject a quote that is absent from the stated range. They do not decide
whether a source supports an interpretation. Keep recorded facts and
interpretations distinct.

Select a user-owned root and fold explicitly:

```sh
python3 tools/origami-fold/src/origami_fold.py \
  --root /absolute/project --fold /absolute/project/fold.json orient
```

`refresh` checks dependencies; `refresh --apply` records derived freshness and
updates the readable view, leaving claims and bindings intact. `render` writes
that view. `rebind` accepts a dependency only after review, with `--reviewed-by`
and a note; it does not relocate a changed quote automatically. Back up your
fold before intentional updates. None of these operations grants authority to
execute a recorded task.

Git is optional. When unavailable, provenance reports `UNKNOWN`. SHA-256 checks
detect changed bytes; they do not authenticate an author. `orient` is a report
and may exit successfully while displaying stale entries; use `verify` or
`refresh` when a nonzero status for detected problems is needed. Missing source
files and changes must be resolved by reading evidence, never by filling gaps
from a summary.

The original engine's receipt and disposition triggers remain available for
users who configure those record conventions. The tutorial has no historical
receipt corpus, host grants, private notes or claim of scientific validity.

```sh
python3 -B -m unittest discover -s tools/origami-fold/tests -q
```
