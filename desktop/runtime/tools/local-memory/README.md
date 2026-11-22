# Local memory

Explicit records, typed links, lexical search and bounded context packets, using
Python's standard library. Start with an empty directory of your own:

```sh
python3 -B lodestone.py memory init ./my-memory
python3 -B lodestone.py memory add ./my-memory --title "Project constraint" --body "Keep original measurements unchanged." --source notes.md --tags project,evidence
python3 -B lodestone.py memory search ./my-memory "measurements"
python3 -B lodestone.py memory packet ./my-memory "measurements" --max-words 300
python3 -B lodestone.py memory verify ./my-memory
```

Use `memory --help` for linking, related records and explicit supersession.
The path is always supplied by you; no private store is shipped or discovered.
Records are ordinary JSONL files. Packets are word-bounded selections, not a
guarantee about a model's tokenizer, complete recall or semantic equivalence.
Similarity is a retrieval hint, not proof. Source paths preserve provenance but
do not automatically ingest the named files. Store selected notes, not credentials.
`packet --output` writes its chosen destination; select a new path if preserving
an earlier packet. The engine is a local single-writer store, not a shared database.

## Reopen the source behind a note

`source-bind` records an exact passage from an existing UTF-8 source capture.
`source-address` searches the same notebook, then reopens each matching note's
bound passage to check its source and slice hashes. Both commands return JSON.
The bindings are kept in a separate manifest outside the notebook; the existing
record and link formats stay unchanged.

For a note already saved in `./my-memory`, replace `RECORD_ID` below with the ID
printed by `memory add` or `memory search`. This example binds line 1 of your
existing `./source-scope/notes.md` capture:

```sh
python3 -B lodestone.py memory source-bind ./my-memory RECORD_ID \
  --bindings ./source-bindings.json --scope ./source-scope \
  --source notes.md --start-line 1 --end-line 1 \
  --capture snapshot-1 --attribution "Project planning note"
python3 -B lodestone.py memory source-address ./my-memory "measurements" \
  --bindings ./source-bindings.json --scope ./source-scope --limit 5
```

Choose the actual line span containing the passage. A binding retains its
captured scope, attribution, source hash and slice hash. `--source-url` can also
retain the original page URL; it is attribution only and is never fetched.
Capture labels and attribution are supplied by the caller, not authenticated
publisher identities or verification of a Git revision. The file and passage
hashes are computed from the bytes actually read. A relocated byte-identical
capture can be reopened by supplying its new `--scope` directory.
`source-bind` explicitly updates the single-writer manifest atomically.
It returns the exact bound text for inspection. Line numbers follow Python's
universal line boundaries: `\n`, `\r`, `\r\n`, `\v`, `\f`, `\x1c`–`\x1e`,
U+0085, U+2028 and U+2029. These can differ from editor or `wc -l` line numbers;
inspect the returned passage before relying on the binding.
`source-address` is read-only and checks up to ten matching notes; use
`--include-superseded` when you deliberately need historical notes.

The result distinguishes a verified address from changed bytes, missing sources,
ambiguous bindings and an unknown result within the supplied scope. A lexical
match or verified address does not establish that the passage supports the note,
that the source is true, or that learning occurred. An absent local capture does
not establish absence elsewhere. Malformed inputs, invalid paths, UTF-8 failures
and exceeded bounds return exit code 2; structured lookup findings are JSON
results. No model, network connection or daemon task is involved.

The new commands accept at most 512 records and 1,024 links in a combined 1 MiB
catalogue, and 512 bindings in a 1 MiB manifest. Each source is limited to 2 MiB
of strict UTF-8 and each selected passage to 16 KiB. One lookup reopens at most
64 bindings and 16 MiB of source bytes, and emits at most 1 MiB of JSON including
its newline. A bound is an explicit refusal, never a silently selected binding.
Multiple candidates retain overall ambiguity even with `--limit 1`; `truncated`
indicates that more matching notes were omitted. Source symlinks may resolve
inside the supplied capture root, but escapes are refused. Manifest/catalogue
files must be regular files, not symlinks. These are single-writer local-file
operations, not protection against concurrent hostile filesystem changes.
Manifests use private mode 0600 on replacement. An interrupted write can leave
a `.source-bindings-*` temporary file; it is never treated as the manifest.

For checkpointing a task before native session compaction, use the
[continuity toolkit and compact-context skill](../../docs/continuity.md).
