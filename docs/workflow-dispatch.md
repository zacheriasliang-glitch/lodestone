# Explicit GitHub workflow dispatch

`tools/github_actions/dispatch.py` supplies a repository-owned action function and
CLI for starting one reviewed GitHub Actions workflow. A local/custom connector
can call `create_plan`, `execute_plan` and `inspect_intent`. This does **not** add
a tool to OpenAI's hosted GitHub connector. The connected tool inventory inspected
for this implementation exposed workflow reads and rerun operations, but no new
`workflow_dispatch` action. Rerunning an old job is a different operation.

This optional tool needs Python 3.10+ and an already authenticated `gh` executable
with access to the selected repository. It invokes `gh api` with argument arrays
and JSON on stdin; it never extracts or prints a token, starts a model, or changes
credentials. No connector server, background service or workflow is installed.

## Plan, execute once, then inspect

Planning uses GET requests only. Running the script without a subcommand prints
help and does nothing else. Use a new output file and an explicit local state
directory. Plans/receipts contain workflow inputs, so do not use inputs for secrets
or publish the local state directory.

```sh
python3 -B tools/github_actions/dispatch.py plan \
  --repository OWNER/REPOSITORY --workflow verify.yml --ref BRANCH \
  --expected-sha FULL_40_CHARACTER_COMMIT_SHA \
  --state-dir ./workflow-state --output ./workflow-plan.json
```

The result includes the SHA-256 of the exact plan bytes. Review the named workflow,
its source commit and inputs. Optional `--inputs FILE.json` accepts a JSON object
of scalar string, boolean or numeric inputs without shell or `@file` expansion.
Execution is a separate explicit action:

```sh
python3 -B tools/github_actions/dispatch.py execute \
  --plan ./workflow-plan.json --plan-sha256 PLAN_SHA256_FROM_PLANNING
python3 -B tools/github_actions/dispatch.py status INTENT_DIRECTORY_FROM_EXECUTION
```

The adapter currently permits public github.com repositories only. It resolves an
unambiguous branch/tag to the expected commit, including annotated tags, and pins
the workflow ID, path and selected file blob. It checks that the file also exists
on the default branch. GitHub validates the workflow's dispatch trigger and input
schema; the adapter does not implement a YAML parser or prove that its jobs are
safe or free. Read the workflow before authorizing execution. Public visibility
alone does not establish runner, storage or external-service costs.

The existing Desktop workflow supports `verify_prior_local_evidence: true` and
`expected_head: EXACT_SHA` for its declared zero-inference verification path. The
adapter does not silently select those inputs or change any workflow. No workflow
was dispatched while implementing or testing this adapter.

## What the receipt establishes

The selected ref and workflow are rechecked immediately before dispatch. A durable,
exclusive intent is recorded **before** one POST, with file synchronization and
POSIX directory synchronization. The same plan cannot execute
again against that state, even after an interrupted process or lost response.
Keep this directory intact. This is local duplicate suppression, not a server-side
idempotency key: creating another plan or deleting/moving state can submit another
run. The tool never automatically retries a POST, cancels a run or chooses the
latest similar run.

| Receipt state | Meaning |
| --- | --- |
| `ACCEPTED_WITH_RUN_ID` | GitHub returned a run ID and matching repository URLs; completion is not implied. |
| `ACCEPTED_UNBOUND` | GitHub accepted the request but returned no usable exact run identity, including legacy HTTP 204. |
| `POST_AMBIGUOUS` / `RESERVED_OUTCOME_UNKNOWN` | Dispatch might have happened; preserve the intent and investigate without resubmitting it. |
| `REJECTED` | GitHub returned a definitive client-error response; the attempted intent is still retained. |
| `RUN_PENDING` / `RUN_LOOKUP_PENDING` | The known run is unfinished or could not yet be read. |
| `RUN_BINDING_MISMATCH` | Run ID, repository, workflow, event, commit or original attempt differs; no completion claim is accepted. |
| `RUN_COMPLETED` | The bound original attempt ended; inspect `conclusion` and `successful` separately. |

After acceptance, a GET verifies the exact returned run ID, repository ID, workflow
ID/path, `workflow_dispatch` event, expected head commit and attempt 1. `status` repeats
only that GET and retains another observation. A later rerun (attempt 2+) does not
stand in for this original attempt. A CLI exit code of zero means a structured
receipt was produced, **not** that the workflow passed; inspect its state and
`successful` field.

A mutable ref can move between the preflight GET and POST. Post-dispatch head
verification detects this but cannot undo work already started. For source-critical
jobs, the workflow itself must check a supplied expected commit before substantive
steps, or use an appropriately protected tag. The tool does not claim atomic
compare-and-dispatch or create/protect tags.

## API contract and verification

This adapter pins GitHub REST API version `2026-03-10`. Its current dispatch
contract returns HTTP 200 with `workflow_run_id`, `run_url` and `html_url`.
The earlier API supports `return_run_details: true`; without it the older behavior
is HTTP 204. Both response classes are handled without guessing a run.
[Dispatch REST reference](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[earlier-version reference](https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28#create-a-workflow-dispatch-event),
[GitHub's run-ID announcement](https://github.blog/changelog/2026-02-19-workflow-dispatch-api-now-returns-run-ids/).

GitHub requires the workflow to support `workflow_dispatch` and be present on the
default branch. Fine-grained authentication needs repository Actions write
permission for dispatch; reads use the corresponding read access. The `ref` is a
branch or tag name, with up to 25 configured inputs.
[Manual workflow guidance](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[exact workflow-run read](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run).

Run the focused offline checks:

```sh
python3 -B -m unittest discover -s tests/github_actions -v
```

These use fake transports and subprocess fixtures, covering drift, duplicate
dispatch, crashes, legacy responses, ambiguous acceptance and incorrect run
identity. They do not establish a successful live dispatch, CI completion, paid
service entitlement or any change to the hosted connector.
