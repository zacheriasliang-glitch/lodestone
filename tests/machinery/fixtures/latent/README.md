# Original codec compatibility fixture

`codec_4386273.py` is the unchanged 9,990-byte original codec from repository
`zacheriasliang-glitch/lodestone-lab`, commit
`4386273d116907f082aa5672ba803d9d36ee62a9`, path
`experiments/001-rule30/Genepool/crystal-latent/codec.py`.

SHA-256: `64b5f0bea5683191a8914f0cde36ce3ec9885ae3b951168edea8460e4e342068`

Git blob SHA-1: `c6c8416aea232e3bef28943627f74c10557b8521`

Tests verify this pin before loading the fixture. They use invented byte strings
and tables to compare the original wire reader/writer with the port. The fixture
contains no model, training, corpus, credentials or recovered research inputs.
Do not edit it when updating the port: its independence is the compatibility check.

The compatibility test additionally pins these original source bodies at commit
`5cd4cf8aa0211b0264607b0e17c9d91346bb22cc`, under the same historical directory:

- `store_adapter.py` function `restore_source`, lines 373–408, 1,824 bytes:
  SHA-256 `526f12ba6c08a7e7bc8cc3113650b08ea2b33804f8bec1857a2913e8e2c9394f`.
- `compact_checkpoint.py` class `ReadSession`, lines 344–487, 8,189 bytes:
  SHA-256 `afcae4627ca3f18e8edb60a401241e2d598d382a9bfbef2d67ffcb33c77733a1`.

Each body includes its final newline and has no import rewrites. Tests compare
`inspect.getsource` bytes with these pins. Separate synthetic integration tests
execute the matching bodies on newly emitted store/catalog records. This avoids
copying whole historical modules or their model preparation fixtures.
