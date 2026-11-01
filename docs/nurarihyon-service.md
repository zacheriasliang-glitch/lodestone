# Nurarihyon service: explicit opt-in

Installing or extracting Lodestone never starts a daemon, approves a task, clears
STOP or installs a login job. Manual execution remains available on macOS/Linux:

```sh
python3 -B lodestone.py nurarihyon --home /absolute/user-state run
python3 -B lodestone.py nurarihyon --home /absolute/user-state serve --seconds 60
```

`run` selects at most one approved task. `serve` stays in the foreground for its
bounded duration and accepts no work unless it has enough time for the declared
operation budget. Neither detaches, registers startup or retries a failed task.
Use only one owner for a state; locks prevent competing executors.

## Prepare macOS graphical-login startup

Keep the extracted package at a stable location. Choose an initialized dedicated
state and a separate existing log directory. Review state integrity, STOP and
pending approvals first. This command prepares files only:

```sh
python3 -B lodestone.py nurarihyon startup \
  --home /absolute/user-state \
  --output /absolute/new-startup-preparation \
  --log-file /absolute/existing-log-directory/nurarihyon.jsonl
```

Read the generated `com.lodestone.nurarihyon.plist` and `STARTUP.json` manifest. They bind the package source and
existing Python/launcher identity to explicit paths. The plist has `RunAtLoad`
for the current user's graphical (`Aqua`) session and `KeepAlive: false`.
A preparation result is not an installed or tested login service. Do not replace
an existing registration with the same label, especially one belonging to another
installation. This package does not manage system-level services or change security
settings. Automatic login startup on Linux is not packaged; use explicit foreground
execution or your own separately reviewed service configuration.

For an explicit long-running foreground daemon on either supported platform,
`python3 -B lodestone.py nurarihyon identity` reads the current `code_sha256` and
`environment_sha256` without creating state. Supply those exact values to:

```sh
python3 -B lodestone.py nurarihyon --home /absolute/user-state daemon \
  --log-file /absolute/existing-log-directory/nurarihyon.jsonl \
  --expected-code-sha CODE_SHA256 --expected-environment-sha ENVIRONMENT_SHA256
```

This command stays attached to its terminal; it does not register a service.

## Install and load only when you choose

These are manual activation instructions, not installation defaults. Ensure no
other Nurarihyon executor owns the selected state and no existing
`com.lodestone.nurarihyon` registration would be displaced. Keep original service
files and state unchanged if a conflict exists.

1. Inspect the prepared `com.lodestone.nurarihyon.plist` and source/interpreter pins.
2. Place that file at `~/Library/LaunchAgents/com.lodestone.nurarihyon.plist` only
   if that destination and service label are unused. Keep the preparation manifest.
3. To explicitly load and start that registration in your current graphical session:

   ```sh
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.lodestone.nurarihyon.plist"
   launchctl print "gui/$(id -u)/com.lodestone.nurarihyon"
   ```

Loading may execute already-approved queued tasks. Review the queue before this
step. A present STOP causes the service to stop rather than release it. There is
no automatic retry or restart after a failure. KeepAlive is deliberately disabled.

The daemon logs fixed events with UTC timestamp/PID and task ID/outcome, including
`STARTED` and `TASK_OBSERVED`. A running process or STARTED event alone does not
prove a task passed; inspect its controller result and integrity. The log rotates
at 256 KiB with one retained predecessor and contains no source text by design.
Registration with RunAtLoad establishes intended login configuration; proving
that a particular login caused startup requires separate observed login evidence.
This release's configuration check is not a reboot or future-reliability guarantee.

## Stop, inspect and upgrade

```sh
python3 -B lodestone.py nurarihyon --home /absolute/user-state stop
python3 -B lodestone.py nurarihyon --home /absolute/user-state status
```

Wait for the active operation to reach its recorded stopped/interrupted boundary.
To remove the loaded registration after it is quiescent, explicitly run
`launchctl bootout "gui/$(id -u)/com.lodestone.nurarihyon"`. This does not delete
state or release STOP. Preserve the plist and manifest if you intend to resume.

A changed runtime/interpreter identity fails closed. Do not patch a plist's pins
to conceal a mismatch. Keep the older package and state, review the new version,
and prepare a new registration deliberately. An existing controller policy is
sealed; changing enabled toolkits requires a separate new configuration/state.
No unattended service migration or automatic rollback is provided.
