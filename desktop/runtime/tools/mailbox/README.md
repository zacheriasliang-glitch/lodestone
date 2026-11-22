# File mailbox

See [setup, commands and boundaries](../../docs/mailbox.md).

- `mailbox.py`: immutable file transport through your explicitly configured Git branch.
- `observe_processes.py`: bounded, passive POSIX process-tree observations.
- `worker.py`: optional, separately invoked executor for explicitly admitted jobs.
- `test_*.py`: temporary local Git and fake-CLI tests; no live worker or network.

No personal remote, credentials, historical job manifest, inbox or background
service is included. All three scripts provide `--help`.
