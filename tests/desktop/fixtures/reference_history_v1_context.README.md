# Legacy native context fixture

`reference_history_v1_context.json` is the exact 3,644-byte native context emitted by the prior released runtime, commit `655fa55672a4c3ae2d0284da881921b7bed97c78`, using its labelled generic bootstrap teaching and no historical runs.

The fixed encounter, purpose and reader are in `test_default_v1_context_matches_prior_release_golden_bytes`. Generation used the prior release's own bootstrap helper and configured native preparation, then closed the preparation as `ABSTAINED` without any provider or model. It contains synthetic teaching only, no user history or local paths.

SHA-256: `fd77d2aeeacc1e9ca2a53072f0c7b5bc0724b1480081c8c554bed0e24704ea8f`. Prior `bezoar_learning.py` SHA-256: `30836076bfb6c4dbf19f5c6d6dd5489db01edd67c67404cdc12f987026857322`.

The fixture has no trailing newline because the assertion compares the exact stored context bytes. The separate historical-record and projection tests cover retained outcomes, incomplete runs, multiline corrections and derived lessons.
