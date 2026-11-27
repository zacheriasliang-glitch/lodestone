# Loom voxels: could, would, should and the named knot

Source: `src/lodestone_machinery/loom_voxels.py`, `src/lodestone_machinery/loom_would_probe.py`,
one hook in `loom_adapter.render_prompt`. Tests: `tests/machinery/test_loom_voxels.py`.

## Why

The v4 assessment (17 Nov 2026) asked Loom to obey a format that the system prompt
classified as data, graded a stamped final answer, and compared greedy texts. The
base and the six-update adapter printed token-identical text, so nothing about a
changed balance could show. In the changed-purpose case Loom repeated a correction
whose purpose had gone ("add firmness" after the maker chose to keep the shape).

The host's framing:
- **could** holds every possible intention; costly, it punishes failure.
- **would** is governance: slow, spaced, reacting by logic.
- **should** is the oracle: it reasons from intention to outcome and aligns results to
  that intention. That is the trap, and also the escape.

Learning is not obedience. The record therefore keeps the layers apart and shows its folds.

## The cycle

1. **could:** `could(readings)` keeps a balance over 2–8 readings (weights sum to one).
   It holds a balance, not a single label, so a tentative call and a well-supported call
   stay distinguishable later. Holding more than eight readings is refused.
2. **would:** `seal_would(readings=…, predictions=…, purpose=…)` records what each reading
   predicts *would* be observed, and seals it (SHA-256 over canonical JSON) before the
   outcome is shown. `verify_seal` refuses a would that was retold afterwards.
3. **reveal:** `reveal(would, observed=…, observation=…)` either **HOLDs** (the leading
   reading came) or names a **KNOT**: what was expected, how strongly, and what came.
   - `surprise_bits` is −log₂ of the weight the observed reading had.
   - An outcome outside every offered reading is kept as `UNLISTED`; it is never forced
     into one.
   - A tie leads nowhere and is named as such.
4. **should:** `should(intention=…, act=…, unless=…, purpose=…)` ties a move to the purpose
   it serves. `carry_should(prior, current_purpose)` returns `STALE` when the purpose has
   changed: a should is derived anew, never copied forward.
5. **next row:** `voxel_context(teaching=…, knots=[…], shoulds=[…])` builds the next encounter's context
   from the teaching plus the named knots. `render_prompt` renders this context as readable
   sections (Teaching, Named knots, Encounter, Purpose) instead of a JSON dump. It describes
   the situation and never commands an answer format. Every other context renders exactly as
   before.

**Expiry through the normal path** (added after Mote's review). Earlier shoulds travel in the room, but whether
each one is current is decided only at render time: `render_voxel_request` passes it through `carry_should`
against the request's own purpose.
- A CURRENT should appears under "Current advice for this purpose".
- A STALE one appears only under "Earlier advice, kept as history; its purpose has changed, so it is not current
  advice". Its act never enters the room as advice.
- A should changed inside a room is refused.

## Would-probe

`run_probe(adapter, request, candidates)` scores fixed candidate continuations, each ending in
EOS, by total log-probability: one forward pass per candidate, with logits kept only for the
candidate positions.
- It turns the scores into a balance over the candidates.
- It compares base and adapter by KL divergence, per-candidate shift and leading change.
- There is no generation, no output cap and no format.
- Identical greedy text can hide a moved balance; this shows it.

The balance covers the listed candidates only. It is a reading of the would, not a grade, and
it activates nothing.

## Boundaries

- Records are evidence and advice, not authority.
- A seal proves the record did not change; it does not prove the expectation was wise.
- `HELD` is not a reward and `KNOT` is not a failure: a named knot is what the next row is made from.
- No model is loaded by `loom_voxels`. The probe loads nothing itself: the caller supplies an
  already loaded, separately supervised adapter.
- Nothing here trains, activates or promotes a fold.

## Carry voxel teaching through retained practice

`build_adapter_request(..., voxel=...)`, `UnifiedLoom.run(..., voxel=...)`,
`LoomBridge.run(..., voxel=...)` and `ImprovementCycle.run(..., voxel=...)` accept
an optional **canonical context string** returned by `voxel_context`. The
configured command can build the same packet without calling a model:

```sh
loom-practice packet RUN_HANDLE --kind adapter --context literal.txt \
  --voxel-context voxel.json
```

Write `voxel.json` with the exact UTF-8 string returned by `voxel_context`; do not
pretty-print it or add a trailing newline. `loom-practice improve run` also
accepts `--voxel-context voxel.json`; as before, running an attempt requires an
explicit trusted provider command. This addition supplies no provider, starts
no training, and adds no GUI control. The subscription packet route does not
accept the option. Runtime configuration rejects a persistent `voxel` field:
this teaching is selected per request.

The practice renderer adds a `VOXEL_TEACHING` section containing the existing
Teaching, Named knots and purpose-sensitive advice sections. The request's
purpose determines which advice is current; stale advice remains marked as
history. Native SEED/NACRE content and retained outcomes remain in the practice
projection. Conscience validates the complete optional block before its normal
projection checks, and the effective execution binding includes the block and
the voxel renderer source.

The original history selection happens **before** adding the optional block.
A larger voxel block cannot cause additional history to fold or depart. If the
combined context exceeds its byte bound, assembly refuses and the normal
lifecycle retains the failure. The local reader's token bound still applies;
passing the byte bound does not establish token fit. No-voxel requests keep their
original packet and prompt bytes. Old frozen studies are not modified.

A voxel context is advisory text. Its presence, a valid digest, a successful
fixture or a dispatched packet does not prove improved judgment. This route
does not reconstruct historical probabilities, invent sealed predictions from
known outcomes, or call the probability probe.
