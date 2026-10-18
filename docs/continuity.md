# Checkpoint and resume a task

The compact-context workflow saves selected task state for a later session.
It preserves explicit source references so that the next session can detect
changed files before relying on a summary. Comparing two checkpoints also
detects loss of declared evidence, notes and unresolved questions. This is an
explicit workflow; it does not intercept an agent application's compaction.
The checkpoint helper uses only Python's standard library and makes no network
calls.

Command help is available through `python3 -B lodestone.py continuity --help`
or `python3 -B tools/continuity/checkpoint.py --help`.

## Create a checkpoint

From an extracted Lodestone folder, select the project files that justify the
current result and next action. These examples use this package's own files;
replace them with the relevant files in your project.

```sh
python3 -B tools/continuity/checkpoint.py create --root . \
  --output checkpoint-01.json \
  --task "Review the portable toolkit" --status "IN_PROGRESS" \
  --next-action "Read the archive guide and choose an explicit fixture" \
  --source README.md --source docs/archives.md \
  --note "No scientific experiment has been run" \
  --unresolved "Archive recovery has not yet been tested in this task"
```

`--source` is repeatable. Each name is relative to `--root`, uses `/` separators,
and identifies a regular file. Directories and symlinks are rejected. The helper
does not discover or recursively collect files. It records paths, byte counts,
SHA-256 hashes, task, status, next action, optional notes and unresolved points;
it does not copy source contents. The size limits are 256 selected files,
64 MiB per file and 1 MiB per checkpoint. A concurrent source change detected
during hashing is an error.

The output's parent directory must already exist. Creation refuses an existing
output, including a symlink; use `checkpoint-02.json` for the next checkpoint.
These are immutable records **by the tool's write contract**, not protected
storage. Other programs can edit or delete them. A digest binds the recorded
fields and detects accidental edits; it is not a signature or publisher identity.
Creation flushes the file, but does not fsync its parent directory; it does not
promise survival of a crash or power loss.
File selection and notes can contain sensitive information: choose only the
material needed for this task and keep private checkpoints out of publication.

Retain the creation result's **`sha256`** independently: it hashes the complete
checkpoint file. **`checkpoint_sha256`** is a different digest of the recorded
fields. It cannot substitute for a raw-file pin.

## Verify and resume

```sh
python3 -B tools/continuity/checkpoint.py verify checkpoint-01.json --root .
```

That form checks internal consistency and current source bytes. To also bind
verification to the checkpoint originally saved, assign `checkpoint_01_sha256`
the creation result's saved `sha256` value, then run:

```sh
python3 -B tools/continuity/checkpoint.py verify checkpoint-01.json --root . \
  --expected-sha256 "$checkpoint_01_sha256"
```

Pins must be exactly 64 **lowercase hexadecimal** characters. Use a previously
recorded value from a trusted record; computing a new pin from an untrusted
checkpoint immediately before verification would only endorse its current bytes.

On a different machine, `--root` selects the relocated project with the same
relative source names. No original absolute project path is saved. A `VERIFIED`
result means the named file bytes match. `DRIFT` identifies `CHANGED`, `MISSING`
or `UNREADABLE` sources. Invalid checkpoint JSON or a changed checkpoint digest
is an error. A supplied raw-file pin is checked before parsing the checkpoint.

Read the relevant matching files before resuming the next action. If a source
has changed, inspect it and reconcile the recorded claims; append a new
checkpoint when the current state is understood. Verification does not establish
that notes are true, tests passed, remote sources are current, or an action is
still authorized. It does not replay the next action automatically. The named
sources and the checkpoint must be backed up separately when recovery from disk
loss is required. File modes, timestamps, unselected files and remote services
are outside this check's contract.

## Check what survives a handoff

Use `preserve` when both checkpoints refer to the **same task and source
revision**. The candidate must retain every predecessor source tuple (relative
path, byte count and SHA-256), every note and every unresolved string. Text is
compared exactly, including duplicate counts. Reordering and additions are
allowed; renaming, rewording or removing an old item fails this comparison.
Status and next action may change.

For example, after the first checkpoint above, add a guide without dropping
the existing evidence or the unresolved recovery question:

```sh
python3 -B tools/continuity/checkpoint.py create --root . \
  --output checkpoint-02.json \
  --task "Review the portable toolkit" --status "IN_PROGRESS" \
  --next-action "Choose an explicit archive fixture" \
  --source README.md --source docs/archives.md --source docs/continuity.md \
  --note "No scientific experiment has been run" \
  --unresolved "Archive recovery has not yet been tested in this task"
```

Save its returned raw `sha256` as `checkpoint_02_sha256`. With both saved pins:

```sh
python3 -B tools/continuity/checkpoint.py preserve \
  checkpoint-01.json checkpoint-02.json --root . \
  --predecessor-sha256 "$checkpoint_01_sha256" \
  --candidate-sha256 "$checkpoint_02_sha256"
```

Both mandatory raw-file pins are checked before either checkpoint is parsed.
The tool then checks retained items and reopens **every candidate source,
including additions**, against its recorded bytes. `PRESERVED` means both
checks passed. `LOSS` reports missing or replaced items; `DRIFT` reports source
bytes that no longer match. When both occur, the top-level status is `LOSS`
and the source failures remain in the result. Exit codes are 0 for creation,
verification or preservation success, 1 for loss or drift, and 2 for malformed
input, pin mismatch or an invalid request.

Equal source counts cannot hide an exchange of one required source for another.
Still, passing does not establish that the chosen sources answer every future
question or that a model will interpret them correctly. It checks declared
evidence retention, not semantic equivalence or reasoning quality.

When a source legitimately changes or an unresolved question is resolved,
review that transition and create a new baseline while keeping the earlier
records. Do not alter a predecessor or present a changed source revision as
preservation of the old one. These checks create no archive and make no atomic
snapshot or general crash-recovery guarantee.

## Install the compact-context skill

The portable skill lives at `skills/lodestone-compact-context/`. For a Codex
installation, run this from the extracted Lodestone folder:

```sh
python3 - <<'PY'
import os
from pathlib import Path
import shutil
source = Path("skills/lodestone-compact-context")
destination = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills" / source.name
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, destination)  # Refuses any existing destination.
print(destination)
PY
```

Use `$lodestone-compact-context` in a session where the skill has been loaded.
Keep the Lodestone package available and tell the agent its location; copying
the skill does not copy or install the toolkit. For another agent, use that
agent's supported skill directory or read `SKILL.md` directly. The workflow is
portable; automatic discovery depends on the agent application.

The skill saves and reopens task state. It cannot trigger, prevent or extend
native context compaction, recover unsaved messages, or confer execution
permissions. It installs no daemon and modifies no agent settings automatically.
