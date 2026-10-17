# Checkpoint and resume a task

The compact-context workflow saves enough task state to resume after a session
ends or its context is shortened. It preserves explicit source references so
that the next session can detect changed files before relying on a summary.
The checkpoint helper uses only Python's standard library and makes no network
calls.

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
File selection and notes can contain sensitive information: choose only the
material needed for this task and keep private checkpoints out of publication.

## Verify and resume

```sh
python3 -B tools/continuity/checkpoint.py verify checkpoint-01.json --root .
```

On a different machine, `--root` selects the relocated project with the same
relative source names. No original absolute project path is saved. A `VERIFIED`
result means the named file bytes match. `DRIFT` identifies `CHANGED`, `MISSING`
or `UNREADABLE` sources. Invalid checkpoint JSON or a changed checkpoint digest
is an error. Exit codes are 0 for successful creation or verification, 1 for
source drift and 2 for an invalid request or checkpoint.

Read the relevant matching files before resuming the next action. If a source
has changed, inspect it and reconcile the recorded claims; append a new
checkpoint when the current state is understood. Verification does not establish
that notes are true, tests passed, remote sources are current, or an action is
still authorized. It does not replay the next action automatically. The named
sources and the checkpoint must be backed up separately when recovery from disk
loss is required. File modes, timestamps, unselected files and remote services
are outside this check's contract.

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
