# File mailbox and optional worker

The mailbox carries exact file bytes through a **dedicated Git branch you own**.
It uses Python 3.10+, Git and POSIX file locks (macOS/Linux). It does not interpret
delivered files or start an AI session. Nothing is installed as a service.

Every invocation requires your own remote, existing branch and local state
directory. Use a private repository when the payloads are private: packets are
stored in Git history. Authentication is your normal Git configuration; do not
put a token in the remote URL or in a payload.

```sh
python3 tools/mailbox/mailbox.py \
  --remote /absolute/path/to/your-mailbox.git \
  --branch mailbox --state /absolute/path/to/your-private-inbox \
  send --to cloud --id note-001 --file /absolute/path/to/note.txt

python3 tools/mailbox/mailbox.py \
  --remote /absolute/path/to/your-mailbox.git \
  --branch mailbox --state /absolute/path/to/your-private-inbox \
  receive --side mac
```

An SSH/HTTPS Git remote also works. The remote must already have the named
branch with an initial commit; the tool does not create a hosting account,
repository or first branch. `mac` and `cloud` are protocol endpoint labels and
can be used on either supported operating system. Each endpoint should use its
own state directory. State is bound to the exact remote and branch and created
with mode 0700. Do not reset it to repeat a consumed job.

`receive` does one pickup. `watch --side mac --interval 30` repeats pickup until
Ctrl-C; do not start a second watcher for the same state. A `ping` packet gets a
same-bytes `pong`, which proves transport, not that an agent read the message.

Delivered files are `STATE/inbox/SIDE/ID/payload` plus `envelope.json`. Payloads
are limited to 1 MiB. IDs are 1–120 ASCII letters, digits, dots, underscores or
hyphens, starting with a letter/digit; ping IDs allow 116 characters. Reusing an
ID with identical bytes is a no-op; different bytes are refused. Envelopes bind
size, SHA-256, endpoint and ID. These are integrity checks, not signatures or
protection against another authorized Git writer rewriting the branch.

## Optional admitted Codex worker

`tools/mailbox/worker.py` is a separate opt-in executor for one or two explicitly
admitted read-only jobs. It is not started by `receive`, `watch`, installation,
Conscience or importing the package. Review the script and create a new job and
admission manifest for your current task before invoking it.

A job payload has exactly `schema: "lodestone-readonly-job/v1"`, `id` and `prompt`.
Its manifest has exactly `schema: "lodestone-worker-manifest/v1"` and `jobs`, an
array of `{ "id": "your-job-id", "sha256": "SHA256_OF_EXACT_PAYLOAD_BYTES" }`.
The admission digest is of the serialized payload, not the envelope or a
reformatted JSON object. The worker requires explicit `--repo`, `--manifest`,
`--remote`, `--branch`, and `--state`; state must be outside the inspected Git
checkout. `--help` explains the optional flags.

The existing adapter checks the installed Codex CLI's required flags and an
existing ChatGPT login, rejects API-key configuration, bounds time/output, and
records claims and results. It refuses to replay an unfinished claim. Configured
MCP tools require explicit scoping, and CLI features can cause a fail-closed
preflight. Compatibility with a future Codex version is not established by the
fake-CLI tests. No credential, admitted job or live model test is shipped.

`worker` status `ok` means a successful exit, a nonempty answer and matching Git
observations. It is not proof of answer correctness, complete filesystem
isolation or successful reads. The CLI sandbox and account permissions remain
separate controls.

## Process observation and tests

`observe_processes.py --help` describes the bounded POSIX sampler. It records
process identifiers and executable basenames for one selected tree; labels do
not establish executable identity. It starts no worker and does not signal
observed processes.

```sh
python3 -B -m unittest discover -s tools/mailbox -p 'test_*.py' -q
```

The tests use temporary local Git repositories and a fake CLI. They make no
network or model calls and do not use a host's existing mailbox.
