# Lodestone Lab: project identity and evidence

**Lodestone Lab is a local AI project notebook that keeps selected source records,
outcomes and corrections, and lets you inspect recorded model requests and
responses.**

**Lodestone Lab** is the public name of the open-source toolkit and desktop
application maintained in
[zacheriasliang-glitch/lodestone](https://github.com/zacheriasliang-glitch/lodestone).
Use that repository's [Releases page](https://github.com/zacheriasliang-glitch/lodestone/releases)
for official downloads and its [issue tracker](https://github.com/zacheriasliang-glitch/lodestone/issues)
for support and reported defects. A matching product name elsewhere does not
identify this project's source or installers.

## Downloads and compatibility

The [v0.4.0 desktop release](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.4.0)
provides separate unsigned x86_64 downloads for Intel macOS, Ubuntu 24.04 and
Windows. The [installation guide](../desktop/README.md) states the exact OS
targets, native test environments and available toolkit operations. Apple
Silicon, ARM Windows, other Linux distributions and mobile platforms are outside
this release's target set.

The public name does not rename the repository, `lodestone` command, download
filenames, application data directories or existing launchers. Launchers retain
“Lodestone Preview” for upgrade continuity; installed metadata identifies 0.4.0.
The separately usable [core v0.3.2](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.2)
has its own release history and requirements.

## Who it is useful for today

- **People working on a long project:** retain decisions, their reasons and source
  references, then prepare a handoff for a later AI session. This helps answer
  what was decided and what the record says supported it.
- **People studying model behavior:** inspect recorded requests, outcomes,
  failures and corrections, and compare later encounters that receive selected
  feedback. This supports investigating whether corrections help; it does not
  establish that they do.

Source retention is explicit. A source reference in a notebook or a checkpoint
does not automatically copy the referenced file; use the relevant capture or
archive operation when its contents must be preserved. A request can be recorded
before dispatch and an interrupted attempt can have a partial or absent response.
Inspect the actual outcome status before treating a request as a completed model
encounter.

## What the software does

- Unified Loom assembles model requests, runs the local reader and retains
  encounters, outcomes and corrections.
- Corrections saved through the feedback workflow can be supplied to later
  encounters according to the configured selection. A correction typed in an
  ordinary chat message is not a guarantee of durable retention or future use.
  Delivery records let a reviewer check which material was supplied; even a
  delivered correction can be misunderstood or ignored by the reader.
- The toolkit provides explicit operations for project memory, handoffs,
  preservation, recovery and source-bound records. Availability depends on the
  platform and operation; see the [function matrix](../desktop/README.md#current-platform-and-function-availability).

The [31-component programme map](capability-map.md) preserves the distinction
between implemented functions, experiments and unfinished research.

## Data flow and cost

First desktop setup downloads pinned runtimes, dependencies and model files over
HTTPS and verifies their hashes. After setup, the default reader performs
inference locally using installed files and a loopback model process; it does
not require a model subscription or paid API account.

Conversations, outcomes and corrections are stored locally. Sharing a handoff,
using a network-capable toolkit operation, or explicitly configuring an external
provider can send data outside the computer. Provider terms and costs then depend
on that configuration. Local inference is not a promise that every possible
operation is offline. For trusted toolkit commands, process ownership, timeouts
and output bounds do not supply a filesystem or network sandbox. In the desktop,
optional external command providers and garden execution use a separate
confinement route and refuse when the required fence is unavailable. This does
not make arbitrary AI-proposed commands safe. Local storage is not a claim of
encryption at rest or isolation from other applications with access to those files.

The software is free to download. The combined desktop distribution is
[GPL-2.0-only](../desktop/LICENSE); the separately usable runtime is
[MIT-licensed](../LICENSE). [Third-party credits](../desktop/THIRD_PARTY.md)
identify the modified BasiliskLLM interface and other dependencies.

## What the published evidence establishes

The [v0.4.0 native run](https://github.com/zacheriasliang-glitch/lodestone/actions/runs/46055542433)
checked installation, GUI behavior, process supervision and lifecycle operations
on macOS 15 Intel, Ubuntu 24.04 and Windows Server 2022. It also completed two
bounded local-reader encounters per target and checked delivery of the retained
correction to the second encounter. macOS 12–14 and Windows 11 are design targets
that were not themselves exercised by that run.

The release includes a
[verification record](https://github.com/zacheriasliang-glitch/lodestone/releases/download/v0.4.0/RELEASE_VERIFICATION.json),
[retained validation history](https://github.com/zacheriasliang-glitch/lodestone/releases/download/v0.4.0/VALIDATION_HISTORY.json)
and [checksums](https://github.com/zacheriasliang-glitch/lodestone/releases/download/v0.4.0/SHA256SUMS.txt).
These bind the downloads and recorded checks to the tested source. Hashes help
check whether bytes match a recorded reference; they do not establish truth,
complete evidence, tamper-proof storage or a publisher signature. A false answer
can be preserved accurately. Later documentation edits on the main branch do not
change the v0.4.0 tag or its installer bytes.

**Improved judgment and semantic learning have not been demonstrated.** Delivering
a correction does not establish that the model understood or used it correctly.
Local practice does not update model weights. A future claim of improvement must
identify the actual cases, prior exposures, comparison conditions, failures and
reviewed outcomes, including when the model should hold its decision and when it
should change it. The released installation and retention checks do not settle
that question.
