# Recover a notebook together with its captured sources

This procedure preserves a selected source-address operation: its notebook,
source bindings, captured files, accepted output and matching toolkit. It uses
the existing [file archive tools](archives.md). It does not create a controller,
start a daemon, submit a task or read a production home. There are no service or
STOP controls in this notebook operation. Existing services, STOP markers and
pending tasks remain separate and untouched.

The source-address commands are documented in
[the local-memory guide](../tools/local-memory/README.md). This guide describes
the recovery layout and commands; an executed recovery is established by its
separate receipts, not by this document.

## Select the complete operation

Prepare two explicit directory roots:

```text
toolkit/                  Exact reviewed toolkit, including lodestone.py
material/
  memory/
    records.jsonl         Existing notes and their identities
    links.jsonl           Existing relationships, including supersession
    state.json            Existing notebook metadata
  bindings.json           Existing source-binding manifest
  capture/                Original files at their bound relative paths
  EXAMPLE-OUTPUT.json      Previously accepted result, unchanged
  evidence/               Selected original review and provenance records
```

The filenames under `material/` are a delivery convention. Ordinary commands
also support other explicit paths. Keep `bindings.json` outside `memory/`, as
required by `source-bind` and `source-address`.

Preserve the selected existing notes, links and binding bytes. Include every
captured file needed by those bindings, not just the excerpt: a binding checks
both the whole-file and passage hashes. A saved URL is attribution and is never
fetched automatically. A result alone cannot replace the notebook, bindings and
capture it describes.

For Lodestone's recorded D1 example, these files already exist in its
`verification-01/example/` directory; `EXAMPLE-OUTPUT.json` is in
`verification-01/`. Its accepted passage is the existing toolkit README lines
3–4. Reuse those original records and review evidence rather than recreate an
equivalent-looking example. That passage was inspected during development and
is not unseen learning evidence.

Select only files authorised for this package. Do not include credentials,
tokens, session secrets, unrelated personal material, a production controller
home, service files, model weights or downloaded compilers. The generic archive
tool automatically excludes `.git`; it is not a secret scanner. Explicitly omit
caches and any other unwanted entries. Preserve prior backups and their evidence.

Use a quiescent single-writer notebook or a separately prepared immutable copy.
The packer detects observed changes to file bytes and inventory, but does not
make concurrent notebook writes transactional. When staging from accepted
records, verify selected source hashes, copied bytes and unchanged originals
before sealing the delivery. Do not change the historical evidence to fit a new
layout.

## Create a new archive with the existing implementation

The following commands assume `toolkit/` and `material/` are the explicitly
prepared roots above, and their current directory has room for a new archive.
The example limits are for a small selected notebook delivery, not a general
backup ceiling. Verify the selected inventory fits before packing it.

```sh
python3 -B toolkit/lodestone.py yggdrasil archive plan \
  --root toolkit=./toolkit --root material=./material \
  --exclude-name __pycache__ --exclude-name .DS_Store \
  --max-entries 1024 --max-bytes 8388608 --max-seconds 30

python3 -B toolkit/lodestone.py yggdrasil archive pack \
  --root toolkit=./toolkit --root material=./material \
  --exclude-name __pycache__ --exclude-name .DS_Store \
  --destination ./source-memory-archive \
  --epoch selected-source-memory-copy \
  --authority-ref explicit-owner-selected-copy \
  --max-entries 1024 --max-bytes 8388608 --max-seconds 120
```

`plan` inventories metadata only. A successful `pack` result is
`ARCHIVED_AND_VERIFIED`, and includes `descriptor_sha256`. Preserve that exact
value independently of the archive, for example in a separately retained
`source-memory-archive.descriptor.sha256` file and the delivery receipt. The
archive directory must be new and cannot overlap either source root.

The archive contains `archive.json`, `manifest.json` and `archive.tar.gz`.
The descriptor binds the manifest and payload; the payload deduplicates equal
file bytes. Packing rechecks source hashes and inventory before publishing its
completion descriptor. Preserve partial output and the original files if any
step fails. A successful archive is not evidence that it has been restored or
stored off-device.

The command's `authority-ref` is a record of the existing authorisation. Writing
that string cannot grant permission for additional access, execution or upload.

## Keep a trusted reader outside the archive

Recovery needs an interpreter and archive reader before the archived toolkit
can be recovered. Do not rely on executing an unverified extracted file to
verify its own archive.

