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

## Policy compatibility in 0.3.0

Operational fields are now validated before a policy is pinned or a command is
dispatched. Malformed shapes and incorrect types are rejected, including scalar
record/write roots, incomplete writer rules, non-list interpreter flags and a
boolean, negative or fractional wake budget. Duplicate keys and nonfinite numbers
are rejected too. This can expose an invalid custom policy that older versions
accepted until a later operation.

Valid neutral `lodestone-conscience-policy/v1` policies remain supported.
`interpreter.pinned` stays optional; when supplied it must be nonempty text.
Optional descriptive metadata is retained. A pin field alone is not interpreter
authentication; Nurarihyon's deployment identity checks are separate.
If your custom policy is rejected, start from the included
`tools/ecosystem/conscience-policy.json`, carry over your intended writer/root
rules with the documented types, and inspect the error before dispatch. Do not
remove validation or change an initialized controller's sealed policy to bypass
it; prepare a separate valid configuration when that boundary requires one.

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


## Nurarihyon binding

The optional reconciliation profiles prepared by `nurarihyon configure` seal
an explicit actor, assessment workspace and hashes of the selected Conscience
policy/modules into the controller policy. This preserves the existing public
standalone interface while adding a specific runtime admission condition.

Before a reconciliation worker starts, the controller derives its exact argv,
private stage and write effects from the fixed toolkit implementation. Conscience
assesses both the command and those effects. `DEFER`, `NEEDS_HOST` and `REFUSE`
prevent that worker from starting and retain the custody outcome. An `ALLOW`
verdict still cannot approve a task: exact host plan approval, code/policy/source
identity, STOP, locks, budgets and independent result acceptance all remain.

This assessment covers the mapped reconciliation worker's effects, not every
controller action or arbitrary native code. It is not a universal security
sandbox. The process-local audit restrictions and POSIX controls have their
stated limits; the trusted local account can change code/files. Participant or
actor labels are attribution, not authentication. Imported documents and retained
candidates cannot supply authority, replacement modules or new policies.

Bindings use user-owned absolute paths and pinned bytes. A moved installation
must account for those external dependencies; recovery never rewrites sealed
policy or starts a service to make a binding appear valid. See the
[Nurarihyon recovery guide](nurarihyon-recovery.md).
