# Use a reviewed lesson in a later source question

Yggdrasil can pass an explicitly selected Bezoar/NACRE lesson into a new
Origami provider request. The provider receives the retained lesson alongside
the current source passages and prior fold. Origami retains the context in the
encounter, and the new answer's separate checker can inspect it.

This source-checkout workflow uses the existing host-provider file exchange.
It prepares and consumes requests without automatically calling a model or
starting Nurarihyon. A caller supplies the actual answer and reviews.

## Select the original review and lesson

Follow [reviewed source graphs](reviewed-source-graph.md) to obtain:

- A completed Origami answer with its separate PASS support review.
- A NACRE lesson published from a PASS review of at least two distinct dated
  Watcher observations, with their original Bezoar HELD records retained.
- A Yggdrasil graph explicitly selecting both the answer-review handle and the
  NACRE handle. Supply the source binding's capture root when packing it.

The lesson must concern the **same exact source binding, relative source path
and expected source hash** selected for the new encounter. A matching filename
or identical bytes under a different source-binding handle do not substitute
for that binding. Existing NACRE lessons concern source discrepancy findings;
this interface does not turn an arbitrary correction into an eligible lesson.

The selected source must now be `CURRENT`. For example, an earlier drift
finding remains historical evidence after the original captured bytes have
been restored. A retained lesson cannot override a currently changed source.
Keep the original source root and encounter files: graph relocation alone does
not migrate an Origami encounter.

## Review applicability to the new question

The earlier NACRE review checked the lesson against its historical finding.
The new encounter also needs an explicit applicability decision from its
declared checker. Give that checker the lesson, its evidence, current sources
and exact new question. Save the actual response as `lesson-applicability.json`
with these fields:

```json
{
  "schema": "lesson-applicability/v1",
  "lesson_handle": "bezoar:EXACT_NACRE_HASH",
  "source_handle": "origami:EXACT_SOURCE_BINDING_HASH",
  "question": "The exact new question supplied to reuse",
  "reviewer": "separate-reviewer",
  "verdict": "APPLICABLE",
  "reason": "The checker's actual assessment of why this lesson applies here"
}
```

Replace all example values with the returned handles and actual judgment.
`UNRELATED` and `UNKNOWN` are valid decisions but refuse lesson-bearing reuse;
the tool does not silently drop a rejected lesson and proceed. The reviewer
must match the new encounter's checker, separate from its provider. Names
retain attribution; they do not authenticate who performed a review.

## Prepare the request and obtain the new answer

The variables below contain handles returned by your earlier operations. Both
lesson options are required together; omit both for ordinary source reuse.

```sh
python3 -B lodestone.py yggdrasil reuse ./review-graph.json \
  --root ./configured-corpus --records-workspace ./answer-workspace \
  --capture-root "$yg_source_handle=./capture" --review "$yg_review_handle" \
  --lesson "$bz_nacre_handle" --lesson-applicability ./lesson-applicability.json \
  --question "The exact new question supplied to reuse" \
  --epoch current-task --authority-ref local-user-request \
  --constructor task-author --consumer task-owner \
  --checker separate-reviewer --provider answer-provider
```

The result contains the new run id and provider request. Its `lesson_context`
holds the exact NACRE handle, text, finding, provenance, applicability judgment
and context hash. The historical request, review and observation chain are
reopened and checked. Current source evidence remains separately identified.

Give the request to the declared answer provider, then complete the existing
[answer and support-review exchange](reviewed-answers.md#supply-an-answer-obtain-a-separate-review-consume-it):

```sh
python3 -B lodestone.py --workspace ./answer-workspace origami advance RUN_ID \
  --response ./provider-response.json
python3 -B lodestone.py --workspace ./answer-workspace origami review-request RUN_ID \
  > ./new-answer-review-request.json
python3 -B lodestone.py --workspace ./answer-workspace origami review RUN_ID \
  --response ./new-answer-support-review.json
python3 -B lodestone.py --workspace ./answer-workspace origami answer RUN_ID \
  --review checker:EXACT_NEW_REVIEW_HASH
```

Have the checker supply `new-answer-support-review.json` from the new review
request before running the last two commands. The old answer review never
certifies the new answer. Inspect `REVIEWED_PASS`, `REVIEWED_FAIL` or
`REVIEWED_UNKNOWN` as the actual returned result.

Lesson text is untrusted advice. It supplies no action authority, and its handle
is not a valid source-evidence citation for a factual claim. Facts still need
current passage IDs. Changing a saved context or its historical dependencies
refuses reuse or subsequent review rather than preserving an old acceptance.
Original NACRE and HELD records remain unchanged.

When packing the newly reviewed answer, its graph also retains the lesson's
original source binding. Supply `--capture-root` for **both** the new answer's
source handle and that original handle, even when both map to the same source
directory. The graph preserves typed advice links and the historical review
chain separately from factual support links.

## What the checks establish

The integration fixture in `tests/machinery/test_reviewed_lesson_reuse.py`
creates historical observations, publishes a reviewed lesson, restores current
source bytes, transports the lesson through the real request exchange, consumes
a scripted answer and records its own scripted support review. It also checks
unrelated judgments, mismatched question/reviewer/source binding, absent graph
selection, source drift, changed context and attempts to cite a lesson as
source evidence. Those checks establish the software connection and refusals.
Their answers and judgments are authored test inputs, not model evaluations.

For a useful-transfer comparison, use new questions and match current evidence,
historical observations and provider conditions across arms. Compare evidence
alone, a plain instruction, a selected relevant lesson and an unrelated control.
Keep the plain instruction's content equal to the relevant lesson when measuring
the additional value or cost of retention. An unrelated lesson must be rejected
by this consumer; testing a provider's response to unrelated text is a separate
explicitly defined experimental control, not a reason to mislabel it applicable.

Under this source-specific contract, new questions can concern the same captured
binding; new source families require a separately designed evidence contract.
A narrow question can test whether a provider distinguishes two observations
from two independent incidents, instead of repeating the existing hash-freshness
guard. Freeze questions, scoring and limits before collecting responses. A
context that supplies extra historical facts only to the lesson arm would
confound that comparison.

Measure correctness, omissions and unrelated-case regressions. Record the cost
of constructing, reviewing, retaining, retrieving and applying the lesson, not
just the final answer. Origami retains request/response byte sizes and supplied
provider usage; absent usage remains unknown. Its preparation timer does not
cover all preceding graph and lesson validation. External timing and storage
accounting are needed for the complete comparison. Successful transport or a
scripted PASS alone establishes neither useful learning nor net cost savings.
