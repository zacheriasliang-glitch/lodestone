# Nurarihyon selected-state export and recovery

This procedure preserves one dedicated controller's selected state and runtime
source. It is not a full machine, whole laboratory or automatic cloud backup.
Export obtains controller custody and uses SQLite's backup API; copying a changing
database by hand does not provide the same consistency check.

## Make a stopped, consistent local snapshot

Use the package and policy that own your state. Request STOP explicitly, then
inspect status and verify that no task/worker is still running. Do not kill an
active worker or clear a failure marker to make the export proceed.

```sh
STATE="$HOME/lodestone-data/my-state"
python3 -B lodestone.py nurarihyon --home "$STATE" stop
python3 -B lodestone.py nurarihyon --home "$STATE" status
python3 -B lodestone.py nurarihyon --home "$STATE" verify
python3 -B lodestone.py nurarihyon --home "$STATE" export \
  --output "$HOME/lodestone-data/recovery-001"
```

The export destination must be new, with an existing parent, and must not overlap
source or state. Busy locks, changing state/source, disallowed paths or exceeded
limits fail without altering original state. Preserve partial output for diagnosis;
do not relabel it a complete archive.

A successful result is `EXPORTED_VERIFIED`. Keep the complete `recovery-001`
directory **and** its external `recovery-001.descriptor.sha256` file. The descriptor
pin must remain outside the package it authenticates. The export contains:

- Runtime source needed by the selected controller, Pando and machinery paths.
- A consistent controller database, immutable generations, source captures,
  artifacts, task/event history, internal lessons and the captured STOP condition.
- A file manifest, capture time, dependency inventory and restoration instructions.

It intentionally excludes transient locks, SQLite companions, Git/cache files,
known credential paths, services and unrelated state. This is not an exhaustive
secret detector: your ingested document text remains private data in the export.
Review it before any transfer. Do not publish a recovery package to a public repo.

## Verify and restore into a new isolated directory

```sh
python3 -B lodestone.py nurarihyon restore \
  --package "$HOME/lodestone-data/recovery-001" \
  --descriptor "$HOME/lodestone-data/recovery-001.descriptor.sha256" \
  --output "$HOME/lodestone-data/restored-001"
```

Success returns `RESTORED_VERIFIED_INERT`. The restorer checks archive structure,
member contents, required runtime files, the complete file manifest and SQLite
integrity. It neither executes the recovered code nor starts a service or task.
The stopped-source recipe above preserves STOP. Restore does not silently add or
clear a stop condition: an export taken with STOP clear retains that fact, while
remaining unexecuted. Read `metadata/package.json` and `metadata/RESTORE.md` first.

The result names the restored `runtime/` and `state/`. With trusted source and all
external dependencies available, a deliberate read-only status check is:

```sh
cd "$HOME/lodestone-data/restored-001"
python3 -B runtime/tools/nurarihyon/run --home state status
```

Do not clear STOP, retry a task or install a login service as part of verifying a
backup. A passing restore demonstrates this selected archive's recovery on the
tested environment, not a full disaster-recovery or reboot test.

## Version compatibility

Recovery is verified for exports produced by this 0.3.0 runtime. Older laboratory
controller exports are not supported migration inputs: the required runtime-file
closure has changed, and Pando may add roster metadata when opening an older board.
Keep such state with its original runtime and recovery instructions. Do not point
this package at an existing private installation to test compatibility. Preserve
old history inert; use fresh configuration and state for new public work.

## Dependencies outside this snapshot

Python 3.10+ and its standard library/POSIX facilities are required. Model weights
and cloud accounts are not needed. Host-specific service registration, interpreter
installation, external Conscience policy/module binding paths, and a separately
selected outcome-memory catalogue are not automatically made portable or backed
up by this selected-state export. Retain those user-owned dependencies separately
and inspect the manifest's external-dependency disclosure.

An existing sealed Conscience binding names exact absolute paths and hashes.
Moving the state does not rebind it. If those original dependencies are unavailable,
retained bytes/history can still be restored, but resuming that bound workflow is
not established. Editing its sealed policy is not a migration procedure. Keep the
original history inert and prepare new user-owned configuration/state for new work.
A service requires explicit fresh preparation against the intended runtime and
interpreter; no recovered configuration authorizes its own activation.

Restore requires a new destination and has bounds of 128 MiB logical contents,
16,384 entries and 180 seconds. It does not preserve every ownership, permission,
extended-attribute or machine setting. Check what your recovery objective needs.
A local export is not off-device protection until an independently stored copy
has been downloaded and verified. A Git source publication alone cannot recover
private board state, captures or outcomes.
