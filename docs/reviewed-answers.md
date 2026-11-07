# Answer from verified passages and retain the support review

Use a current source checkout on macOS or Linux with Python 3.10+. These commands
are not in the v0.3.2 downloads. This interface connects the
existing source-address resolver, Origami file exchange and immutable organ
records. You supply the answer and an actual separate checker through JSON
files; these commands do not call a model or invent a review.

## Select the evidence

Create source bindings using the [local memory commands](../tools/local-memory/README.md),
then save a resolver report. Keep the notebook, source capture and Origami
workspace separate:

```sh
python3 -B lodestone.py memory source-address ./notebook "gate schedule" \
  --bindings ./bindings.json --scope ./capture > addresses.json
python3 -B lodestone.py --workspace ./answer-workspace origami ask \
  --source-address-report ./addresses.json \
  --question "When does the gate close?" --epoch current \
  --authority-ref "User requested this source question" \
  --constructor answer-author --provider answer-author \
  --consumer answer-reader --checker separate-reviewer > encounter.json
```

`encounter.json` contains the run id and complete provider request. It does not
contain a model-generated answer. A report with multiple candidates needs an
explicit repeated `--source-address-selection RECORD_ID:BINDING_INDEX` option;
indices start at zero. An ambiguous report is never silently reduced to its top
hit. Only verified selected bindings are accepted, with current source and
passage hashes rechecked. The original `--root`/`--source` mode still works and
cannot be combined with a resolver report.

The resolver uses universal line boundaries while Origami uses LF boundaries.
Sources for which these disagree refuse; the tool does not silently move the
selection. Exact seeds are limited to 64 passages and 16 KiB total selected
UTF-8 bytes, additionally bounded by the requested read budget. Caller-supplied
reports retain attribution and manifest hashes; their provenance is not an
authenticated statement that a particular process ran.

## Supply an answer, obtain a separate review, consume it

Give the saved provider request to your answer provider. Its response must
follow the request's `response_schema` inside the `provider_envelope`; the
provider name must match the declared provider. Save the returned envelope as
`provider-response.json`. Replace `RUN_ID` below with the returned run id:

```sh
python3 -B lodestone.py --workspace ./answer-workspace origami advance RUN_ID \
  --response provider-response.json
python3 -B lodestone.py --workspace ./answer-workspace origami review-request RUN_ID \
  > review-request.json
```

For a terminal `ANSWER`, the review request contains the exact question,
claims, evidence, prior context, file identities and `response_schema`. Give
it to the named separate checker. The checker must return one supported,
unsupported or unknown verdict for every claim, an overall verdict, omissions,
contradictions and applicability. Save that actual response as `support-review.json`:

```sh
python3 -B lodestone.py --workspace ./answer-workspace origami review RUN_ID \
  --response support-review.json
python3 -B lodestone.py --workspace ./answer-workspace origami answer RUN_ID \
  --review checker:RECORD_HASH > reviewed-answer.json
```

Use the exact handle returned by `review`. The final JSON contains readable
Markdown with each support verdict and evidence locator. `REVIEWED_PASS` exits
zero; failed, unknown or absent support exits two. Recording a review succeeds
even when its verdict is FAIL: persistence is distinct from acceptance.
An absent review is visibly `NOT_INDEPENDENTLY_REVIEWED`. Changed sources,
changed encounter files and another run's review refuse. Claims marked
unsupported or unknown, material omissions/contradictions, and unrelated
applicability cannot accompany PASS. Role names record attribution; they do
not authenticate reviewers or prove that two independent people/models acted.
JSON evidence and review inputs permit at most 64 nested containers, checked
before decoding; punctuation inside strings does not count toward that limit.

Records are linked source → answer → review in the existing RecordStore. Keep
the workspace and its declared source capture. The record hash binds bytes,
not truth. The ordinary answer artifact remains unchanged, and no review
grants permission to execute anything.

## Apply retained knowledge to a later question

Start another `ask` with `--previous RUN_ID`, an appropriate fresh source report
and the later question. The provider sees the prior fold and must reassess it.
The new answer needs its own review: an earlier review never certifies it.
Current bytes alone do not establish relevance. A related question may benefit
from the prior fold; an unrelated question must not inherit an accepted answer.
When source bytes change, preserve the old encounter and rebind/review current
evidence rather than overwriting history.

The supplied tests and retained example use explicitly scripted answers and
reviews. They check the software connection and refusals, not semantic skill,
autonomous learning or a saving on unseen tasks. The 31-component programme
map remains unchanged. No daemon dispatch, neural adapter, production update
or dependency installation is part of these commands.
