# Experimental supplied-candidate storage

Origami and Yggdrasil expose the same optional `crystal latent put/get` command.
Ordinary `crystal put/get` and its literal/zlib/recursive strategies are unchanged.
This is an engineering integration of an existing explicit experimental storage selector, not a
candidate generator or evidence of learned transfer.

## Run the complete example

From the checkout root, with Python 3.10+ and only the standard library:

```sh
python3 -B examples/machinery/supplied_candidates.py /tmp/lodestone-candidates-example
```

The directory must be new. The example retains synthetic inputs, explicit plans,
two independent SQLite stores, external receipts, recovered bytes and
`result.json`. It exercises the actual Origami/Yggdrasil command dispatch and the
catalog API, closes and reopens both stores, and compares recovered bytes with
input. Model initialization, inference and compilation are guarded against
execution. Its placeholder model is labelled synthetic provenance; no R09 or
other original research artifact is used or regenerated.

The two candidate rows are explicitly supplied in preference order. A 28-byte
fixture exercises a cheaper later row, the `m=8` learned boundary, the `m=9`
literal tie, and a four-byte tail. The expected selected payload is 187 bits,
versus 227 bits using only the first supplied row. These correspond to 98- and
103-byte packets after the unchanged 74-byte header and padding. Both exceed the
28-byte input before any dependency, metadata or SQLite cost. Physical sizes
are measured, not portable constants.

## Use your own pinned inputs

Supply existing model provenance bytes, a 32,768-byte table, a complete candidate
plan, and their independently recorded SHA-256 pins. Nothing compiles a table or
generates rows implicitly. One source of at most 4,096 bytes is accepted:

```sh
python3 -B lodestone.py origami crystal latent put \
  --store candidates.sqlite --source-id example-v1 --file input.bin \
  --model model.bin --expected-model-sha256 MODEL_SHA256 \
  --table table.bin --expected-table-sha256 TABLE_SHA256 \
  --candidates candidates.json --receipt source-receipt.json --index-mode scan

python3 -B lodestone.py yggdrasil crystal latent get \
  --store candidates.sqlite --receipt source-receipt.json --output recovered.bin
```

The public launcher accepts `python3 -B lodestone.py origami crystal latent ...`
and `python3 -B lodestone.py yggdrasil crystal latent ...`. This is an explicit
experimental storage operation, not a registered Nurarihyon task toolkit.
`--index-mode` is a creation choice; it cannot convert an existing store.
Receipt and recovered output files must be new. Input files are never consumed.
Recovery requires the database and receipt only, and verifies the external
manifest hash and recovered source hash/length. Keep the receipt with the store.
Caller-supplied hashes establish byte identity, not authenticity or authority.

`crystal latent get` requires a quiescent database with no SQLite companion
files and rollback file-format read/write versions 1/1. It checks the fixed
100-byte SQLite header before opening SQLite. WAL-mode databases are rejected
unchanged even when cleanly closed with no `-wal` or `-shm` files; recovery does
not convert their journal mode. Invalid, truncated or other header versions
are also rejected. Concurrent access or modification during recovery is
unsupported; these checks do not lock out another process. This boundary applies
to this recovery command; ordinary crystal operations and latent `put` are
unchanged.

`load_candidate_bundle` checks the model and table byte hashes and size bounds.
It deliberately does not authenticate their compilation relationship or parse
the opaque model as a supported neural schema. These bundles can only be used
with an explicit plan; default neural encoding rejects them, including empty
sources. The report says `CALLER_PINNED_BYTES_NOT_COMPILE_AUTHENTICATED`.
Existing `prepare_model` callers retain their original behavior; this command
never invokes that API. The model provenance bytes are retained and charged
even though reconstruction only needs the packet, table and manifest.

## Candidate plan contract

The schema is `crystal-supplied-candidates/v1`, with exactly:

- `corpus_sha256`: SHA-256 of canonical JSON of the complete ordered new-source
  roster, each entry containing `id`, `sha256`, `bytes`, `mandatory`.
- `table_sha256`: hash of the exact table bytes.
- `sources`: the same ordered roster, each entry containing exactly `id`,
  `sha256`, `bytes`, `chunks`.
- Each chunk contains exactly `offset`, `sha256`, `bytes`, `candidates`.
  `candidates` has one ranked list per complete eight-byte block. Each list
  contains zero through 64 signed integer coordinate triples, values -8 to 7.
  Empty lists deliberately select literal. Tails have no candidate list.

Canonical JSON uses sorted keys, UTF-8, no ASCII escaping, compact separators
and no nonfinite numbers; the digest has no trailing newline.
`candidate_inputs.corpus_sha256` computes only the roster binding.
The example shows explicit plan construction without searching table rows.
The command rejects duplicate JSON keys and plan files exceeding 2 MiB.

