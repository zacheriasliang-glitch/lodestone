# Porcelain mechanics

Porcelain is an optional local simulator for exploring how declared wheel and
contact motions affect a synthetic clay body. It includes a solid cylinder with
a flat platen, a hollow wall with inner and outer contact patches, paired
examples, numerical checks, and plots of saved states.

**The material coefficients are illustrative and uncalibrated.** These examples
do not establish real porcelain behavior, teaching effectiveness, or learned
abstraction. The release contains source and inputs; running an example creates
its results on your computer.

## Install the optional environment

Use Python 3.12 or newer. Run these commands from the Lodestone release root,
the directory containing `lodestone.py`. The numerical dependencies stay in
their own virtual environment; the core Lodestone commands do not need them.

macOS or Linux:

```bash
python3 -m venv .venv-porcelain
.venv-porcelain/bin/python -m pip install -r experiments/porcelain-simulator/requirements.txt
```

Windows PowerShell:

```powershell
py -3 -m venv .venv-porcelain
.\.venv-porcelain\Scripts\python.exe -m pip install -r experiments/porcelain-simulator/requirements.txt
```

Installation downloads NumPy, SciPy, scikit-fem, Matplotlib and Pillow. Simulation
and rendering then run locally without an account, API key, or network access.
`requirements-tested.txt` records the exact dependency versions used in the
historical Python 3.14 environment; it is an optional replacement for
`requirements.txt`, not a guarantee of availability on every platform.

The toolkit selects `.venv-porcelain` at the release root. To use an existing
environment elsewhere, set `LODESTONE_PORCELAIN_VENV` to its absolute directory.
The toolkit does not install dependencies automatically. On Windows, use
`py -3` wherever the following examples show `python3`.

## Inspect the commands

This prints help without advancing the solver:

```bash
python3 lodestone.py porcelain run --help
python3 lodestone.py porcelain pair --help
```

All paths in these toolkit commands are relative to your current directory.
The examples below assume you remain at the release root.

For a small machine, set numerical-library thread counts to one before starting.
In a macOS/Linux shell:

```bash
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
```

In PowerShell:

```powershell
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"
```

## Create and inspect an episode

Start with one accepted step. Choose a fresh output directory for each new run:

```bash
python3 lodestone.py porcelain run experiments/porcelain-simulator/examples/compress_and_release.json --out runs/porcelain/platen --stop-after 1
python3 lodestone.py porcelain report runs/porcelain/platen/episode.npz --out runs/porcelain/platen
```

The renderer creates `simulation_summary.png` from the saved episode. Add
`--animate` to create `simulation.gif`. Rendering does not advance the simulation.

To continue the same platen episode through its full 0.2-second schedule:

```bash
python3 lodestone.py porcelain run experiments/porcelain-simulator/examples/compress_and_release.json --out runs/porcelain/platen --resume
python3 lodestone.py porcelain report runs/porcelain/platen/episode.npz --out runs/porcelain/platen --animate
```

Full simulations and numerical checks can take many minutes on a small CPU;
simulated time is not wall-clock runtime. The solver saves each accepted state
to `episode.npz`. A failed step keeps the last accepted checkpoint and records
`failure.json`. Resume requires the same configuration and simulation source
digest. Retain the same dependencies when comparing runs; cross-platform bitwise
reproducibility has not been established.

## Compare two scenes

This explicitly runs both scenes in a new directory:

```bash
python3 lodestone.py porcelain pair experiments/porcelain-simulator/examples/pairs/support-vs-squeeze.json --out runs/porcelain/support-pair
python3 lodestone.py porcelain report_pair runs/porcelain/support-pair --animate
```

The pair writer creates `a/` and `b/` scene directories plus `comparison.json`.
The renderer creates `pair_summary.png` and, with `--animate`, scene GIFs.
Pair execution has no `--resume` option. A partial paired run needs a new output
directory for a fresh run; keep partial artifacts if you need their provenance.

