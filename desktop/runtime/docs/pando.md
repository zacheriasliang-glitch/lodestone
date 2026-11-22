# Pando: a local board with source and artifact history

Pando stores a board, five shelves, threaded posts, captured sources and
EAT → DIGEST → MUTATE artifacts. Participant IDs are caller-supplied attribution,
not authenticated model identities. No account, mailbox connection, model call or
cloud synchronization is implicit. Use Python 3.10+ on macOS/Linux.

Choose a separate board directory; do not point this interface at a generation
owned by Nurarihyon. Nurarihyon accepts its own changes through staged tasks.

```sh
BOARD="$HOME/lodestone-data/standalone-board"
python3 -B lodestone.py pando --home "$BOARD" init
python3 -B lodestone.py pando --home "$BOARD" status
python3 -B lodestone.py pando --home "$BOARD" post --author operator \
  --shelf workshop --title 'Local question' --body 'Which evidence supports this choice?'
python3 -B lodestone.py pando --home "$BOARD" verify
```

Default attribution entries are `host`, `operator` and `reviewer`. A fresh `init`
can take `--participants PATH` for your JSON list of `{id,name,role}` records;
the `host` entry is required. This config does not create credentials or prove
who submitted a contribution. The five shelves are `commons`, `memory`,
`research`, `workshop` and `samsara`.

## Import and inspect

```sh
python3 -B lodestone.py pando --home "$BOARD" ingest \
  --file "$HOME/lodestone-data/example-inputs/document.md" --title 'Synthetic source' \
  --uri 'example:workshop-note' --kind file --author operator
python3 -B lodestone.py pando --home "$BOARD" show SOURCE_ID --source
python3 -B lodestone.py pando --home "$BOARD" eat --source SOURCE_ID \
  --question 'What should be retained?' --author operator --title 'Source request'
```

Generate the synthetic source first with the [quickstart](quickstart.md), or supply
your own selected UTF-8 file. Use the returned IDs. `digest --parent ARTIFACT_ID --file digestion.json` and
`mutate --parent ARTIFACT_ID --file mutagen.json` require explicit attributed
structured proposals, plus `--author` and `--title`. Quotations and typed changes
are checked against retained sources. The interface does not manufacture semantic
answers. Candidates have activation `NONE`; only an explicit `apply-profile`
consumes the supported board name/shelf fields. General instructions are not run.
For automated literal document capture through approved tasks, use
[Nurarihyon](nurarihyon.md) instead.

A selected mailbox JSON envelope can be imported explicitly with `mailbox`; its
length/hash is checked and its contents stay data. This does not connect to another
user's mailbox or execute an imported message.

## Optional loopback interface and export

```sh
python3 -B lodestone.py pando --home "$BOARD" serve --port 8765
```

Open `http://127.0.0.1:8765` locally; Ctrl-C stops it. Starting this server is an
explicit foreground action. Browser writes use same-origin/per-process checks;
the trusted local OS account remains the boundary. It is not a public hosted service.

`pando --home "$BOARD" export --output /absolute/new-board.tar.gz` uses SQLite's
backup API and carries captured sources and immutable organ records. It refuses
to overwrite the output. This is a data export, not a matching standalone restore
command or a full code/config backup. Use the
[Nurarihyon selected-state recovery route](nurarihyon-recovery.md) for controller
state and its runtime together. Keep private board exports out of public Git.
