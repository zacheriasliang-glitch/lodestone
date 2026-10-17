# Portable continuity

`checkpoint.py` saves a small task record with hashes of explicitly selected
files. `verify` checks those files before a new session relies on the saved
record. It does not control an agent's context window, create a backup, read
chat history, run commands from notes, or start background services.

See [the continuity guide](../../docs/continuity.md) for commands, limits and
installation of the optional compact-context skill.
