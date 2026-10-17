# Lodestone orchestrator

Two local Python planners and a proposal-only attachment bridge. Start with
[the public guide](../docs/orchestrator.md). Python 3.10+; standard library only.

```sh
python3 -B orchestrator/bootstrap.py --help
cp -R orchestrator/examples/project ./my-project
python3 -B orchestrator/bootstrap.py --root ./my-project --experiment 001 --goal "Audit the demonstration" --dry-test
python3 -B orchestrator/bootstrap.py --root ./my-project --experiment 001 --goal "Audit the demonstration"
```

The first command after copying only observes; the second writes one decision
and one task packet under `my-project/orchestrator/output/`. It does not run the
task. A selected `BUILDER` route is a recommendation, not an agent launch.

Tests use fresh synthetic temporary projects:

```sh
python3 -B -m unittest discover -s orchestrator/tests
```