All bindings, chunk offsets/lengths/hashes, block counts and coordinates are
validated before the command opens its store. Storage APIs validate again before
artifact writes and snapshot mutable candidate lists. Every supplied candidate
is validated and scored, including duplicates and entries after an exact match.
Literal costs 65 bits; learned storage costs `29 + 4m`, where `m` is the number
of incorrect nibbles. A learned choice must be strictly cheaper. Equal learned
costs preserve candidate order; literal wins a tie. The decoder and wire formats
are unchanged.

The Python API is `lodestone_machinery.latent.store_adapter.store_source(...,
supplied_candidates=plan)` or `compact_checkpoint.build_catalog(...,
supplied_candidates=plan)`. The former has one chunk of at most 4,096 bytes;
the latter uses 1,024-byte chunks, at most 64 sources / 262,144 total bytes.
Even an empty source has one empty chunk. Catalog plans bind the complete
**added** roster for an append, not previous contents. Candidate mode requires
one latent layer and nonmandatory sources. Literal, lifting, fixed-code,
mandatory and two-layer combinations are rejected. Catalogs containing mixed
mandatory/nonmandatory inputs therefore cannot use this optional path unchanged.

## Costs and limits

Reports keep payload bits, packet framing/padding, retained model/table/manifest
or directory bytes, and full physical SQLite bytes distinct. Logical bytes are
already represented inside SQLite and must not be added to its file size.
The command also charges its external receipt; whole-store costs include any
preexisting unrelated records. Store deltas are separate incremental quantities.
Content-addressed artifacts may deduplicate; scoring still precedes packet
deduplication. No cache implementation has changed.

Plans remain caller-owned and are not decoder dependencies. Their canonical
hash/size and input file size are reported. If retained for reproducibility,
charge that external copy too. The example inventories all retained input,
plan, output and database files, then reports its own report size separately.
Its two store costs are independent deployments; summing them is not a shared
deployment measurement. Candidate acquisition and historical model preparation
costs are unknown, not zero. `encoder_calls=0` means this operation bypassed
neural inference, not that encoding was free.

Timeouts are cooperative bounds checked around bounded operations, not a hard
process/RSS limit. The CLI `--max-seconds` applies separately to validation,
append and reopened recovery (maximum 300 seconds per phase); reported command
wall/CPU time covers the complete command. The catalog's structural maximum
is 2,097,152 candidate evaluations and 34,078,720 selection/writer nibble
comparisons, excluding validation, hashing, copies, serialization and I/O.
Each artifact append is atomic, but a multi-artifact operation is not a single
transaction. Operational failure can leave immutable intermediate artifacts;
the receipt is published only after recovery. There is no automatic retry.

## Port and verification provenance

The selected source is commit `5cd4cf8aa0211b0264607b0e17c9d91346bb22cc`, path
`experiments/001-rule30/Genepool/crystal-latent`, from the historical-base branch
`codex/cost-aware-candidates-20261023`. Its supplied-candidates integration note
and saved verification material were reviewed before this port. Only eight
runtime dependencies and scoped tests were moved into the current package;
the historical branch was not merged or blindly cherry-picked. Original
research files and outcomes remain untouched. Package provenance records exact
source hashes. The public 0.2.3 tree already supplies the byte-identical crystal and transformer
backend. The 0.3.0 port adds the existing ten-module latent package, the lazy
`crystal latent` dispatcher, synthetic example and seven focused test modules.
It preserves the original compatibility fixture and recorded source pins. No
private model, table, corpus or archived study output is copied. The package
contains optional numerical library functions, but the supplied-candidate CLI
and its engineering example never initialize, train, run or compile a model.

Ported tests cover the selector, both storage callers, malformed plans with
zero writes, default-path compatibility, frozen original-reader recovery and
actual command dispatch. Compatibility fixtures are original source bytes with
recorded pins, not regenerated research data. Run only this route’s synthetic engineering tests with:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests/machinery -p 'test_latent*.py' -v
```

Public candidate verification on macOS 12.7.6 / Python 3.12.7 passed all 55 focused
latent tests and 9 existing crystal CLI regressions. The guarded synthetic example
recovered its exact 28-byte input through both independent stores. Source pins were
unchanged during the check. This is development-tree evidence; final built-archive
installation, supported-platform CI and independent final review remain separate.
See [the capability map](capability-map.md) for scope and retained limitations.

The example and test fixtures prove engineering behavior only. Recovery
completion remains distinct from the unachieved learning goal. No training,
R09/MATCHED-SHARED-01 rerun, candidate-generation result or learned-transfer
claim is part of this integration.
