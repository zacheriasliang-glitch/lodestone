# Release notes

## 0.3.1 — usable notebooks and reliable worker monitoring

- Leads the README with a working local notebook, AI handoff and archive/restore
  workflow; exercises the public notebook commands on every CI platform.
- Fixes a worker-exit race in RSS monitoring: after a process-disappearance
  response, allow up to 100 ms for the owned child to finish. An unconfirmed exit,
  permission error or malformed sample still fails; nonzero worker exits and
  independent result validation keep their existing handling.
- Corrects publication status in the changelog and capability maps while
  preserving all 31 original component criteria and historical evidence.

## 0.3.0 — approved local toolkit workflows

- Adds portable Nurarihyon and Pando interfaces with explicit user state and
  attribution: durable task queue, exact-plan approval, bounded worker execution,
  independent acceptance, idempotency, STOP and preserved history.
- Exposes implemented profile changes, literal document ingestion, captured
  Bezoar/Yggdrasil reconciliation and historical observation. Unresolved findings
  stay unresolved; source text and correction claims cannot authorize repair.
- Adds user-owned policy preparation, a specific Conscience worker-admission
  binding, synthetic examples, explicit profile-outcome retention and retrieval.
- Validates Conscience operational policy fields before pinning or dispatch.
  Invalid custom field shapes/types now refuse early. Valid neutral v1 policies,
  optional `interpreter.pinned` and descriptive metadata remain compatible; see
  the [policy migration note](docs/conscience.md#policy-compatibility-in-030).
- Includes selected-state export/inert restoration and opt-in macOS login-service
  preparation. Extracting/installing the package never starts a daemon, approves
  a task, clears STOP, contacts a model or configures cloud access.
- Includes the existing experimental supplied-candidate exact storage interface
  with caller-owned pins and literal fallback; no model-generation or benefit claim.
- Preserves existing public commands/workspaces. New state is separate; sealed
  controller policies and pinned services are not automatically migrated.
- Keeps all 31 programme rows and the original negative/frozen findings visible.
  No useful-learning, semantic Origami integration, automatic-repair or
  whole-programme completion claim is added. The rejected passage-budget skill selector and frozen
  experiments 025/026 are not enabled as public runtime features.

Published as [v0.3.0](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.0)
after [PR #5](https://github.com/zacheriasliang-glitch/lodestone/pull/5) merged.
The reviewed build is `c378e44dc5dc7b7234f258e46f14f8235a448f26`; the merge/tag
target is `6871c3f37fc59afbe05ba57b2c815ddef109aa90`, with the same source tree.
[All five reviewed CI jobs passed](https://github.com/zacheriasliang-glitch/lodestone/actions/runs/41334839775).
The exact published ZIP also passed fresh installation on the Mac, including
53 synthetic workflow checks, five completed toolkit tasks, refusal/STOP behavior
and isolated recovery. The release page records archive identities, platform
boundaries and Claude's final scoped acceptance. No private runtime, corpus,
credentials, recovery archive, model artifact or service configuration is distributed.

## 0.2.3 — declared evidence preservation

- Adds independently pinned checkpoint verification and an explicit `preserve`
  comparison for required sources, notes and unresolved items, including duplicates.
  Every candidate source is reopened; equal counts do not conceal substitution.
- Updates the compact-context skill and guides to explain this workflow and its
  limits: selected evidence checks do not guarantee semantic or automatic memory.
- Rejects unverified present restart notes in portable Conscience wake, and returns
  the declared actor alongside the record handle. Absent notes remain normal.
- Rejects nonfinite sampled mesh Jacobians, integrated volume and invalid drift
  limits in Porcelain. Adds synthetic numerical and macro-voxel contract regressions.
  No new scientific result or geometric completeness claim is made.

## 0.2.2 — peer review follow-up

- Adds help for doctor, context and toolkit discovery, plus a Porcelain overview
  that works before its optional environment is installed.
- Adds a standalone corpus-fold guide and further portable Conscience regression
  coverage for history, argument forwarding and command destinations.

## 0.2.1 — download and export documentation

- Uses the extracted versioned folder in onboarding instructions, removing the
  stale 0.1.0 folder name from the 0.2.0 download route.
- Documents UTF-8 text-only selection for the repository source export and
  directs arbitrary binary preservation to the file archive toolkit.
- Runtime code is unchanged from 0.2.0.

## 0.2.0 — portable toolkit expansion

- Includes local memory, explicit checkpoint/resume, installable compact-context skill,
  source re-entry folds, configurable mailbox, repository export/recovery and Python orchestration.
- Exposes receipt preflight, brief/registry checks and source-root-bound Yggdrasil routes.
- Adds an explicit toolkit catalogue with prerequisites and excluded host/private state.
- Public download remains independent of the private laboratory. No services or scientific
  runs start during installation. No native context-window control is claimed.

## 0.1.0 — first public release

- Downloadable source ZIP and tar.gz; run directly with Python 3.10 or later.
- Callable machinery, Origami file exchange, exact archives, source navigation,
  crystal storage and bounded numerical modules with self-contained fixtures.
- Portable Conscience policy and readiness checks for a fresh user.
- Optional porcelain source and example configurations, with separate dependencies.
- File checksum verification and clean-environment onboarding checks.

The source is a reviewed export with fresh history. The private laboratory and
its operational services remain separate. Public version 0.1.0 identifies this
distribution; individual modules retain their own internal schema/version numbers.

Local verification on macOS with Python 3.14: 340 core tests (3 live-laboratory
cases intentionally skipped), 53 corpus-navigation tests, 31 portable ecosystem
tests and 2 download-integrity tests passed. Temporary-user smoke checks covered
doctor, demo, Conscience wake and exact archive recovery. No provider contacted.

Optional porcelain: syntax and import checks passed for nine modules, CLI help
passed for six modules, five input configurations and 16 manifest entries checked.
No numerical scenes were rerun for this release. These are software packaging
checks, not physical calibration or a new scientific result.

The CI workflow checks the core on Linux/Python 3.10 and 3.14, macOS/Python 3.14,
and a narrower newcomer route on Windows/Python 3.14. The Windows job checks
entry points and portable ecosystem/integrity tests, not the POSIX archive/core
suite. Native Windows does not support the archive/corpus-navigation/Origami routes. Check the workflow run linked from the release for executed platform results.
