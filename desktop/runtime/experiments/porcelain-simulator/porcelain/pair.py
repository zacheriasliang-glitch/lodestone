"""Paired teaching comparisons on a hollow clay wall with hand patches (v0.2).

A pair is two scenes that share mesh, material, wheel schedule and every hand that
both contain, and differ in exactly the declared way (an extra outer-wall squeeze,
a compliance value, a stopped wheel, one or two contacts). Each scene is run with
the v0.1 solver extended by hands.py; every accepted step is checkpointed in the
same episode.npz layout as porcelain.run so porcelain.report can draw it.

Outputs per scene: episode.npz, diagnostics.jsonl, summary.json, config.json.
Output per pair: comparison.json with the declared difference and the measured
differences in wall thickness, asymmetry, off-axis offset, contact force, mass
balance and runtime. Nothing here chooses an outcome; the numbers are whatever
the solver produced under the declared laws. Material coefficients are illustrative.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

import numpy as np

from .core import Material, Solver
from .hands import Hand, annulus_mesh
from .run import digest, source_hash, source_manifest, write_checkpoint

SCENE_FIELDS = {"name", "description", "synthetic", "geometry", "material", "gravity", "slip_beta", "dt",
                "max_volume_drift", "hands", "schedule"}


def validate_scene(scene):
    unknown = set(scene) - SCENE_FIELDS
    if unknown:
        raise ValueError(f"Unknown scene fields: {unknown}")
    if scene.get("synthetic") is not True:
        raise ValueError("Scenes must be explicitly synthetic (illustrative coefficients).")
    g = scene["geometry"]
    if set(g) != {"radial", "angular", "vertical", "inner_radius", "outer_radius", "height"}:
        raise ValueError("Hollow geometry needs radial, angular, vertical, inner_radius, outer_radius, height.")
    hands = scene["hands"]
    names = [h["name"] for h in hands]
    if len(set(names)) != len(names):
        raise ValueError("Hand names must be unique.")
    schedule = scene["schedule"]
    if len(schedule) < 2 or schedule[0]["time"] != 0:
        raise ValueError("Schedule must start at zero with at least two knots.")
    times = np.array([k["time"] for k in schedule])
    if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("Schedule times must be finite and strictly increasing.")
    for knot in schedule:
        if set(knot) != {"time", "wheel_omega", "hand_radii"} or set(knot["hand_radii"]) != set(names):
            raise ValueError("Each knot needs time, wheel_omega and a hand_radii entry per hand.")
    return scene


def action_at(schedule, names, t):
    for a, b in zip(schedule[:-1], schedule[1:]):
        if t <= b["time"] + 1e-12:
            f = float(np.clip((t - a["time"]) / (b["time"] - a["time"]), 0, 1))
            omega = (1 - f) * a["wheel_omega"] + f * b["wheel_omega"]
            radii = [(1 - f) * a["hand_radii"][n] + f * b["hand_radii"][n] for n in names]
            return float(omega), radii
    last = schedule[-1]
    return float(last["wheel_omega"]), [last["hand_radii"][n] for n in names]


def make_solver(scene):
    g = scene["geometry"]
    mesh = annulus_mesh(**g)
    hands = [Hand(**h) for h in scene["hands"]]
    return Solver(mesh, Material(**scene["material"]), gravity=scene["gravity"], slip_beta=scene["slip_beta"],
                  max_volume_drift=scene.get("max_volume_drift", .005), hands=hands,
                  inner_radius=g["inner_radius"], outer_radius=g["outer_radius"])


def run_scene(scene, directory, progress=True, stop_after=None):
    scene = validate_scene(scene)
    directory = Path(directory)
    if (directory / "episode.npz").exists():
        raise FileExistsError(f"{directory} already holds an episode; use a new directory.")
    directory.mkdir(parents=True, exist_ok=True)
    solver = make_solver(scene)
    names = [h["name"] for h in scene["hands"]]
    history = dict(positions=[solver.mesh.doflocs.copy()], velocities=[solver.u.copy()],
                   pressures=[solver.p.copy()], times=[0.])
    records = []
    (directory / "config.json").write_text(json.dumps(scene, indent=2) + "\n")
    write_checkpoint(directory / "episode.npz", solver, history, scene, records)
    schedule, end = scene["schedule"], scene["schedule"][-1]["time"]
    started = time.monotonic()
    status = "complete"
    try:
        while solver.t < end - 1e-12:
            if stop_after is not None and solver.step_index >= stop_after:
                status = "paused"
                break
            next_knot = next(k["time"] for k in schedule if k["time"] > solver.t + 1e-12)
            dt = min(scene["dt"], next_knot - solver.t, end - solver.t)
            omega, radii = action_at(schedule, names, solver.t + dt)
            record = solver.step(dt, omega=omega, hand_radii=radii)
            record["wall_seconds_step"] = time.monotonic() - started - sum(r.get("wall_seconds_step", 0) for r in records)
            records.append(record)
            history["positions"].append(solver.mesh.doflocs.copy())
            history["velocities"].append(solver.u.copy())
            history["pressures"].append(solver.p.copy())
            history["times"].append(solver.t)
            write_checkpoint(directory / "episode.npz", solver, history, scene, records)
            if progress:
                print(f"[{scene['name']}] step {solver.step_index:3d} t={solver.t:.4f}s "
                      f"F={[round(f, 3) for f in record['hand_radial_force_n']]}N "
                      f"thick={record['wall_thickness_mean_m']*1e3:.3f}mm asym={record['wall_thickness_asymmetry_max_m']*1e3:.3f}mm "
                      f"off={record['outer_centroid_offset_max_m']*1e3:.3f}mm drift={record['volume_relative_drift']:+.3%} "
                      f"it={record['nonlinear_iterations']} {record['wall_seconds_step']:.0f}s", flush=True)
    except Exception as error:
        (directory / "failure.json").write_text(json.dumps(dict(status="failed", error=str(error),
                                               last_accepted_time=solver.t, step=solver.step_index), indent=2) + "\n")
        raise
    (directory / "diagnostics.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    last = records[-1] if records else {}
    summary = dict(status=status, scene=scene["name"], simulated_seconds=solver.t, accepted_steps=len(records),
                   wall_seconds=time.monotonic() - started,
                   max_absolute_volume_drift=max((abs(r["volume_relative_drift"]) for r in records), default=0),
                   max_nonlinear_residual=max((r["nonlinear_residual"] for r in records), default=0),
                   final_wall_thickness_mean_m=last.get("wall_thickness_mean_m"),
                   final_wall_thickness_asymmetry_max_m=last.get("wall_thickness_asymmetry_max_m"),
                   final_outer_centroid_offset_max_m=last.get("outer_centroid_offset_max_m"),
                   peak_hand_force_n={n: max((abs(r["hand_radial_force_n"][i]) for r in records), default=0) for i, n in enumerate(names)},
                   total_bulk_dissipation_j=float(sum(r["bulk_dissipation"] * r["dt"] for r in records)),
                   total_slip_dissipation_j=float(sum(r["slip_dissipation"] * r["dt"] for r in records)),
                   wheel_work_j=float(sum(r["wheel_torque_z"] * r["wheel_omega"] * r["dt"] for r in records)),
                   synthetic=True, calibrated=False, source_sha256=source_hash(), source_manifest=source_manifest(), config_sha256=digest(scene))
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return records, summary


def compare(pair, summaries, out):
    a, b = summaries
    keys = ["final_wall_thickness_mean_m", "final_wall_thickness_asymmetry_max_m", "final_outer_centroid_offset_max_m",
            "max_absolute_volume_drift", "total_bulk_dissipation_j", "total_slip_dissipation_j", "wheel_work_j", "wall_seconds"]
    result = dict(schema="porcelain-pair-comparison/v1", pair=pair["name"], declared_difference=pair["declared_difference"],
                  isolates=pair.get("isolates"), scenes=[a["scene"], b["scene"]],
                  measured={k: {"a": a.get(k), "b": b.get(k), "b_minus_a": (None if a.get(k) is None or b.get(k) is None else b[k] - a[k])} for k in keys},
                  peak_hand_force_n={"a": a["peak_hand_force_n"], "b": b["peak_hand_force_n"]},
                  steps={"a": a["accepted_steps"], "b": b["accepted_steps"]},
                  status={"a": a["status"], "b": b["status"]},
                  reading="numbers only; whether the difference matters for a learner is not something this comparison can establish",
                  synthetic=True, calibrated=False, source_sha256=source_hash())
    Path(out).write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pair", type=Path, help="JSON with name, declared_difference, isolates, scenes [a, b]")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--only", choices=("a", "b"))
    args = parser.parse_args()
    pair = json.loads(args.pair.read_text())
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    summaries = []
    for label, scene in zip("ab", pair["scenes"]):
        if args.only and label != args.only:
            summaries.append(json.loads((out / label / "summary.json").read_text()) if (out / label / "summary.json").exists() else None)
            continue
        _, summary = run_scene(scene, out / label, stop_after=args.stop_after)
        summaries.append(summary)
    if all(summaries):
        result = compare(pair, summaries, out / "comparison.json")
        print(json.dumps({k: result["measured"][k]["b_minus_a"] for k in result["measured"]}, indent=2))


if __name__ == "__main__":
    main()
