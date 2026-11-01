"""Explicit historical-observation capture; strict complete-capture CLI is unchanged."""
from pathlib import Path
from . import bezoar_capture as capture
# Explicit contract facade: all physical integrity and resource machinery is shared.
from .bezoar_capture import (observation_manifest as manifest, resolve_observation as resolve,
    preflight_observation as preflight, prepare_observation as prepare,
    observe_resolved as evaluate_resolved, publish_observation as publish, read_observation as read,
    ExistingStore, sibling, copy_records, stage_requirement, entry_requirement,
    MAX_FILE_BYTES, MAX_MANIFEST_BYTES, STAGE_LIMIT)



def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with args.spec.open("rb") as stream:
        raw = stream.read(capture.MAX_MANIFEST_BYTES +1)
    spec = capture.r.parse(raw, capture.MAX_MANIFEST_BYTES)
    print(capture.canonical(capture.prepare_observation(
        args.root.absolute(), spec, args.output.absolute())).decode("utf-8"))


if __name__ == "__main__":
    main()
