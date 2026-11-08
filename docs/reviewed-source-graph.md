# Follow reviewed answers to their current sources

This source-checkout interface connects existing Origami review records,
Yggdrasil graphs, Watcher observations and Bezoar lessons. Use macOS or Linux
with Python 3.10+. It does not call a model, start a service or alter a source.

## Capture the reviewed evidence

First complete the [reviewed-answer workflow](reviewed-answers.md). Its review
receipt returns `handle`, `answer_handle` and `source_handle`. Keep those exact
values: the review handle identifies the attributed judgment; the source handle
identifies the exact captured source bindings.

Yggdrasil's existing `pack` route requires your separately configured canon and
receipt corpus. A public source checkout does not carry a laboratory adoption.
Use that configured corpus as `--root`; keep generated graphs outside it. The
examples below use shell variables containing handles returned by your own run:

```sh
python3 -B lodestone.py yggdrasil pack \
  --root ./configured-corpus --records-workspace ./answer-workspace \
  --record "$yg_review_handle" \
  --capture-root "$yg_source_handle=./capture" \
  --output ./review-graph.json
python3 -B lodestone.py yggdrasil expand ./review-graph.json \
  --anchor "$yg_review_handle" --direction ancestors --depth 6
```

Repeat `--record` for additional selected records. The graph includes their
reachable dependencies; unrelated records appended later do not invalidate
that selection. Without `--record`, the original full-workspace inventory rule
applies, including detection of additions and removals.

Repeat `--capture-root` for each distinct source-binding handle. Even equal
relative filenames in different captures remain separately addressed. Saved
absolute source paths are retained as attribution, never silently reopened.
The supplied folder must contain the exact bound files and passages. Missing,
changed, malformed or aliased input refuses capture; no partial graph is
published. Existing source, passage and graph size limits remain enforced.

## Reopen evidence before reuse

```sh
python3 -B lodestone.py yggdrasil reenter ./review-graph.json \
  --root ./configured-corpus --records-workspace ./answer-workspace \
  --capture-root "$yg_source_handle=./capture"
```

Supply each capture again. After moving an intact capture, map its same handle
to the new folder explicitly. `CURRENT` concerns the declared graph's bytes;
it does not turn a FAIL/UNKNOWN review into PASS or certify a new question.
Missing required inputs remain unavailable; changed evidence requires refresh.
Older graphs that retained source-binding records without capturing their
external evidence cannot claim complete freshness under this interface.

Use `reuse` to prepare a new question from the reviewed passages:

```sh
python3 -B lodestone.py yggdrasil reuse ./review-graph.json \
  --root ./configured-corpus --records-workspace ./answer-workspace \
  --capture-root "$yg_source_handle=./capture" --review "$yg_review_handle" \
  --question "What does this evidence imply for the next task?" \
  --epoch current-task --authority-ref local-user-request \
  --constructor task-author --consumer task-owner \
  --checker separate-reviewer --provider answer-provider
```

This returns a new Origami run and provider request containing the exact retained
passages and previous fold. It reopens the original encounter and requires its
PASS review. Supply an actual answer through `origami advance` or the existing
provider interface, then obtain a separate review for that new question. The
prior review never certifies the new answer. No provider is invoked by `reuse`.
To also supply an explicitly selected NACRE lesson, use the
[reviewed lesson consumer](reviewed-lessons.md). It requires a separate
applicability judgment for the exact new question and source binding; the
lesson remains advice and the new answer still requires its own review.
The existing exact-seed limit is 64 passages and at most 16 KiB of passage text
(or a smaller supplied read budget); larger selections refuse before a new run.

This consumer requires the original encounter's source folder and saved run
files. Graph re-entry supports explicit source relocation; that does not migrate
an Origami encounter or change its original review bindings.

## Retain discrepancies without erasing them

Watcher traverses the selected record chain and reopens its external sources
using the same binding reader:

```sh
python3 -B lodestone.py --workspace ./answer-workspace watcher observe \
  --handle "$yg_review_handle" \
  --capture-root "$yg_source_handle=./capture"
python3 -B lodestone.py --workspace ./answer-workspace bezoar hold \
  --handle "$yg_observation_handle"
```

Use the observation handle returned by Watcher in `hold`. Drift, missing roots,
missing files and coverage limits are retained as findings. A clean observation
does not clear an older held record. Source and record hashes establish custody,
not whether the answer is true.

## Keep a reviewed boundary lesson as nacre

Choose at least two distinct dated observations of the same source/finding,
each already held by Bezoar. Supply a specific lesson and separate reviewer:

```sh
python3 -B lodestone.py --workspace ./answer-workspace bezoar lesson-request \
  --held "$bz_first_held" --held "$bz_second_held" \
  --lesson "Reopen the captured source before reusing its reviewed answer." \
  --producer lesson-author --checker separate-reviewer
```

Give the returned request and its evidence to that checker. Save the checker's
actual response using the envelope below, then record it. Replace every sample
value with the returned handle, request hash and actual review; include exactly
one layer verdict for each observation in the request:

```json
{
  "schema": "bezoar-nacre-review/v1",
  "request_handle": "bezoar:REQUEST_HASH",
  "request_sha256": "REQUEST_BODY_HASH",
  "reviewer": "separate-reviewer",
  "verdict": "PASS",
  "layer_verdicts": [
    {"observation": "watcher:FIRST_HASH", "verdict": "SUPPORTED", "reason": "Actual layer review"},
    {"observation": "watcher:SECOND_HASH", "verdict": "SUPPORTED", "reason": "Actual layer review"}
  ],
  "applicability": "SAME_SOURCE_AND_FINDING",
  "reason": "Actual assessment of this lesson and its limits"
}
```

The overall verdict can be `PASS`, `FAIL` or `UNKNOWN`; each layer can be
`SUPPORTED`, `UNSUPPORTED` or `UNKNOWN`. Applicability can also be `UNRELATED`
or `UNKNOWN`. A PASS requires all layers supported and the stated source/finding
applicability. Do not copy the sample PASS as a substitute for the review.

```sh
python3 -B lodestone.py --workspace ./answer-workspace bezoar lesson \
  --request "$bz_lesson_request" --review ./lesson-review.json
```

A supported PASS retains a NACRE/LAYERED lesson linked to its evidence and
review. FAIL and UNKNOWN remain retained judgments; they do not become pearls.
The original held seeds remain present in every case. Here *pearl* names the
retained nacre lesson, not another storage system. Reviewer names are attributed
identities, not authentication. Two dated observations can be from one ongoing
incident; they do not establish two independent failures or a general lesson.

Read a retained lesson with `python3 -B lodestone.py --workspace
./answer-workspace bezoar lesson-show --handle "$bz_nacre_handle"`.
Its dependency chain verifies the historical request, review and observations.
It does not certify the present source. To graph both the lesson and current
evidence, select the NACRE handle **and** the original answer's review handle
with separate `--record` arguments and supply its `--capture-root` again.

The existing [compact-context skill](../skills/lodestone-compact-context/SKILL.md)
can checkpoint the selected graph, records and capture files. Re-entry must
still verify them. The engineering examples use scripted provider/checker
responses; operational success is not evidence of useful learning.
