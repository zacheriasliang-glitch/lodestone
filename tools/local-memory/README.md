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

For checkpointing a task before native session compaction, use the
[continuity toolkit and compact-context skill](../../docs/continuity.md).
