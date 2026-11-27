# Portable desktop release verification

The portable preview has separate archives for Intel macOS, Ubuntu 24.04 x86_64
and Windows x86_64. They share product source and retained-record formats, but
use different pinned dependencies, installation paths and process supervision.
See the [installation guide](../../desktop/README.md) for supported OS versions,
unsigned-download handling, platform limits and optional command isolation.

## Native checks

`.github/workflows/desktop-portable.yml` installs each archive on a standard
public GitHub runner for its target. It never uses a private or larger runner,
Actions cache or artifact-storage upload. The workflow runs these checks:

1. Validate exported source, dependency custody, native storage/process behavior
   and scripted practice retention. Scripted responses are identified as fixtures.
2. Build a deterministic source-complete ZIP, check its digest, extract it into
   a new directory without Git metadata, and run its normal installer.
3. Bind the installed files and selected dependency lock to that archive and
   the exact checked-out source commit.
4. Exercise real GUI controls, drafts, saved conversations and earlier readers;
   start and normally close the installed application, then reopen it through
   its installed launcher. The startup check makes no model call.
5. Reinstall identically, switch to a labelled synthetic upgrade, roll back,
   check the restored installation and uninstall. Captured retained files must
   keep their original bytes. This synthetic upgrade is not another release.

All state belongs to a fresh runner directory. Existing installations and
personal records are not used. Linux GUI checks use Xvfb; Windows uses a native
Windows Server 2022 runner. These checks do not establish performance on every
supported machine or assistive-technology compatibility.

Pull requests run in `mechanical_only` mode and cannot request inference. A
manual run must supply `expected_head` equal to both the run SHA and checkout.
To add the bounded local smoke, dispatch the workflow with:

```json
{
  "expected_head": "<exact 40-character release commit>",
  "local_model_smoke": true
}
```

That mode runs two local Qwen3 encounters before the installation lifecycle
checks. The first attempt and its unresolved correction are retained. The second
must include that exact correction in its request and rendered input. The smoke
checks actual worker results, token/digest binding and confirmed process stop.
On Windows it also checks Job ownership of the worker, server and model runner
and the configured committed-memory cap. It makes no API call and updates no
model weights. Correction delivery is not proof that the model understood or
applied the correction, or that semantic decisions improved.

## Published evidence

Each native job prints a bounded, hashed evidence envelope in its final log,
including failures. Recover a completed job's evidence with:

```sh
gh api --allow-escape-sequences \
  repos/zacheriasliang-glitch/lodestone/actions/jobs/JOB_ID/logs > native-job.log
python3 -B tools/desktop_prior_evidence.py recover \
  --log native-job.log --output recovered-evidence
```

The output directory must be new. The recovery tool checks the archive's bounds,
digest and member identities. `portable-ci.json` records the exact commit,
target, mode and final status. The remaining receipts record installed identity,
GUI results, local smoke when requested, and lifecycle preservation. Failed
attempts remain evidence; a later passing run does not overwrite their history.

A published portable release must link completed native checks for all three
targets, expose its verification record, and provide `SHA256SUMS.txt` for its
archives. The source commit, target archive manifest and recovered native build
identity must agree. After publication, download each public asset and verify
its digest again. Dispatch acceptance, a green unrelated workflow, a source
hash or an existing installation alone cannot establish release readiness.

The complete product source and its changes are available in the public GitHub
repository. The combined desktop distribution is GPL-2.0-only; its separately
attributed runtime remains MIT. See [third-party notices](../../desktop/THIRD_PARTY.md).
Pinned external runtimes and model files are downloaded during setup under their
own licenses; private lab research and user records are not bundled.
