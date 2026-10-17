# Optional Porcelain mechanics

Porcelain runs local synthetic clay-mechanics examples and records shapes,
forces, pressure, velocity and numerical diagnostics. It is an optional module
with its own Python environment. The standard-library Lodestone tools work
without its dependencies.

The overview works without optional dependencies:

```bash
python3 lodestone.py porcelain --help
```

Follow the [installation and first-run guide](../experiments/porcelain-simulator/README.md).
After installing that environment, module-specific help is:

```bash
python3 lodestone.py porcelain run --help
python3 lodestone.py porcelain pair --help
```

Install the optional environment first; on Windows use `py -3` in place of
`python3`. The default environment is `.venv-porcelain` at the release root.
`LODESTONE_PORCELAIN_VENV` can select another environment. Explicit `--out`
arguments choose numerical output locations, relative to the calling directory.

The module includes a platen example and four pairs: adding a squeeze,
changing compliance, stopping the wheel, and splitting equivalent contact
support. Renderers read saved states; verification commands run numerical
problems and can take many minutes.

The supplied material is synthetic and uncalibrated. Numerical verification
does not establish physical porcelain fidelity, a teaching outcome, or learned
abstraction. Release packaging checked source preservation and lightweight
entry points; historical scientific runs were not repeated. See the module's
[limits](../experiments/porcelain-simulator/README.md#model-limits) and
[provenance](../experiments/porcelain-simulator/PROVENANCE.md).
