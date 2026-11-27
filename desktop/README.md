# Lodestone Desktop preview

Unified Loom provides a local Qwen3 reader and retained encounters and corrections.
Origami handoffs and toolkit execution have additional platform restrictions,
listed below.

The `0.4.0-preview.6` cross-platform preview targets Intel macOS, Ubuntu 24.04
x86_64 and Windows x86_64. The
[named release page](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.4.0-preview.6)
provides downloads when published, with the exact source commit, checksums and
native verification record. Apple Silicon, other Linux distributions, ARM Windows
and mobile platforms are outside this target set.

The public [native verification guide](https://github.com/zacheriasliang-glitch/lodestone/blob/main/docs/desktop-validation/portable-preview.md)
describes the release checks, retained evidence and local inference smoke.

This is a preview. A retained correction is made available to a later encounter;
the reader can still misunderstand or misapply it. Local practice does not update
model weights. The preserved 31-component programme map includes experimental and
unfinished research; installing the application does not complete that research.

## Current platform and function availability

This matrix describes desktop preview.6. Its local reader and retained-practice
workflow are available on all three targets; the full set of desktop functions
does not yet have platform parity.

| Function | Intel macOS | Ubuntu 24.04 x86_64 | Windows x86_64 |
|---|---|---|---|
| Local Qwen3 reader, owned worker cancellation and timeout | Verified in release checks | Verified in release checks | Verified in release checks |
| Retain an unresolved correction and deliver it to a later encounter | Verified in release checks | Verified in release checks | Verified in release checks |
| Save/reopen conversations, projects and unsent drafts | Verified in release checks | Verified in release checks | Verified in release checks |
| Install, reinstall, upgrade, roll back and remove the application while preserving retained records | Verified in release checks | Verified in release checks | Verified in release checks |
| Human-curated Origami handoff from a retained encounter | Implemented with POSIX custody | Implemented with POSIX custody | Unavailable; preparation and completion refuse |
| Execute an operation from the desktop toolkit catalogue | Explicitly approved trusted subprocesses; operation prerequisites apply | Explicitly approved trusted subprocesses; operation prerequisites apply | Unavailable for every catalogue operation; process and operation portability remain unfinished |

The release checks cover native installation, GUI behavior, process supervision
and two local-reader encounters per target. They establish actual generation and
exact correction delivery, not whether the reader understands or applies that
correction. The handoff and toolkit rows describe implementation and prerequisites;
the release's two-encounter smoke does not exercise every toolkit operation.

Windows toolkit refusal includes deterministic memory, board and source-inspection
operations. The current POSIX catalogue runs trusted subprocesses with bounded
output, timeouts and owned process groups. This does not provide filesystem or
network isolation. A Windows port needs equivalent process ownership and an audit
of each operation's dependencies; a kernel sandbox is not a prerequisite imposed
by this catalogue. Human-curated Origami handoffs have a separate POSIX custody
requirement and also remain unavailable on Windows. These are product portability
gaps. The historical 31-component programme map and its research criteria remain
unchanged.

## Install

Choose the ZIP for your operating system and architecture from the named preview
release, and compare its SHA-256 with the release's `SHA256SUMS.txt` before running
setup. Extract the entire ZIP; keep its files together. Setup obtains the pinned
application runtimes, dependencies and local model and verifies their hashes.
Existing Python, Git, model accounts and API keys are not required.

Plan for at least 12 GiB of free disk space for the initial downloads, extraction,
retained cache and application; retained releases and practice history need more
space over time. A machine with 16 GiB of RAM is recommended. These are planning
allowances, not measured minimum requirements or performance guarantees. First
setup needs internet access. Local inference subsequently uses the installed
files and a private loopback model process, with no model subscription or paid API
calls. The GUI requires a graphical desktop session.

Installation and data paths must not pass through symbolic links or junctions.
Use ordinary local directories so the installer and retained-record store can
verify their ownership throughout each operation.

| Target | Setup entrypoint | Open after setup |
|---|---|---|
| macOS 12 or later, Intel x86_64 | `Install Lodestone.command` | `Lodestone Preview.app` in your user Applications folder |
| Ubuntu 24.04, x86_64 | `Install Lodestone.sh` | Lodestone Preview in the desktop applications menu |
| Windows 11 or Windows Server 2022, x86_64 | `Install Lodestone.cmd` | The installed `Lodestone Preview.cmd` launcher |

Windows Server needs a graphical Desktop Experience session. The Windows
preview requires OS build 20348 or later; this is not a claim of verification
on every later Windows release.

### Intel macOS

Double-click **Install Lodestone.command** in the extracted folder. This preview
is unsigned and not Apple-notarized. If macOS blocks it, use Apple's
[instructions for opening an app from an unidentified developer](https://support.apple.com/en-us/102445)
after verifying the download. Setup does not change macOS security settings.

### Ubuntu 24.04

Open a terminal in the extracted folder and run:

```sh
bash "Install Lodestone.sh"
```

The application and its Python environments install under your user account.
The script checks the OS prerequisites and uses `sudo apt-get` if any are missing:
`curl`, `ca-certificates`, `libgtk-3-0t64`, `librsvg2-common`, `at-spi2-core`,
`libwebkit2gtk-4.1-0`, `libnotify4`,
`libportaudio2`, `libgl1`, `libegl1`, `libsm6`, `libxtst6`, `libsdl2-2.0-0`,
`libglib2.0-bin` and `bubblewrap`. These provide HTTPS downloads, GTK/WebKit,
audio, SVG icons, accessibility services, graphical libraries, desktop launching
and command isolation. An administrator can install these packages first;
`--no-system-deps` then checks
their presence and refuses if any are missing. It does not skip the prerequisite
check. `--no-open` completes setup without opening the GUI.

### Windows

Compare the downloaded ZIP's hash using PowerShell's
`Get-FileHash -Algorithm SHA256`. After verifying the archive and its source,
use the ZIP's **Properties → Unblock** option if present, then extract it.
Double-click **Install Lodestone.cmd**. The console shows setup progress and stays
open if setup fails. It downloads the pinned Python archive, checks its hash
before extraction and execution, and retains failed download/staging files for
inspection. Application files install under your account. The Windows long-path
prerequisite described below may require administrator approval.

The `.cmd` entrypoint runs native Windows commands and small built-in PowerShell
checks. It does not load a PowerShell script or change execution policy. From a
Command Prompt in the extracted folder, optional arguments work as follows:

```bat
"Install Lodestone.cmd" --no-open
```

`--no-open` completes setup without opening the GUI or pausing on failure. Paths
may contain spaces and Unicode. This bootstrap refuses command-shell
metacharacters such as `%`, `!`, `&`, `^` and parentheses in its extracted folder,
data root, launcher or cache path before creating files. Choose ordinary directory
names if it reports this restriction. `--root`, `--app` and `--cache` accept
absolute local-drive paths.

Before downloading, setup checks these paths against the longest members of the
pinned archives and wheels, including temporary and retained interrupted
environments. It reports the deepest member and remaining UTF-16 path budget.
The default data root usually requires Windows long-path support. If the check
refuses, choose a shorter directory you own with `--root`, or explicitly run
**Enable Windows Long Paths.cmd** from the verified download. That separate
helper asks you to type `ENABLE` and may request administrator approval through
UAC. It changes only `LongPathsEnabled` to `1`; it never changes execution policy
and is never run automatically by setup. Managed-host policy still applies.

The helper first saves the previous value, including whether it was absent, and
an exact restore command under
`%LOCALAPPDATA%\Lodestone Windows Setup\receipts`. Keep that receipt if you want
to undo the setting later in an administrator terminal. Restart setup after the
change. Extraction, cache, bootstrap and launcher paths must still fit their
reported limits, even when long-path support is enabled.

To reopen the installed application, press **Win+R**, enter `shell:programs`, and
open **Lodestone Preview.cmd** in that folder. This uses the installed launcher
directly and does not depend on Start-menu search or pinning.
This preview's Windows launcher keeps a console window open alongside the GUI.
Quit through the application's menu or main window so it can save normally.

**Advanced PowerShell route:** the same pinned setup remains available as
`powershell.exe -NoProfile -File ".\Install Lodestone.ps1"`, with `-Root`, `-App`,
`-Cache` and `-NoOpen`. This script is unsigned. If it carries a download block,
review that specific file and use `Unblock-File -LiteralPath
'.\Install Lodestone.ps1'` before retrying; see Microsoft's
[Unblock-File documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/unblock-file).
This unblocks only that file and does not change execution policy. If host or
organization policy prohibits unsigned scripts, that optional route remains
blocked. Neither entrypoint changes the host's security policy.

## Use the preview

Open Lodestone Preview, enter an encounter and its purpose, then send it to the
local reader. Use the feedback controls to retain a correction and inspect what
happens in a later encounter.

Unsent drafts, their purpose and the selected configured model survive normal
quit and reopen. Saving a project as a `.bskc` file also preserves its purpose.
Older projects remain readable. The optional purpose metadata leaves the database
migration revision unchanged so the preceding preview can still read its records;
that older application does not gain the new draft-purpose behavior.

**Practice options** can select named retained lessons for one encounter and set
an explicit window of recent practice. A lesson must have a valid linked Bezoar
pattern before it can be used; pairing does not establish that its advice applies
to the current task. Options are saved with the draft and project. Changing the
message or purpose requires reviewing or clearing those options before sending.
Ordinary submissions continue to use the configured selection.

For an explicitly structured decision case, the optional **evidence-first**
response asks the reader to select a complete source observation before choosing
its action and whether to hold or change. Every supplied observation and action
remains available. This can still produce a wrong decision. A separate decision
review checks the original response against the attributed case policy; it never
replaces that response with a computed answer.

Explicit structured responses also receive an automatic **machine structural
assessment** after their original outcome is retained. It checks source identity,
evidence selection and internal transition consistency. Without a declared
condition/action policy, a structurally consistent response stays **UNRESOLVED**;
this check cannot establish that the action is correct. Generation completion
and this assessment are separate statuses. Open **Correct → Structured case
review** to inspect the saved machine finding and the unchanged raw answer.

Machine findings are retained as attributed Bezoar evidence, separately from
human corrections. To reuse a definite **REJECTED** finding, explicitly select
its evidence handle in **Practice options**. The existing voxel section then
delivers it as a named knot: what the case contract required and what was
returned, keeping evidence, action and transition separate. Unresolved findings
cannot be delivered as advice. Findings are recorded after the outcome with
unknown surprise; they are not presealed predictions, probability measurements,
weight updates or demonstrated learning. Ordinary calls omit this assessment
and knot selection, and old study records are not retroactively changed.

A `.bskc` project contains the conversation and its saved settings. Native learning
history and correction receipts remain in the installation's data directory;
copying the project file alone to another installation does not transfer them.
Keep that data directory when backing up your practice, and use the explicit
memory-import tools below when moving retained learning into another workspace.

The preview keeps its launcher and data separate from existing installations:

| Target | Data root | Launcher |
|---|---|---|
| macOS | `~/Library/Application Support/Lodestone Preview` | `~/Applications/Lodestone Preview.app` |
| Ubuntu | `$XDG_DATA_HOME/lodestone-preview`, or `~/.local/share/lodestone-preview` | `$XDG_DATA_HOME/applications/lodestone-preview.desktop`, or `~/.local/share/applications/lodestone-preview.desktop` |
| Windows | `%LOCALAPPDATA%\Lodestone Preview` | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Lodestone Preview.cmd` |

A relative `XDG_DATA_HOME` is ignored. The shell and Windows `.cmd` installers
accept `--root`, `--app` and `--cache`; the optional PowerShell installer accepts
`-Root`, `-App` and `-Cache`.
Keep the same data root and launcher path when upgrading. Back up the complete
data root to preserve native learning history, failed attempts, corrections,
conversations and unsent drafts. Models, cached downloads and old release folders
also remain there.

Your existing Loom and Lodestone workspaces stay in their existing locations.
The new workspace starts with clearly labelled instructional examples, not a copy
of another person's records. Imported evidence never supplies permission to run
commands. Model generation is bounded and failures remain available for review.

## Manage this installation

Use the persistent `worker_python` interpreter and `release` directory recorded
in the data root's `ACTIVE.json`. These paths include the selected dependency set
and release identity; do not substitute a temporary bootstrap interpreter.

On macOS, read those paths using the operating system's JSON-capable `plutil`:

```sh
preview_root="$HOME/Library/Application Support/Lodestone Preview"
worker_python="$(/usr/bin/plutil -extract worker_python raw -o - "$preview_root/ACTIVE.json")"
preview_release="$(/usr/bin/plutil -extract release raw -o - "$preview_root/ACTIVE.json")"
"$worker_python" -I -B "$preview_release/install.py" --root "$preview_root" doctor
```

On Ubuntu, open the data root's `ACTIVE.json` in a text editor and copy its
`worker_python` and `release` values into the two placeholders below. These are
filesystem paths, without the JSON string's enclosing quotes; use the decoded
path if your editor displays JSON escapes. This route does not require another
Python or JSON parser to be installed:

```sh
preview_root="${XDG_DATA_HOME:-$HOME/.local/share}/lodestone-preview"
# If XDG_DATA_HOME is relative, use "$HOME/.local/share/lodestone-preview".
worker_python='/paste/the/worker_python/value/here'
preview_release='/paste/the/release/value/here'
"$worker_python" -I -B "$preview_release/install.py" --root "$preview_root" doctor
```

On Windows, PowerShell reads the same fields directly:

```powershell
$PreviewRoot = Join-Path $env:LOCALAPPDATA 'Lodestone Preview'
$Active = Get-Content -LiteralPath (Join-Path $PreviewRoot 'ACTIVE.json') -Raw | ConvertFrom-Json
$WorkerPython = $Active.worker_python
$Installer = Join-Path $Active.release 'install.py'
& $WorkerPython -I -B $Installer --root $PreviewRoot doctor
```

Use your custom data root if you selected one. Replace the final `doctor` with
`open`, `rollback` or `uninstall` for those operations; `open` launches the GUI.
`doctor` checks the selected payload, dependency identities and learning
configuration without making a model call. After removal, `UNINSTALLED.json`
retains the former interpreter and release paths. To reinstall, run the verified
setup entrypoint for the same target and data root.

Close the application normally before upgrading, rolling back or removing it.
Install a later verified download to upgrade. Failed activation, rollback and
removal attempts retain recovery transactions and restore the previous managed
files. An interrupted transaction is recovered before the next launch or
installation change. Rollback restores the prior activation while retaining all
user records and conversations. Removal deactivates the app and keeps the data,
models and release history in the data directory. On a first installation,
rollback deactivates the application because there is no earlier version.

Optional `import-memory SOURCE NEW_DESTINATION --seed bezoar:...` validates and
copies an explicitly selected native record closure into a separate workspace.
It does not switch the current workspace or import launchers, credentials,
execution queues or host configuration. Preserve the source as your original.
To use the imported records, place `NEW_DESTINATION` inside the preview's data
directory, close the preview normally, then run `select-memory NEW_DESTINATION`.
Selection validates the import again, backs up the old selection, and updates both
Loom and Origami. Imported records remain advice and do not grant execution authority.

For a large retained history, `import-memory SOURCE NEW_DESTINATION --seed bezoar:...
--history-references` explicitly creates a separate reference-history workspace.
The source records remain unchanged; the import preserves their bytes. Select it
with `select-memory NEW_DESTINATION` using this supporting release. Selection
checks the actual runtime and workspace capability and saves both previous
selection files under `learning/selection-history` before changing them.

Every practice in that workspace needs an explicit **Practice options** history
window: choose 1–32 recent runs. Omitted runs stay
in the native archive and are named in the reader view; they are not delivered to
the model. Selecting a window does not guarantee that the remaining input fits.
The current non-windowed Origami route refuses this workspace. Ordinary imports
and fresh installations continue using the original format by default.

Reference-history workspaces require a compatible runtime. Before rollback, the
current installer checks the selected records and both selection files with the
target reader. It refuses an incompatible pairing before changing active pointers.
To roll back to an older reader, first select a validated compatible v1 import, or
restore the exact saved pre-selection `ACTIVE.json` and `learning-policy.json`
together while the application is closed. Keep the newer workspace intact: its
outcomes and corrections are not rewritten for the old reader. Use this release's
installer for the compatibility check; an older installer does not gain it.

## Optional voxel teaching in practice

The runtime API and configured `loom-practice packet` command can carry an
explicit canonical `voxel_context` beside retained practice. Use
`--voxel-context voxel.json` on an adapter packet, or `voxel=...` on
`UnifiedLoom.run` / `LoomBridge.run`. See
[runtime voxel instructions](runtime/docs/loom-voxels.md) for the exact format and
improvement-command route. This release adds no voxel control to the GUI.

The optional section preserves the history selected without it. If the combined
input is too large, the attempt refuses instead of discarding additional history.
Advice tied to an earlier purpose remains labelled as history. Ordinary requests
without this option keep their existing prompts. This connection does not run the
probability probe, train weights, or establish improved decisions.

## Bounds and components

The default reader is the quantized Qwen3-0.6B base. It has a small model capacity,
an 8,192-token input/output budget and a configured 4 GiB worker limit. On macOS
and Linux, the supervisor samples the worker process group's resident memory
(RSS); this is an observed limit, not an instantaneous allocation ceiling. On
Windows, a Job object owns the worker and its children, applies a 4 GiB committed
memory limit, and reports working-set/RSS observations separately. Committed
memory and resident memory are different measures; neither limit describes total
system RAM use by the OS and GUI. Cancellation and timeout stop the owned worker
processes. A Windows Job does not provide filesystem or network isolation.
Long histories can reach the native context limit; an error or retained refusal
must be inspected before another attempt. Some machines will respond slowly.

Conscience assesses the supported practice contract. Nurarihyon records actual
dispatch outcomes and reconciles interrupted work. Bezoar retains evidence and
corrections; Origami provides curated handoffs on macOS and Linux; the toolkit
catalogue reports each operation's requirements. Optional research/training tools are not dependencies
of the installed local reader and are not run by installation.

Every toolkit catalogue operation is currently unavailable on Windows because
its process and operation portability is unfinished. The POSIX catalogue executes
explicitly approved trusted subprocesses without a kernel sandbox. Human-curated
Origami handoffs separately refuse on Windows because their custody path requires
POSIX. The local Loom reader and its retained learning lifecycle remain available
there.

External Origami command providers and garden execution have a separate kernel
confinement requirement. Those routes refuse on Windows until an equivalent
isolation mechanism is available. On Linux they require working bubblewrap and
refuse when host policy or kernel restrictions prevent it from starting; installing
`bubblewrap` does not override such restrictions. Inspect the catalogue and the
retained refusal before retrying.

On stock Ubuntu 24.04, AppArmor restricts unprivileged user namespaces, so
bubblewrap cannot start and those optional command routes refuse. An
administrator may opt in with `sudo "./Enable Linux Command Fence.sh"`. It loads
upstream AppArmor's `bwrap-userns-restrict` policy, byte-for-byte and pinned by
hash (the policy Ubuntu 25.04 enables by default): `/usr/bin/bwrap` may use user
namespaces, and every program it starts runs without capabilities. The global
restriction stays on; `--remove` unloads and deletes it; it refuses to touch any
profile it did not write. The normal installer never runs it.

The GUI uses native wx controls. The optional `accessible-output3` speech/braille
plugin is not bundled because the target dependency sets lack pinned compatible
wheels. This does not establish screen-reader or braille accessibility on any
of the three platforms; that still needs native assistive-technology testing.

See `THIRD_PARTY.md` and the included source licences. The distribution includes
the modified GUI source and the runtime source used by this release. Models and
dependencies are obtained from the pinned upstream URLs in the target archive's
`dependencies.lock.json`. The source tree also retains the Ubuntu and Windows
locks as `dependencies.ubuntu-24.04-x86_64.lock.json` and
`dependencies.windows-x86_64.lock.json`. The combined desktop distribution is
GPL-2.0-only; the separately usable runtime retains its MIT licence.
