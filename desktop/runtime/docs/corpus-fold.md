# Navigate a record corpus

A corpus fold is a reading map over files you select. It records their paths,
byte counts and SHA-256 hashes, plus relationships explicitly declared in JSON
fields. It preserves the original files. The map helps you find records and
their corrections; it does not establish whether their claims are true.

Use Python 3.10+ on macOS/Linux. No account, Git repository, model or additional
package is required. Run the examples below from the extracted Lodestone folder
containing `lodestone.py`.

## Try two invented records

This creates a new `corpus-demo` directory and refuses to reuse an existing one.
The counts and dates are invented tutorial data.

```sh
python3 -B - <<'PY'
import json
from pathlib import Path

root = Path("corpus-demo")
root.mkdir()
records = root / "records"
records.mkdir()
examples = {
    "count-01.json": {
        "id": "count-01",
        "written_at": "2026-01-01T00:00:00Z",
        "statement": "There are two blue tiles."
    },
    "count-02.json": {
        "id": "count-02",
        "written_at": "2026-01-02T00:00:00Z",
        "supersedes": "count-01.json",
        "statement": "There are three blue tiles."
    }
}
for name, record in examples.items():
    with (records / name).open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
PY
```

Build the map, keeping its output outside the selected `records/*.json` glob:

```sh
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json build --glob 'records/*.json' --built-at 2026-01-03T00:00:00Z --title 'Invented tile records'
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json verify
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json orient
```

The fixed `--built-at` value is a supplied tutorial label; use your observation
time for your own builds. `--root` selects the corpus directory. A relative
`--fold` is resolved inside that root. Both options go before the subcommand.
Quote each `--glob` so the tool receives the pattern rather than shell-expanded
filenames; repeat it to select more patterns.

For this example, `verify` reports `verdict: CLEAN`, two checked and two unchanged.
`orient` reports **2 records, 1 edge, 1 displaced, 1 current**. The first record
remains on disk: it is displaced in the reading list because the second record
explicitly declares that it supersedes it.

## Read the map and reopen the evidence

```sh
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json threads
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json plan
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json expand records/count-01.json
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json render
```

`threads` groups declared lineage. `plan` lists the current reading set; its
optional `--query` filters recorded path/id/decision/schema fields, not the full
source text. `expand` reopens the named record and prints whether its bytes still
match. Check that printed `UNCHANGED` or `CHANGED` status; its exit code alone
does not distinguish those two outcomes. `render` creates `fold/index-01.md`.
Overview commands read the saved map; run `verify` before relying on its currency.

## Detect a new record

Add another invented record without editing either original:

```sh
python3 -B - <<'PY'
from pathlib import Path
with Path("corpus-demo/records/note-01.json").open("x", encoding="utf-8") as stream:
    stream.write('{"id": "note-01", "statement": "The tiles are square."}\n')
PY
python3 -B lodestone.py yggdrasil corpus-fold --root ./corpus-demo --fold fold/index-01.json verify
```

This verification intentionally exits **1**, with `verdict: DRIFTED` and
`records/note-01.json` in `added_since_build`. Verification also reports changed,
missing or rejected bound paths. Publish a new map such as `fold/index-02.json`
when you choose to include the new state. Keep the earlier map as its dated
record. Existing build/render outputs are refused unless you explicitly request
`--replace`.

## Relationships and limits

- JSON relation fields such as `supersedes`, `corrects` and `amends` can name
  another record's full hash or unambiguous filename. Ambiguous names are not
  guessed. Use `amends` for a partial change: it keeps the original in the
  current reading set. `corrects` and `supersedes` declare full displacement.
- Markdown and other selected files can be bound by hash, but JSON fields supply
  the declared lineage. Similar titles or prose do not create a relationship.
- `cohorts` groups records connected by shared cited artifact hashes. That is
  association, not lineage, and distant members need not share one artifact.
  `artifact SHA256` inspects the retained shared-citation table; it is not a
  general search for every singly cited hash.
- `lint --glob 'records/*.json'` reports `LINKED`, `ANCHORED` and `ISLAND` records.
  It returns 1 when islands exist; it does not invent relationships to fix them.
- The CLI build limits are 20,000 records, 32 MiB per file, 512 MiB total and a
  900-second checked deadline. A bound violation refuses the build. The shared
  citation table retains at most 2,000 rows and reports its coverage. A deadline
  check is not an operating-system process timeout.

The map is derived navigation, not byte compression, a backup, authentication,
semantic validation or permission to run a task. Preserve source files and use
the [archive toolkit](archives.md) when you need recoverable file copies.
