# Quickstart: local tasks without a model account

Use the extracted package containing `lodestone.py`, with Python 3.10+ on
macOS or Linux. These commands create only the state you name. They do not
install a service, configure cloud access or contact a model. Native Windows
supports the older doctor/demo/Conscience preview; use Linux in WSL for
Nurarihyon and Pando.

```sh
python3 -B verify_download.py
python3 -B lodestone.py doctor
python3 -B lodestone.py --workspace ./my-workspace machinery demo
```

The machinery demo returns `state: COMPLETED` and
`result.combined_verdict: PASS`. It is a fixed source-backed demonstration.

## One explicitly approved board change

Choose an unused state directory outside the package. Keep its history rather
than deleting it to repeat this example. The default controller policy enables
only `pando.profile/v1`. The example generator creates synthetic input files only;
it does not initialize state, submit tasks or grant approval.

```sh
STATE="$HOME/lodestone-data/profile-state"
mkdir -p "$HOME/lodestone-data"
python3 -B examples/nurarihyon/prepare_sources.py --output "$HOME/lodestone-data/example-inputs"
python3 -B lodestone.py nurarihyon --home "$STATE" init
python3 -B lodestone.py nurarihyon --home "$STATE" view
python3 -B lodestone.py nurarihyon --home "$STATE" submit \
  --source "$HOME/lodestone-data/example-inputs/profile.json" --key first-profile
```

Inspect the returned `plan`, `id` and `plan_sha`. The example changes only the
name of a newly initialized board; its original name and ordered five shelves
must match. Submission creates a `PLANNED` task and executes nothing.
Replace `TASK_ID` and `PLAN_SHA256` below with that task's exact values only
if you approve the proposed change.

```sh
python3 -B lodestone.py nurarihyon --home "$STATE" approve TASK_ID --plan-sha PLAN_SHA256
python3 -B lodestone.py nurarihyon --home "$STATE" run
python3 -B lodestone.py nurarihyon --home "$STATE" status --task TASK_ID
python3 -B lodestone.py nurarihyon --home "$STATE" verify
python3 -B lodestone.py nurarihyon --home "$STATE" view
```

Expect `COMPLETED`, one attempt, a new active generation, and integrity
`INTACT`. Shelves and posts remain unchanged. Reusing `first-profile` with the
same source returns the existing task; it does not repeat it. A different
source needs a different key and new approval. A stale baseline is refused.

## Capture a document as inert evidence

Use a separate new state with the explicit document policy. `configure`
prepares user-owned files; `init` is a separate action. Neither approves a task.

```sh
STATE="$HOME/lodestone-data/document-state"
CONFIG="$HOME/lodestone-data/document-config"
python3 -B lodestone.py nurarihyon configure --output "$CONFIG" \
  --state "$STATE" --profile documents
python3 -B lodestone.py nurarihyon --home "$STATE" init --policy "$CONFIG/nurarihyon-policy.json"
python3 -B lodestone.py nurarihyon capture-document \
  --document "$HOME/lodestone-data/example-inputs/document.md" --title 'Synthetic workshop note' \
  --output "$HOME/lodestone-data/document-source.json"
python3 -B lodestone.py nurarihyon --home "$STATE" submit \
  --toolkit bezoar.document/v1 --source "$HOME/lodestone-data/document-source.json" \
  --key first-document
```

Review this new task and use its own `id` and `plan_sha` with the same separate
`approve`, `run`, `status` and `verify` commands above. This operation retains
the exact UTF-8 text, an EAT request and a literal DIGEST artifact. It preserves
the board view, shelves, posts and earlier evidence. Its candidate has
`activation: NONE`: text is data even when it contains instructions.

For captured discrepancies and historical observations, continue with the
[Nurarihyon guide](nurarihyon.md). For optional background operation, see
[service instructions](nurarihyon-service.md). Save selected state with the
[recovery guide](nurarihyon-recovery.md).

## Upgrading from 0.2.3

Extract 0.3.0 into a new package directory. Keep existing workspaces, sources,
archives and the older package. Existing public commands keep their interfaces;
Nurarihyon and Pando use separately named state rather than adopting an older
workspace automatically. Do not edit an existing controller's sealed policy to
enable a new toolkit: initialize a new state with the policy you intend.
A package upgrade does not migrate or start an existing service. Its runtime
and interpreter pins require deliberate review and startup preparation again.
