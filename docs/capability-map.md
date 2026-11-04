# Public capability map — published 0.3.0

This map separates code availability, public verification and historical laboratory
evidence. It does not declare whole-programme completion or inherit private state.
Published version: **[0.3.0](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.0)**,
following **0.2.3**. Later changes are recorded separately in the changelog.

Access-restricted historical laboratory source is pinned to [`9b78193aecec`](https://github.com/zacheriasliang-glitch/lodestone-lab/tree/9b78193aecec663e58116a7a4b576a8356a1cf0b).
The [restricted historical scope record](https://github.com/zacheriasliang-glitch/lodestone-lab/blob/9b78193aecec663e58116a7a4b576a8356a1cf0b/docs/publication-20261101/COVERAGE.json)
preserves the original 31 components. Public readers do not need access to that
repository: all 31 acceptance meanings/statuses are retained in the JSON below.
The underlying historical scope record SHA-256 is
`af579d036aa9a44acb08032b30238a5decd52326600533e5f36f951a409eca73`.

The [machine-readable map](capability-map.json) contains **every original row’s
acceptance meaning**, base status, source paths, interfaces, dependencies, gaps
and separate public-verification field. Only private location/packet details
are generalized; no criterion is removed to make the release appear complete.

## Evidence status

- **Existing public**: already available in 0.2.3 within its documented limits.
- **New adaptation**: implementation adapted from the laboratory for this public release; its verification is recorded separately from the historical laboratory classification.
- **Historical lab classification**: preserved, never promoted by packaging or publication.
- **`NOT_RUN_FOR_0.3.0`**: no new 0.3.0 check is attached to that particular row. Other local results are recorded separately below.
- **`NOT_EXPORTED_NO_EXECUTION`**: visible laboratory provenance, unavailable as a supported public route.

Implementation links below open the actual adapted source in this public package.
Laboratory revision/path references remain access-restricted historical provenance;
no private access is required to run or inspect the public implementation. The JSON
separates public files from historical files that were not exported.

The published archive was built from `c378e44dc5dc7b7234f258e46f14f8235a448f26`;
[PR #5](https://github.com/zacheriasliang-glitch/lodestone/pull/5) merged at
`6871c3f37fc59afbe05ba57b2c815ddef109aa90` with the same source tree.
[All five reviewed CI jobs passed](https://github.com/zacheriasliang-glitch/lodestone/actions/runs/41334839775).
The exact released ZIP passed fresh Mac installation, 53 synthetic workflow
checks, five completed toolkit tasks, STOP-preserving recovery and independent
Claude review. [Release provenance and platform boundaries](https://github.com/zacheriasliang-glitch/lodestone/releases/tag/v0.3.0)
identify the actual artifacts. These release results do not promote the original
31 research criteria or establish every later branch's CI status.

## Implemented additions and public adaptation

| ID | Capability | Programme rows | Interface and source | Public evidence |
|---|---|---|---|---|
| A01 | Durable approved task execution | H01, H02, H11 | nurarihyon submit/approve/run/serve/status/verify ([public implementation](../src/lodestone_nurarihyon/controller.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A02 | Pando board and literal profile operation | H11, H12 | pando --home BOARD ...; toolkit pando.profile/v1 ([public implementation](../src/lodestone_pando/store.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A03 | Inert document preparation and ingestion | H01, H11, H12 | nurarihyon capture-document; submit --toolkit bezoar.document/v1 ([public implementation](../src/lodestone_nurarihyon/documents.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A04 | Bezoar proposals and bounded observation | H12, H14 | bezoar ingest/eat-request/eat/mutate/hold/show/export-profile; watcher observe ([public implementation](../src/lodestone_machinery/bezoar.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A05 | Complete inline reconciliation | H01, H12, H15 | submit --toolkit bezoar.reconciliation/v1; Plumber.reconcile_captured ([public implementation](../src/lodestone_machinery/bezoar_reconciliation.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A06 | Record-backed complete capture | H01, H12, H15 | bezoar_capture.prepare/publish/read; submit --toolkit bezoar.reconciliation-capture/v1 ([public implementation](../src/lodestone_machinery/bezoar_capture.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A07 | Historical observation without invented predecessors | H01, H12, H15 | bezoar_capture.prepare_observation; submit --toolkit bezoar.reconciliation-observation/v1 ([public implementation](../src/lodestone_machinery/bezoar_observation.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A08 | Conscience-bound worker admission | H01, H22 | User-owned sealed policy binding; assess_worker before worker spawn ([public implementation](../src/lodestone_nurarihyon/conscience_binding.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A09 | Verified terminal outcome retention | H01, H23 | nurarihyon remember TASK --memory STORE ([public implementation](../src/lodestone_nurarihyon/outcome_memory.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A10 | Source-backed context and inert offline preparation | H01, H23 | nurarihyon context; submit --memory; prepare --package --descriptor ([public implementation](../src/lodestone_nurarihyon/context.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A11 | Pinned reconciliation-history reuse | H01, H12, H15, H23 | submit --reuse-reconciliation for inline reconciliation ([public implementation](../src/lodestone_machinery/bezoar_routing.py)) | LOCAL_COMPONENT_CHECKS_NO_COST_BENEFIT_CLAIM |
| A12 | Selected-state consistent export and stopped restore | H01, H16, H27 | nurarihyon export --output; restore --package --descriptor --output ([public implementation](../src/lodestone_nurarihyon/portable.py)) | LOCAL_PUBLIC_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A13 | Bounded service execution and opt-in startup configuration | H01, H26 | nurarihyon daemon; startup.render_agent/validate_agent; explicit service setup only ([public implementation](../src/lodestone_nurarihyon/service.py)) | LOCAL_SERVICE_CONTRACT_TESTS_NO_LOGIN_TEST |
| A14 | Explicit candidate-only latent storage | H17, H19 | origami crystal latent put; yggdrasil crystal latent get; explicit store_source/restore_source and supplied-candidate catalog API ([public implementation](../src/lodestone_machinery/latent/cli.py)) | LOCAL_SYNTHETIC_EXACT_STORAGE_PASS_SEE_PR_FOR_CURRENT_HEAD |
| A15 | Reversible lifting and physical artifact encoding | H19, H20 | Imported library dependencies: encode/decode, compact-layout/checkpoint helpers and raw/zlib artifact encoding; no new top-level training/proposer command ([public implementation](../src/lodestone_machinery/latent/lifting.py)) | SYNTHETIC_STORAGE_DEPENDENCIES_ONLY_NOT_NUMERICAL_FIT |
| A16 | Lab-only custody automation and source-preservation history | H21, H27, H28 | Existing public repository export remains available; lab workflow helpers are provenance ([public implementation](../tools/repository/export.py)) | NOT_RUN_FOR_0.3.0 |
| A17 | Experimental skill library and selector | H01, H23 | Research source only; no supported task toolkit or adoption ([restricted historical source](https://github.com/zacheriasliang-glitch/lodestone-lab/blob/9b78193aecec663e58116a7a4b576a8356a1cf0b/src/lodestone_nurarihyon/skill_library.py); not exported) | NOT_EXPORTED_NO_EXECUTION |

The three implementation boundaries that determine how these routes can be used:

- **Task and data boundary:** A01–A07 consume explicit sources, exact task approval and user-owned policy. Documents and retained candidates remain data. Pando/Bezoar proposals can retain evidence or a versioned profile; they cannot register code, approve themselves or activate a model. Capture and historical observation are separate contracts: observation keeps absent predecessors unresolved.
- **Custody and reuse boundary:** A08 assesses a fixed worker argv and declared effects using pinned user policy/code. It can hold that route; controller approval, STOP, limits and publication remain separate. A09–A11 explicitly retain/reopen terminal or same-corpus evidence. They do not prove useful learning, automatically dispatch work, erase findings or make semantic judgments true.
- **Recovery and service boundary:** A12 snapshots selected state consistently and restores to a new stopped directory. A13 requires supported process/resource observation and explicit service setup; graphical-login registration is macOS-specific. Installation must not load a service, approve a task, clear STOP, call a model or configure cloud access. A14 supplies explicit experimental exact storage; A15 contains its library dependencies without a new training/proposer claim. A16–A17 remain lab custody/research provenance.

## Dependency and verification contracts

| Group | Required dependencies | Verification scope |
|---|---|---|
| A01 | Python/SQLite/POSIX resource/process support; local machinery/Pando | Synthetic successful exact-plan task, duplicate key, wrong plan/source pins, worker rejection, STOP, interrupted state and preserved history. |
| A02 | SQLite and immutable organ records; explicit chosen workspace | Synthetic fresh board and profile task; unchanged unrelated shelves/posts; exact lineage and INTACT outcome. |
| A03 | Explicit policy opt-in; trusted local code; no model/provider | Synthetic document with command-like text; exact-byte quotes; bad UTF-8/bounds refusal; daemon-owned task publication. |
| A04 | RecordStore, source anchors, structured proposal contracts | Synthetic tampered-quote rejection, versioned mutation, observed drift and retained hold. |
| A05 | Bounded declared corpus; explicit Conscience binding; Pando/RecordStore/Yggdrasil | Synthetic complete corpus, changed pin, incomplete closure, disagreement retention and independent stage validation. |
| A06 | User-owned capture root/spec; fresh destination; exact manifest and record pins | Synthetic complete physical capture, missing member, record tamper, bounds and matched recomputation. |
| A07 | Explicit observation contract, source/correction evidence declarations and Conscience binding | Synthetic missing historical reference remains unresolved; declared correction not applied; byte tamper refused; observation result independently checked. |
| A08 | Explicit local actor/policy/workspace; no account permission or universal sandbox | Synthetic allow/refuse/hold, changed code/policy, wrong argv/effect scope and no publication on hold. |
| A09 | Dedicated caller-owned memory directory; controller lock; trusted local-memory module | Synthetic idempotent retention, tamper/supersession/bounds cases and unchanged source state. |
| A10 | Exact source/export descriptors; bounded verified history; optional explicit memory catalogue | Fresh-process retrieval; unrelated changed-condition context not asserted applicable; prepare preserves STOP and matches subsequent profile plan. |
| A11 | Existing verified reconciliation record and matching corpus/source/implementation identities | Same-corpus comparison; stale/mismatched source refused; findings preserved; no automatic dispatch. No cost-saving claim without measured evidence. |
| A12 | POSIX paths, SQLite backup, archive code; relocated dependency/binding configuration remains explicit | Fresh-archive selected-state export/restore; source/manifest tamper; required files; STOP and no execution; identity of recovered task/generation history. |
| A13 | Supported process identity/RSS sampler; macOS launchd for graphical-login registration | Fresh isolated foreground service evidence and startup-render checks. This does not constitute a real reboot/login test on another machine. |
| A14 | Caller-supplied pinned non-private candidate plan/model provenance/table bytes; existing CrystalStore; standard library | 55 synthetic tests, 9 existing CLI regressions and guarded fresh-directory exact-recovery example passed; This local record does not claim every archive/platform check; see PR5 for current evidence. |
| A15 | Exact code dependencies of supplied-candidate/catalog storage; explicit arithmetic/model contracts for optional library calls | Codec/wire compatibility, literal catalog paths and supplied candidates tested. Optional lifting fit/neural functions and physical packed-transport research are not rerun or adopted. |
| A16 | Git and user-owned remote/custody policy for hosted paths; no credentials shipped | Public selected-source export checks; no private artifact upload or replay of historical experiments. |
| A17 | Closed experimental work and separately retained evidence | No closed acceptance rerun, no rejected selector activation and no useful-learning claim. |

Core routes use trusted local Python, SQLite, existing machinery and caller-owned
workspaces. Advanced Yggdrasil needs the caller’s canon/receipt contracts. Provider
and cloud routes need the caller’s explicit configuration; none are implicit.
Optional numerical dependencies retain their own licenses. No private policy,
Mac path, existing board, credential, historical corpus or research model is a
required public example.

## Preserved development evidence before publication

On macOS 12.7.6 (Intel), Python 3.12.7:

- Nurarihyon: 187 checks passed. Pando: 17 passed. Conscience: 78 passed. Public configuration/platform boundaries: 4 passed.
- Bezoar: 46 discovered; 45 passed across the original run and a focused two-case correction, with one frozen Genepool-dependent case excluded. The original failures remain retained.
- Five real synthetic toolkit routes, explicit profile-outcome retention, STOP and isolated recovery: 50 checks passed in 203.094 seconds, with no model calls or service installation.
- Portable recovery: 20 checks passed, including the final required-runtime-file closure recheck.
- Process-memory observer: 14 checks passed on macOS; the actual Linux-host probe was skipped and remains for Linux CI. Local-memory compatibility: 9 focused checks passed after the bounded-retrieval correction.
- Supplied-candidate storage: 55 focused tests and 9 existing crystal CLI regressions passed; the guarded synthetic example recovered the exact 28 bytes. Selected payload 187 bits versus 227 bits for the first supplied row still produces a 98-byte packet, larger than the raw input. The two independent stores plus receipts occupied 78,160 and78,017 bytes on this machine. This is exact storage verification, not demonstrated useful compression or learning.

The last group used 18.302371 child CPU seconds and 21.396733 wall seconds including
child startup. Its code pins were unchanged during execution. Tests used invented
bytes and guarded/stubbed model operations; no model training, frozen study or
new scientific benchmark ran. References and limits are in the JSON evidence
records E01–E11.

Fresh installation from the first built archive passed **53 workflow checks** on
the Mac: all five synthetic toolkit operations, STOP, isolated recovery and the
public launcher. The tested archive was built from `74ea6538271d`; its runtime
files matched candidate `e262e551f2f9`. This is a local archive result, not proof
of every platform or a real reboot/login. This first-archive record remains
historical evidence; the final published-build verification is recorded above.

## Whole-programme coverage: all 31 rows

The exact narrow acceptance meanings remain in [capability-map.json](capability-map.json).
`NARROW_PASS` is limited to those criteria. `ASSERTIONS_PASSED_GUARD_FAILED` is
unaccepted despite passing inner assertions. `CLOSED_GUARD` remains closed.
`STATIC_PINS` establishes bytes/reading order, not execution or training.
All release verification is separate from these historical classifications.

| Row | Component | Historical lab status | Public classification | Remaining limitation |
|---|---|---|---|---|
| H01 | Nurarihyon | PARTIAL | NEW_ADAPTATION | Whole-organ coordination and useful retained learning remain unfinished; no source document can approve or register code. |
| H02 | Machinery Engine / Builder | NARROW_PASS | EXISTING_PUBLIC | Durable job execution is not autonomous general reasoning. |
| H03 | Source anchor and expansion | NARROW_PASS | EXISTING_PUBLIC | Handles authenticate retained source bytes, not the truth of source claims. |
| H04 | Literal selection / fold | NARROW_PASS | EXISTING_PUBLIC | Fixed ranking is not a learned selector; failed adaptive selection is not promoted. |
| H05 | Brief builder / mechanical Auditor | NARROW_PASS | EXISTING_PUBLIC | Mechanical source agreement does not establish semantic truth. |
| H06 | Ruler / measurement | NARROW_PASS | EXISTING_PUBLIC | Measurement criteria are caller-declared, not inferred scientific validation. |
| H07 | Pressure and residual retention | NARROW_PASS | EXISTING_PUBLIC | No production fault injection or automatic correction. |
| H08 | RecordStore / NodeStore | NARROW_PASS | EXISTING_PUBLIC | Freshness invalidation requires the explicit caller operation; put alone does not propagate it. |
| H09 | Paired pressure transport | NARROW_PASS | EXISTING_PUBLIC | No paired-model result is inherited and no provider is called automatically. |
| H10 | Origami semantic runtime | NARROW_PASS | EXISTING_PUBLIC | The historical criterion is file exchange only. Semantic folding in the daemon and mechanistic model interpretability remain unproved. |
| H11 | Pando | PARTIAL | NEW_ADAPTATION | Historical HTTP UI remains UNCHECKED; installed lab board, posts and identities are excluded. |
| H12 | Bezoar / Plumber / INGEST-EAT-MUTATE | NARROW_PASS | NEW_ADAPTATION | DIGEST validates a supplied structural proposal; no automatic semantic understanding or candidate activation is implied. |
| H13 | Legacy Bezoar SEED/NACRE registry | NARROW_PASS | EXISTING_PUBLIC | No lab registry or its historical resolutions are copied. |
| H14 | Watcher | NARROW_PASS | NEW_ADAPTATION | Observation does not repair or approve. Relocated historical source paths may need explicit remapping. |
| H15 | Yggdrasil lineage / macro-voxel canon | NARROW_PASS | EXISTING_PUBLIC_WITH_NEW_BRIDGE | Reconciliation connects structural findings to Bezoar. It does not supply semantic Origami integration or erase disagreement. |
| H16 | Origami/Yggdrasil file archives | CLOSED_GUARD | EXISTING_PUBLIC_WITH_NEW_RECOVERY | Generic file archives permit validated relative symlink members; Nurarihyon selected-state restore forbids all code/state symlinks. A new narrow pass cannot erase the closed historical guard. |
| H17 | CrystalStore and claim/mutation extensions | NARROW_PASS | EXISTING_PUBLIC_WITH_EXPLICIT_EXPERIMENTAL_STORAGE | Exact storage and claim bookkeeping do not demonstrate useful learned compression. |
| H18 | Crystal codec policy and routing | FROZEN | FROZEN_RESEARCH | Tests that import closed experiment machinery remain outside this release evaluation; raw-storage/no-full-proposer decision is retained. |
| H19 | Latent supplied-candidate storage | NARROW_PASS | EXPLICIT_EXPERIMENTAL_PUBLIC | Candidate rows, model provenance bytes and table are supplied and pinned by the caller. No automatic proposer/model invocation, useful learned-transfer claim, frozen-study reopening or Nurarihyon toolkit registration. |
| H20 | Tiny transformer inference and training | NARROW_PASS | EXISTING_EXPERIMENTAL_PUBLIC | Existing transformer numerical APIs remain available; the imported latent dependency includes explicit optional numerical functions, but supplied-candidate storage does not invoke them. No new neural training/inference or useful-learning result is claimed. |
| H21 | Genepool | STATIC_PINS | LAB_PROVENANCE_ONLY | Genepool remnants do not establish an integrated evolution loop. |
| H22 | Conscience | ASSERTIONS_PASSED_GUARD_FAILED | EXISTING_PUBLIC_WITH_NEW_BINDING | Advisory assessment can hold this fixed worker route. It is not authority or a universal sandbox; controller queue/publication effects are outside the mapped worker assessment. |
| H23 | Local memory | PARTIAL | EXISTING_PUBLIC_WITH_NEW_BRIDGE | Context/remember bridge is bounded, explicit and profile-oriented. Retention/retrieval is not learned utility or proof of avoided work. |
| H24 | Corpus fold and frozen Origami fold | ASSERTIONS_PASSED_GUARD_FAILED | EXISTING_PUBLIC | No private frozen fold or corpus is copied; semantic abstraction guarantees are not inferred from a map. |
| H25 | Continuity checkpoints and receipt guard | ASSERTIONS_PASSED_GUARD_FAILED | EXISTING_PUBLIC | Does not automatically compact model context or guarantee all future-relevant information survives. |
| H26 | Mailbox transport and existing login readiness | NARROW_PASS | EXISTING_PUBLIC | No private mailbox, credential, receive/watch daemon or inherited login proof is shipped. |
| H27 | Samsara / Saṃsāra and repository custody | NARROW_PASS | EXISTING_PUBLIC_WITH_LAB_ONLY_AUTOMATION | Git source publication is not a complete backup. Private workflow/account custody helpers and recovery archives are not public user state. |
| H28 | Bindle | STATIC_PINS | LAB_PROVENANCE_ONLY | Bindle is the correct component name. The private packet and its contents are not bundled; no Bindu component is invented. |
| H29 | Kernel20 / Kernel21 / legacy orchestrator | NARROW_PASS | EXISTING_PUBLIC | Legacy Node prototype and historical workstreams are excluded; planning does not automatically execute a task. |
| H30 | Blackstone runtime | NARROW_PASS | LAB_PROVENANCE_ONLY | The Blackstone runtime is not present in the v0.2.3 public tree; this row is not silently counted as a public feature. |
| H31 | Porcelain | OPTIONAL_ENVIRONMENT | OPTIONAL_PUBLIC | Historical absent checked environment is not a claim of absence on every machine. Illustrative coefficients are not calibrated science. |

## Connected operation and learning boundary

The concrete integration is declared source → Nurarihyon plan and exact approval
→ staged worker → Pando/Bezoar operation → independent controller acceptance
→ retained generation and findings. Reconciliation invokes Yggdrasil lineage
checks over captured bytes and retains Bezoar discrepancies. Historical observation
records missing predecessors instead of manufacturing them.

The explicit outcome-memory bridge can retain terminal evidence and reopen it
for a later compatible profile plan. Same-corpus reconciliation reuse can retain
an inert routing proposal after independently rechecking prior evidence. Neither
interface establishes lower human effort or total cost, general semantic folding,
adaptive skill selection or model-weight learning. Those remain programme goals
with their original acceptance requirements.

Semantic interpretation concerns the meaning of source content. Mechanistic
interpretability requires records tied to actual model activations, circuits or
interventions; these source/lineage tools do not supply that evidence.

## Negative findings and exclusions

- **operating-cycle-01:** All six paired engineering outcomes tied. Context integration did not demonstrate improved task outcomes; retaining/retrieving context alone is not useful learning.
- **skill-selection comparison:** Fixed lexical rules performed better for the tested task family. FAILED_CPU_LIMIT is preserved as a distinct supervisor/execution finding; it does not explain away the selector result.
- **folded computation:** NO_ADOPTION is retained. Startup-only benefits do not establish a total-cost advantage over strong warm/factor-aware classical baselines.
- **frozen experiments 025/026:** Keep raw storage for the tested group and no full-proposer deployment. Development data exposed during work are not held-out evidence.

Merged laboratory PRs #140–#142 distinguish the installed source snapshot,
development history and frozen studies. Their preservation is provenance; it is
not public packaging validation or automatic production readiness. No private
board, conversation, policy, research panel, model checkpoint, raw log, recovery
archive, mailbox connection, credential or VM image belongs in this public release.
Git source publication is not a complete off-device backup.

**Whole-programme status: OPEN.** A usable local release can add verified interfaces
without asserting that all 31 components are integrated, fully trained or shown to
retain useful learning.
