# Mechanical Desktop preview verification

The Desktop workflow has three distinct modes. A mechanical run tests the
current packaged software without generating a model response or claiming that
an older study exercised the new runtime.

| Manual inputs | Mode | Scope |
| --- | --- | --- |
| `mechanical_only=true`, prior evidence false, exact `expected_head` | Mechanical only | Current source, extracted/installed bytes, real GUI controls and installation lifecycle |
| `verify_prior_local_evidence=true`, mechanical only false, exact `expected_head` | Prior evidence | The unchanged strict verifier binds the installed runtime to the old declared study |
| A pull request event | Mechanical only | Automatic zero-model checks, binding GitHub's run SHA to the checked-out commit |
| Both false on a manual run | Inference smoke | Optional legacy local-model smoke test; this is **not** a zero-model run |

The modes are mutually exclusive. The mode selector runs before dependency
installation, downloads or GUI/model work. Either manual verification mode
requires the supplied 40-character commit to equal both GitHub's run SHA and the
checked-out commit. An accepted dispatch is only a request receipt; verify the
returned run's event, exact head commit and completed conclusion separately.
Automatic pull-request checks also require GitHub's run SHA to equal the checkout,
but have no manually supplied `expected_head`. They select the mechanical mode
and its recoverable-log path, with no inference or artifact-storage step.

Use the repository-owned workflow adapter's plan first. For a mechanical run,
select `desktop-preview.yml` and supply these inputs with the intended full
commit:

```json
{
  "mechanical_only": "true",
  "verify_prior_local_evidence": "false",
  "expected_head": "<exact 40-character tested commit>"
}
```

This document does not dispatch anything. The previous study manifest and
verifier remain unchanged. Do not replace their runtime hashes to make a new
candidate appear to have run those historical calls.

The workflow builds a deterministic ZIP, verifies its checksum, extracts without
Git metadata and installs using its bundled shell bootstrap into a new runner
directory. `desktop_mechanical_check.py identity` binds every payload file to
the source, extracted archive and active installed release. The installer still
performs its own integrity checks. The identity check rejects an uncommitted or
untracked source change, so matching a checkout's commit name alone is insufficient.

GUI checks run with the installed GUI interpreter and
`LODESTONE_DESKTOP_TEST_PAYLOAD` pointing at the installed release. That test-only
override selects both the GUI and native runtime, including the bootstrap code;
module path assertions reject accidental checkout imports. The wrapper enables
real wx events and treats skipped coverage as failure. Its isolated authored
provider fixtures are distinct from model inference. It tests ordinary
corrections, explicit preview/save, saved-review restoration, interruption
retention, unavailable evidence, display fit, conversation persistence and the
previous reader. Project serialization is exercised directly; no native file
picker or physical keyboard/mouse interaction is claimed.

The installed application's normal launch and reopen checks run separately.
Existing idempotent installation, synthetic version upgrade, rollback and
uninstallation checks preserve their captured local records. All paths belong
to the new runner preview. They do not replace an existing native installation.

Mechanical and prior-evidence modes retain bounded evidence in the existing
recoverable log envelope, without uploading artifact/cache storage. The
`native/gui-fixtures` directory preserves actual fixture outcomes and reviews.
The ordinary inference mode retains its existing artifact path. A passing
mechanical check establishes no semantic improvement, weight update, model
interruptibility, or memory-usage reduction. Public release notes must identify
any separately measured local results and their original source identities.

Before publishing, bind the final commit and CI run to the exact ZIP digest and
size, retain unsigned Intel-preview limitations, and verify the downloaded
public asset matches those tested bytes.
