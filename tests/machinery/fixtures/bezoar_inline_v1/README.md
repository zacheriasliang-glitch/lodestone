# Historical inline v1 compatibility fixture

Two byte-exact synthetic `organ-record/v1` records retained from the isolated
`adapter-fixture` case. No production state or private corpus is included.
The original synthetic result was saved on 30 October 2026; its source declares
one invalid `BOGUS` receipt mode, one registry holder and a blocked Origami result.

- Reconciliation record: `93e0a43c0f54e6ef855f3ee4538a3512b4839de5c81efa1be10935d53426c7e9` (2738 bytes).
- Inline source record: `e34af3c649aa21c1f641709db731a8814a8485b96d453fbe31837e39083e3993` (1952 bytes).

Each filename is the SHA-256 of its canonical bytes. Keep these bytes fixed: the
compatibility test uses the original handle, republishes both records with exact
identity checks, then independently recomputes the report and compares it with
the saved historical report. Missing, modified or invalid records must fail.
The report's historical implementation hashes are retained as data, not updated
to make a current implementation match. The test writes only to its temporary
store and scratch directory; it never initializes or writes to this fixture.