A self-contained selected delivery can include an independently pinned copy of
the existing `src/lodestone_machinery/` source tree as `bootstrap/`, together with
a small `recover.py` entry point. That entry point calls the same
`lodestone_machinery.archive_restore.restore` function with an explicit expected
descriptor SHA-256. It does not implement another archive format or extractor.
The delivery manifest must identify these bootstrap files as well as the
archive and the externally recorded descriptor pin. The archive itself contains
the complete toolkit for the later source lookup.

Alternatively, a separately trusted copy of this toolkit can supply the same
existing archive CLI. The next commands use that public CLI. Replace
`TRUSTED_TOOLKIT` and `EXPECTED_DESCRIPTOR_SHA256` with the verified reader path
and independently retained pin from the actual delivery; do not compute the
expected pin from the currently supplied archive and treat that as independent
authentication.

```sh
python3 -B TRUSTED_TOOLKIT/lodestone.py yggdrasil archive verify \
  --archive ./source-memory-archive \
  --expected-sha256 EXPECTED_DESCRIPTOR_SHA256 --max-seconds 120

python3 -B TRUSTED_TOOLKIT/lodestone.py yggdrasil archive restore \
  --archive ./source-memory-archive --destination ./recovered-source-memory \
  --expected-sha256 EXPECTED_DESCRIPTOR_SHA256 --max-seconds 120
```

Use the full default selection for this operation. A partial `--path` restore
can authenticate the archive while omitting a required notebook or source file.
The destination must be new. Restoration verifies all payload members, writes
only the selected restored paths and executes no recovered code. A wrapper's
exact invocation and limits belong in the reviewed delivery instructions.

## Reopen the recovered source

After trusting and verifying the recovered source, an explicit fresh-process
lookup from `recovered-source-memory/` is:

```sh
python3 -B toolkit/lodestone.py memory source-address \
  material/memory 'project knowledge' \
  --bindings material/bindings.json --scope material/capture
```

This invokes the recovered toolkit and reads only the supplied notebook,
manifest and capture. It requires no original checkout path, account, network
connection, model call or dependency download. A bounded recovery rehearsal
should make the original source paths unavailable in its isolation scope and
verify that the command still returns the recorded source bytes, identities,
relationships and explicit status. Original authoritative files need not be
deleted or moved to simulate loss.

Bindings already use relative source paths. Supply the new `--scope`; do not
rewrite the bindings or the memory record's historical `source` field. Archive
root paths record provenance; restored members are placed under their labelled
relative paths, not back into the original locations.

The old accepted output stays byte-identical. A newly generated output reports
the new absolute `scope`, so it need not be byte-identical to the old JSON.
Compare all other expected fields for this exact operation, including record
IDs, source/slice hashes, exact text, line coordinates, ambiguity and
supersession. Any allowed difference must be named before checking the result.

Changed or missing source bytes must retain the normal `STALE` or `UNAVAILABLE`
finding. A malformed, corrupted or wrong-pin archive must not be reported as a
successful recovery, and an existing destination must remain unchanged. Preserve
failed outputs and evidence. Do not reopen a completed successful verification
merely to produce another receipt.

## Recovery boundaries

Python 3.10 or later with its standard library is an external dependency.
The archive implementation uses POSIX no-follow directory operations and is
supported on macOS/Linux; native Windows recovery is outside this contract.
An offline package needs a compatible Python already available. Including a
download instruction does not make the interpreter part of an offline closure.
This notebook operation has no required model, provider or service dependency.

File archives preserve ordinary file bytes, directories, relative names,
symlink target strings and the metadata stated in `docs/archives.md`. They do
not preserve every ownership, ACL, extended attribute, hardlink identity or
machine setting. Absolute symlink targets are not automatically relocated.
The selected D1 README example has no symlink dependency. Do not generalise that
fact to arbitrary captures.

Source line numbers follow Python's universal line boundaries, as documented
in the local-memory guide; they can differ from editor or `wc -l` lines. A
verified address establishes exact bytes and coordinates, not factual truth,
semantic support, independent attribution or useful learned transfer. Caller
capture labels and source URLs remain caller declarations.

This is generic file recovery. The separate
[Nurarihyon selected-state procedure](nurarihyon-recovery.md) uses controller
custody and a consistent SQLite snapshot, preserves its selected STOP state
and restores inert controller history. It also has external sealed-policy and
outcome-memory dependencies. It does not automatically include this notebook,
capture or binding manifest, and should not be invoked just to package them.

Archive creation, independent copied-file verification, an actual restore,
ordinary recovered lookup, source publication and production activation are
different recorded actions. Completing this selected recovery does not finish
the 31-component research programme or erase its unresolved and negative results.
