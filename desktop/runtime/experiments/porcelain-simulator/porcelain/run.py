"""Run/resume deterministic action schedules; retain every accepted material state."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
import scipy
import skfem
from dataclasses import replace

from . import __version__
from .core import Material, Solver, cylinder_mesh


SIMULATION_SOURCES = ("__init__.py", "core.py", "hands.py", "run.py", "pair.py")


def source_manifest():
    """Per-file SHA-256 of every simulation-affecting module (v0.2: hands and pair included)."""
    here = Path(__file__).resolve().parent
    return {name: sha256((here/name).read_bytes()).hexdigest() for name in SIMULATION_SOURCES if (here/name).exists()}


def source_hash():
    """One fingerprint over the simulation sources, in SIMULATION_SOURCES order.

    The fingerprint binds all five modules that define simulation behavior,
    including hand contact and paired scene execution.
    """
    here = Path(__file__).resolve().parent
    digest_all = sha256()
    for name in SIMULATION_SOURCES:
        path = here/name
        if path.exists():
            digest_all.update(name.encode()+b"\0"+path.read_bytes()+b"\0")
    return digest_all.hexdigest()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_config(config):
    allowed = {"name", "description", "synthetic", "geometry", "material", "gravity", "slip_beta", "dt", "schedule", "max_volume_drift"}
    if set(config)-allowed:
        raise ValueError(f"Unknown configuration fields: {set(config)-allowed}")
    if config.get("synthetic") is not True:
        raise ValueError("v0.1 only supports explicitly synthetic configurations.")
    if not np.isfinite(config["dt"]) or config["dt"] <= 0:
        raise ValueError("dt must be positive and finite.")
    schedule = config["schedule"]
    if len(schedule) < 2 or schedule[0]["time"] != 0:
        raise ValueError("Schedule must start at zero and contain at least two knots.")
    times = np.array([k["time"] for k in schedule])
    if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("Schedule times must be finite and strictly increasing.")
    for knot in schedule:
        if set(knot) != {"time", "wheel_omega", "platen_height"}:
            raise ValueError("Each knot needs time, wheel_omega and platen_height.")
        if not np.isfinite(knot["wheel_omega"]):
            raise ValueError("Wheel speed must be finite.")
        if knot["platen_height"] is not None and (not np.isfinite(knot["platen_height"]) or knot["platen_height"] <= 0):
            raise ValueError("Platen height must be positive or null.")
    return config


def action_at(schedule, t):
    for a, b in zip(schedule[:-1], schedule[1:]):
        if t <= b["time"]+1e-12:
            f = np.clip((t-a["time"])/(b["time"]-a["time"]), 0, 1)
            omega = (1-f)*a["wheel_omega"]+f*b["wheel_omega"]
            height = None
            if a["platen_height"] is not None and b["platen_height"] is not None:
                height = (1-f)*a["platen_height"]+f*b["platen_height"]
            return float(omega), None if height is None else float(height)
    return schedule[-1]["wheel_omega"], schedule[-1]["platen_height"]


def make_solver(config):
    return Solver(cylinder_mesh(**config["geometry"]), Material(**config["material"]),
                  gravity=config["gravity"], slip_beta=config["slip_beta"],
                  max_volume_drift=config.get("max_volume_drift", .005))


def surface_triangles(mesh):
    triangles = []
    for f in mesh.boundary_facets():
        a, b, c = mesh.facets[:, f]
        ab, bc, ac = mesh.dofs.edge_dofs[0, mesh.f2e[:, f]]
        triangles.extend(((a, ab, ac), (b, bc, ab), (c, ac, bc), (ab, bc, ac)))
    return np.array(triangles)


def write_checkpoint(path, solver, history, config, records):
    metadata = dict(schema="porcelain-episode-v1", version=__version__, config=config,
                    config_sha256=digest(config), solver_sha256=source_hash(), source_manifest=source_manifest(),
                    dependencies=dict(python=platform.python_version(), numpy=np.__version__,
                                      scipy=scipy.__version__, skfem=skfem.__version__),
                    initial_volume=solver.initial_volume, synthetic=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as f:
        np.savez_compressed(f, positions=np.asarray(history["positions"]),
                            velocities=np.asarray(history["velocities"]),
                            pressures=np.asarray(history["pressures"]),
                            times=np.asarray(history["times"]),
                            tetrahedra=solver.mesh.t, surface_triangles=surface_triangles(solver.mesh),
                            metadata=json.dumps(metadata, sort_keys=True),
                            records=json.dumps(records, sort_keys=True))
    temporary.replace(path)


def run(config, directory, resume=False, stop_after=None, progress=True):
    config = validate_config(config)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = directory/"episode.npz"
    solver = make_solver(config)
    history = dict(positions=[solver.mesh.doflocs.copy()], velocities=[solver.u.copy()],
                   pressures=[solver.p.copy()], times=[0.])
    records = []
    if checkpoint.exists():
        if not resume:
            raise FileExistsError(f"{checkpoint} exists; use --resume or a new directory.")
        with np.load(checkpoint, allow_pickle=False) as data:
            metadata = json.loads(str(data["metadata"]))
            if metadata["solver_sha256"] != source_hash() or metadata["config_sha256"] != digest(config):
                raise ValueError("Checkpoint source/config hash differs. Resume requires the same solver and configuration.")
            for key in history:
                history[key] = list(data[key])
            records = json.loads(str(data["records"]))
        solver.mesh = replace(solver.mesh, doflocs=history["positions"][-1].copy())
        solver.u, solver.p = history["velocities"][-1].copy(), history["pressures"][-1].copy()
        solver.t, solver.step_index = float(history["times"][-1]), len(records)
    elif resume:
        raise FileNotFoundError("No checkpoint to resume.")
    else:
        write_checkpoint(checkpoint, solver, history, config, records)
    (directory/"config.json").write_text(json.dumps(config, indent=2)+"\n")
    schedule = config["schedule"]
    started = time.monotonic()
    end = schedule[-1]["time"]
    status = "complete"
    try:
        while solver.t < end-1e-12:
            if stop_after is not None and solver.step_index >= stop_after:
                status = "paused"
                break
            next_knot = next(k["time"] for k in schedule if k["time"] > solver.t+1e-12)
            dt = min(config["dt"], next_knot-solver.t, end-solver.t)
            omega, height = action_at(schedule, solver.t+dt)
            record = solver.step(dt, omega=omega, platen_height=height)
            records.append(record)
            history["positions"].append(solver.mesh.doflocs.copy())
            history["velocities"].append(solver.u.copy())
            history["pressures"].append(solver.p.copy())
            history["times"].append(solver.t)
            write_checkpoint(checkpoint, solver, history, config, records)
            if progress:
                print(f"step {solver.step_index:3d} t={solver.t:.4f}s  "
                      f"volume drift={record['volume_relative_drift']:+.3%}  "
                      f"hand={record['hand_force_z']:.3f}N  iterations={record['nonlinear_iterations']}", flush=True)
    except Exception as error:
        (directory/"failure.json").write_text(json.dumps(dict(status="failed", error=str(error),
                                                last_accepted_time=solver.t, step=solver.step_index), indent=2)+"\n")
        raise
    (directory/"diagnostics.jsonl").write_text("".join(json.dumps(r, sort_keys=True)+"\n" for r in records))
    summary = dict(status=status, simulated_seconds=solver.t, accepted_steps=len(records),
                   wall_seconds_this_invocation=time.monotonic()-started,
                   max_absolute_volume_drift=max((abs(r["volume_relative_drift"]) for r in records), default=0),
                   max_nonlinear_residual=max((r["nonlinear_residual"] for r in records), default=0),
                   synthetic=True, calibrated=False, source_sha256=source_hash(), config_sha256=digest(config))
    (directory/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    if (directory/"failure.json").exists():
        (directory/"failure.json").unlink()
    return records, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after", type=int)
    args = parser.parse_args()
    records, summary = run(json.loads(args.config.read_text()), args.out, args.resume, args.stop_after)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
