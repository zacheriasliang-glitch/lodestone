# Lodestone Desktop preview

Install Unified Loom on an Intel Mac, with a local Qwen3 reader, retained encounters
and corrections, Origami handoffs and the supported toolkit catalogue.

This is a preview. A retained correction is made available to a later encounter;
the reader can still misunderstand or misapply it. Local practice does not update
model weights. The preserved 31-component programme map includes experimental and
unfinished research; installing the application does not complete that research.

## Install

1. Download the named macOS x86_64 ZIP from the Lodestone GitHub preview release.
2. Extract it and double-click **Install Lodestone.command**. Setup downloads pinned
   application runtimes, dependencies and the local model, verifying their hashes.
3. Open **Lodestone Preview** in your user Applications folder. Enter an encounter
   and its purpose, then send it to the local reader. Use the feedback controls to
   retain a correction, and try a later encounter.

Unsent drafts, their purpose and the selected configured model survive normal
quit and reopen. Saving a project as a `.bskc` file also preserves its purpose.
Older projects remain readable. The optional purpose metadata leaves the database
migration revision unchanged so the preceding preview can still read its records;
that older application does not gain the new draft-purpose behavior.

A `.bskc` project contains the conversation and its saved settings. Native learning
history and correction receipts remain in the installation's data directory;
copying the project file alone to another installation does not transfer them.
Keep that data directory when backing up your practice, and use the explicit
memory-import tools below when moving retained learning into another workspace.

The first setup needs an internet connection and several gigabytes of free space.
Inference thereafter uses local files and a private loopback model process; no
model account, API key or subscription is required. Existing Python, Git and an
existing Lodestone installation are not required. Only the operating-system and
architecture combinations listed in the release verification are supported.

This preview is unsigned and is not Apple-notarized. macOS may require a manual
confirmation when opening the downloaded installer. Verify the release ZIP with
the accompanying `SHA256SUMS.txt`, and follow Apple's
[instructions for opening an app from an unidentified developer](https://support.apple.com/en-us/102445)
if macOS blocks it. Setup does not change your Mac's security settings.

The preview has its own application and data directory:

- `~/Applications/Lodestone Preview.app`
- `~/Library/Application Support/Lodestone Preview`

Your existing Loom and Lodestone workspaces stay in their existing locations.
The new workspace starts with clearly labelled instructional examples, not a copy
of another person's records. Imported evidence never supplies permission to run
commands. Model generation is bounded and failures remain available for review.

## Manage this installation

Use the Python runtime created by setup and the management script in the extracted
download. These examples use the default installation location:

```sh
"$HOME/Library/Application Support/Lodestone Preview/deps/python/3.12.12/python/bin/python3.12" -B install.py doctor
"$HOME/Library/Application Support/Lodestone Preview/deps/python/3.12.12/python/bin/python3.12" -B install.py rollback
"$HOME/Library/Application Support/Lodestone Preview/deps/python/3.12.12/python/bin/python3.12" -B install.py uninstall
```

Close the application normally before upgrading, rolling back or removing it.
Install a later verified download to upgrade. A failed activation restores the
previous active files. Rollback restores the prior activation while retaining all
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

## Bounds and components

The default reader is the quantized Qwen3-0.6B base. It has a small model capacity,
an 8,192-token input/output budget and a 4 GiB observed process-group memory bound.
Long histories can reach the native context limit; an error or retained refusal
must be inspected before another attempt. Some machines will respond slowly.

Conscience assesses the supported practice contract. Nurarihyon records actual
dispatch outcomes and reconciles interrupted work. Bezoar retains evidence and
corrections; Origami provides curated handoffs; the toolkit catalogue reports each
operation's requirements. Optional research/training tools are not dependencies
of the installed local reader and are not run by installation.

See `THIRD_PARTY.md` and the included source licences. The distribution includes
the modified GUI source and the runtime source used by this release. Models and
dependencies are obtained from the pinned upstream URLs in `dependencies.lock.json`.
