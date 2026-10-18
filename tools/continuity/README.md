# Portable continuity

`checkpoint.py` saves a small task record with hashes of explicitly selected
files. `verify` checks those files before a new session relies on the saved
record; an optional independently saved raw-file pin binds that check to the
original checkpoint. `preserve` requires raw pins for both checkpoints and
checks retention of source identities, exact notes and unresolved strings,
then reopens every candidate source. It compares one task within the same
source revision; it does not establish semantic equivalence.

Run `python3 -B lodestone.py continuity --help` from the package root for the
command routes. The workflow does not control an agent's context window,
create a backup, read chat history, run commands from notes, or start services.

See [the continuity guide](../../docs/continuity.md) for commands, limits and
installation of the optional compact-context skill.
