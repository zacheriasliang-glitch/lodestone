# Conscience: local readiness and command records

Conscience observes the package and the workspace you select, assesses an explicit
command, and records the result. It needs Python 3.10+ and the standard library.
It works from an extracted ZIP without Git, an account, credentials or services.
It is an advisory harness, not an operating-system sandbox or a permission grant.

From the extracted package directory:

```sh
python3 lodestone.py doctor
python3 lodestone.py --workspace ./my-workspace wake
python3 lodestone.py --workspace ./my-workspace conscience assess -- python3 --version
python3 lodestone.py --workspace ./my-workspace conscience run -- python3 --version
python3 lodestone.py --workspace ./my-workspace conscience catalog
```

On Windows use `python` instead of `python3` if that is your Python command.
Global `--workspace` and `--repo` options go before `wake` or `conscience`.
The default workspace is `~/.lodestone/workspace`; output never has to live
inside the extracted package. Each command uses the workspace explicitly selected
for that invocation. Moving the package does not move an existing workspace.

`doctor` reads local source availability and checks whether the selected workspace
or its nearest existing parent is writable. It creates nothing, runs no child
programs and makes no network requests. `READY` means those core requirements
are present; it is not a test-suite pass or a claim about optional numerical tools.
The optional porcelain environment has a separate setup described in its README.

`wake` creates the selected workspace when needed and publishes one waking record.
It pins local documentation, records readiness and local resource observations,
and follows the unique predecessor chain of earlier wakes. It does not start any
background process or execute a machinery job. By default an absent restart note
is normal. You may put `RESTART-README-YYYYMMDD.md` in `WORKSPACE/continuity`, or pass
`wake --runtime-dir PATH`. A present note requires a matching `.md.sha256` sidecar.
A missing or mismatched sidecar publishes a diagnostic waking record with a failed
check and exit code 2, regardless of which checklist items were scheduled. An
empty or whitespace-only sidecar returns an error before publication. Read the
diagnostic before retrying; `WOKEN` means a record exists, not that all checks passed.
The declared actor appears in both the record and the command result.

## Command assessment

`assess` publishes an assessment without executing the command. `run` executes
only a command with verdict `ALLOW`, leaves its stdout intact, writes the harness
summary to stderr and returns the child exit code. The harness consumes only its
leading `--`; a child's later `--` and arguments containing spaces are preserved.
`REFUSE`, `NEEDS_HOST` and `DEFER` do not execute the child and return exit code 2.
These verdicts describe the local policy; `ALLOW` confers no outside authority.

The default actor is `local-user`. The neutral policy has no preassigned people,
machine paths, external services or inherited grants. It permits ordinary writes
under the current directory or selected workspace, while protecting existing
`WORKSPACE/organs` records against recognized overwrite/removal commands. Reads
from record paths remain allowed. Copy destinations, destructive sources, symlink
aliases, simple shell redirects and bounded Git forms are assessed separately.
Unsupported forms of recognized commands return `NEEDS_HOST`.
The simple-shell parser accepts bounded POSIX-style syntax, including forward
slashes in paths; backslash escapes remain unsupported and return `NEEDS_HOST`.
It does not interpret native `cmd.exe` or PowerShell program effects.

The grammar is deliberately bounded. Arbitrary program internals, Python scripts,
plugins and race conditions are not contained or inspected. Record protection is
not an access-control boundary. Run only commands and downloaded code you trust.

To customize policy, copy `tools/ecosystem/conscience-policy.json` and pass
`conscience assess --policy PATH -- COMMAND` (or `run`). Explicit writer rules and
writable roots can separate collaborators. `--actor NAME` changes the recorded
actor; it does not authenticate that identity. The supplied training-launch rule
requires a `--preregistration` argument, but does not verify that argument's
scientific adequacy or authorize a study.

## Records and limits

Records live in `WORKSPACE/organs/conscience/records`. Canonical content hashes
identify bytes; records are published without replacement through the record API.
This is neither encryption nor a digital signature. Someone with filesystem
access can still alter or remove them; subsequent integrity checks detect damaged
records. No private memory or backup from another installation is inherited.

Wakes select predecessors by links, not hash order or timestamps. A fork, missing
predecessor, cycle, disconnected chain or damaged Conscience record prevents a
new wake or assessment until that history is inspected. Original records are never
rewritten to repair a chain. Catalog traversal has a 256-record default bound;
this release has no automatic rotation or concurrent-writer serialization. Use
one writer per workspace and retain separate workspaces for independent sessions.

`conscience compare HANDLE_A HANDLE_B` compares selected observations and history
digests. It does not prove semantic equivalence, compression quality or learning.
The golden-order helpers schedule a finite checklist; equal schedules are not
promised to desynchronize separate processes. Temperature (`COLD`, `WARM`, `HOT`)
is a policy heuristic over observed load/free space, not a thermal sensor.

The optional macOS/Linux launcher can be installed with
`python3 tools/ecosystem/install.py`; it pins this interpreter and package path,
adds no packages or services, does not edit PATH, and refuses to overwrite a
different launcher. `python3 lodestone.py` remains the portable entry point.