| Example in `examples/pairs/` | Declared comparison |
|---|---|
| `support-vs-squeeze.json` | Inner contact alone versus the same contact plus an outer squeeze |
| `wrist-support.json` | Two compliance values under the same imposed radial disturbance |
| `driven-vs-stopped.json` | Driven and stopped wheel under the same hooked hold |
| `one-vs-two-contacts.json` | One patch versus adjacent halves with the same total support, zero compliance and identical motion: an equivalence check |

Splitting a zero-compliance patch with identical motion does not create a new
physical boundary condition. Shared nodes belong to the first-listed hand, so
individual force attribution depends on that ordering. Making a physically
different two-contact experiment requires a separately declared change in
contact laws, motion, or support.

## Run numerical checks when ready

These commands execute numerical problems, including refinement and restart
checks. They are separate from the lightweight command-help checks above:

```bash
python3 lodestone.py porcelain verify --out runs/porcelain/verification.json
python3 lodestone.py porcelain verify_hands --out runs/porcelain/verification-hands.json
```

Each command prints check results and writes JSON. A failed check exits with a
nonzero status. Numerical verification tests the implemented method under its
declared laws; it is not experimental calibration. Historical numerical checks
exist for the upstream synthetic baseline. This public export was checked for
syntax, imports, command help and unchanged numerical inputs; those historical
simulations were not rerun for packaging.

## Configuration and outputs

Copy an included JSON example before changing it. Inputs use SI units: metres,
seconds, kilograms, newtons and pascals; angles use radians. Keep `synthetic: true`.
A positive `dt` and a strictly increasing schedule starting at zero are required.
The platen runner takes a single configuration. The pair runner takes an object
with a `name`, `declared_difference`, and exactly two `scenes`.

Cylinder geometry declares `radial`, `vertical`, `radius`, and `height`. Hollow
geometry declares `radial`, `angular`, `vertical`, `inner_radius`, `outer_radius`,
and `height`. Each hand has a unique name, wall side, angular sector, height band,
and compliance in metres per newton. Every pair schedule knot supplies a
`hand_radii` value for each hand, in addition to `time` and `wheel_omega`.

Each scene writes `config.json`, `summary.json`, `diagnostics.jsonl`, and
`episode.npz`. Episodes contain positions, velocities, pressures, times, mesh
connectivity, JSON metadata and JSON diagnostic records. Read them with
`numpy.load(path, allow_pickle=False)`. Frame zero is the initial state;
diagnostic record `i` describes the step producing frame `i + 1`.

## Model limits

The model uses regularized Bingham/Herschel–Bulkley flow, quadratic moving
tetrahedra, a driven base, gravity, and unilateral contact. Its material update
is first order and its weak incompressibility constraint does not conserve
finite-step volume exactly. Volume drift and residuals are recorded separately.
Time-step and mesh sensitivity need evaluation for each new question.

Wall contact uses nodal constraints and a declared facet-majority rule for slip.
The compliance law uses the previous step's force. It does not model elasticity,
stress history, surface tension, drying, fracture, remeshing, or arbitrary hand
geometry. Use short deformations. These limitations preclude treating a force
trace as a measured human or clay property.

## Direct module use

The toolkit is optional. From `experiments/porcelain-simulator`, select the
environment's Python and invoke a module directly. On macOS/Linux:

```bash
../../.venv-porcelain/bin/python -B -m porcelain.run --help
../../.venv-porcelain/bin/python -B -m porcelain.pair --help
```

On Windows the equivalent interpreter is
`..\..\.venv-porcelain\Scripts\python.exe`. The other entry points are
`porcelain.report`, `porcelain.report_pair`, `porcelain.verify`, and
`porcelain.verify_hands`. Input and output paths remain relative to the current
directory, so use `examples/...` when running directly from this module directory.

See [provenance](PROVENANCE.md) and the [source manifest](SOURCE_MANIFEST.json) for
attribution, public file hashes, and export changes.
