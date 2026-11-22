"""Reproducible numerical checks, including manufactured solutions and refinement."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time

import numpy as np
from skfem import MeshTet, MeshTet2

from .core import (Material, Solver, cylinder_mesh, bases, mass, viscous,
                   divergence, body_force, mixed_solve, strain_rate)
from .run import run, source_hash


def cube(n):
    q = np.linspace(0, 1, n+1)
    return MeshTet2.from_mesh(MeshTet.init_tensor(q, q, q))


def solve_manufactured(n, kind="polynomial", transient=False):
    mesh = cube(n)
    vb, pb = bases(mesh, order=4)
    x, y, z = np.asarray(vb.global_coordinates())
    if kind == "polynomial":
        exact = np.array([x*x, -2*x*y, np.zeros_like(x)])
        laplace = np.zeros_like(exact)
        laplace[0] = 2
        pexact = x+y+z
        gradp = np.ones_like(exact)
        xn, yn, zn = vb.doflocs
        trace = np.array([xn*xn, -2*xn*yn, np.zeros_like(xn)])
    else:
        sx, sy, sz = np.sin(np.pi*x)**2, np.sin(np.pi*y)**2, np.sin(np.pi*z)**2
        tx, ty = np.sin(2*np.pi*x), np.sin(2*np.pi*y)
        cx, cy, cz = np.cos(2*np.pi*x), np.cos(2*np.pi*y), np.cos(2*np.pi*z)
        exact = np.array([np.pi*sx*ty*sz, -np.pi*tx*sy*sz, np.zeros_like(x)])
        laplace = np.array([2*np.pi**3*ty*(cx*sz-2*sx*sz+sx*cz),
                            -2*np.pi**3*tx*(cy*sz-2*sy*sz+sy*cz), np.zeros_like(x)])
        pexact = x
        gradp = np.zeros_like(exact)
        gradp[0] = 1
        trace = np.zeros_like(vb.doflocs)
    mu = 1.3
    f = -mu*laplace+gradp
    K = viscous.assemble(vb, eta=mu)
    B = divergence.assemble(vb, pb)
    A = K
    if transient:
        # One material implicit step, u_old=.7*u_exact, rho/dt=17.
        M = mass.assemble(vb)
        A = K+17*M
        f += 17*.3*exact
        old = .7*trace[np.arange(vb.N) % 3, np.arange(vb.N)]
        load = body_force.assemble(vb, force=f)+17*M @ old
    else:
        load = body_force.assemble(vb, force=f)
    fixed = vb.get_dofs().all()
    prescribed = trace[fixed % 3, fixed]
    u, p, reaction, residual, _ = mixed_solve(A, B, load, fixed, prescribed, pressure_gauge=0)
    error = np.asarray(vb.interpolate(u))-exact
    p_error = np.asarray(pb.interpolate(p))-pexact
    p_error -= np.sum(p_error*pb.dx)/np.sum(pb.dx)
    return dict(mesh_subdivisions=n, velocity_l2=float(np.sqrt(np.sum(np.sum(error**2, axis=0)*vb.dx))),
                pressure_l2_mod_constant=float(np.sqrt(np.sum(p_error**2*pb.dx))),
                linear_residual=float(residual), velocity_dofs=int(vb.N))


def constitutive_check():
    rates = np.geomspace(1e-5, 1e3, 100)
    errors = []
    for exponent in (1., .6):
        mat = Material(exponent=exponent)
        delta = rates*1e-4
        fd = (mat.viscosity(rates+delta)-mat.viscosity(rates-delta))/(2*delta)
        errors.append(float(np.max(abs(fd-mat.viscosity_derivative(rates))/np.maximum(1., abs(fd)))))
        assert np.all(mat.viscosity(rates) > 0)
        assert np.all(mat.viscosity(rates)+rates*mat.viscosity_derivative(rates) > 0)
    assert max(errors) < 2e-6
    m = Material()
    high_rate = 100.
    stress_error = abs(m.viscosity(np.array(high_rate))*high_rate-(m.consistency*high_rate+m.yield_stress))
    assert stress_error < 1e-9
    return dict(max_viscosity_derivative_relative_error=max(errors), high_rate_Bingham_stress_error=stress_error)


def manufactured_check():
    cases = [solve_manufactured(2), solve_manufactured(2, transient=True)]
    assert max(c["velocity_l2"] for c in cases) < 1e-9
    assert max(c["pressure_l2_mod_constant"] for c in cases) < 1e-8
    return dict(steady=cases[0], material_inertia_step=cases[1])


def spatial_check():
    cases = [solve_manufactured(n, "trigonometric") for n in (2, 3, 4)]
    errors = [c["velocity_l2"] for c in cases]
    assert errors[2] < errors[1] < errors[0]
    order = np.log(errors[-2]/errors[-1])/np.log(4/3)
    assert order > 1.5
    return dict(cases=cases, observed_velocity_order_last_pair=float(order),
                note="Manufactured smooth Stokes field on fixed cubes; not a claim of converged contact forces.")


def hydrostatic_check():
    mesh = cube(2)
    vb, pb = bases(mesh)
    x = np.asarray(vb.global_coordinates())
    f = np.zeros_like(x)
    rho, g = 2., 3.
    f[2] = -rho*g
    nodes = mesh.doflocs
    fixed = []
    for i in range(nodes.shape[1]):
        if abs(nodes[2, i]) < 1e-12:
            fixed.extend(3*i+np.arange(3))
        else:
            if abs(nodes[0, i]) < 1e-12 or abs(nodes[0, i]-1) < 1e-12:
                fixed.append(3*i)
            if abs(nodes[1, i]) < 1e-12 or abs(nodes[1, i]-1) < 1e-12:
                fixed.append(3*i+1)
    fixed = np.unique(fixed)
    M = mass.assemble(vb)
    K = viscous.assemble(vb, eta=5.)
    B = divergence.assemble(vb, pb)
    load = body_force.assemble(vb, force=f)
    u, p, reaction, residual, _ = mixed_solve(M+K, B, load, fixed, np.zeros(len(fixed)))
    error = np.asarray(pb.interpolate(p))-rho*g*(1-x[2])
    speed = float(np.max(abs(u)))
    pressure_error = float(np.max(abs(error)))
    assert speed < 1e-10 and pressure_error < 1e-9
    assert abs(load[2::3].sum()+rho*g) < 1e-12
    assert abs(reaction[2::3].sum()-rho*g) < 1e-10
    return dict(max_velocity=speed, max_pressure_error=pressure_error,
                weight=float(-load[2::3].sum()), support_force=float(reaction[2::3].sum()))


def contact_check():
    s = Solver(cylinder_mesh(2, 2))
    compression = s.step(.0025, omega=.5, platen_height=.05995)
    release = s.step(.0025, omega=.5, platen_height=.08)
    assert compression["hand_force_z"] < 0 and compression["active_contact_nodes"] > 0
    assert compression["min_contact_multiplier"] >= -1e-8
    assert compression["sampled_min_contact_gap"] > -1e-10
    assert release["hand_force_z"] == 0 and release["active_contact_nodes"] == 0
    for row in (compression, release):
        assert row["bulk_dissipation"] >= 0 and row["slip_dissipation"] >= 0
        assert abs(row["frozen_geometry_power_residual"]) < 1e-7
        assert row["nonlinear_residual"] < 1e-6
    # Exercise the shear-thinning branch in an assembled mechanics solve.
    hb = Solver(cylinder_mesh(2, 2), Material(exponent=.6))
    row = hb.step(.0025, omega=.5, platen_height=.05995)
    assert row["nonlinear_residual"] < 1e-6 and row["bulk_dissipation"] > 0
    return dict(compression=compression, release=release, herschel_bulkley=row)


def contact_temporal_check():
    states, cases = [], []
    for dt in (.005, .0025, .00125, .000625):
        solver = Solver(cylinder_mesh(2, 2))
        for _ in range(round(.01/dt)):
            row = solver.step(dt, omega=1., platen_height=.06-.02*(solver.t+dt))
        states.append(solver.mesh.doflocs.copy())
        cases.append(dict(dt=dt, volume_relative_drift=row["volume_relative_drift"],
                          hand_force_z=row["hand_force_z"], radius_max=row["radius_max"]))
    differences = [float(np.linalg.norm(states[i]-states[i+1])) for i in range(len(states)-1)]
    ratios = [differences[i]/differences[i+1] for i in range(len(differences)-1)]
    drifts = [abs(c["volume_relative_drift"]) for c in cases]
    assert all(b < a for a, b in zip(drifts, drifts[1:]))
    assert ratios[-1] > 1
    return dict(cases=cases, displacement_difference_ratios=ratios,
                note="Abruptly applied drive: first coarse difference ratio is about 1; the next improves. First-order state convergence is NOT established for this contact transient. Volume drift decreases on every refinement.")


def temporal_check():
    # Smooth exact moving-domain solution: u=(sin(t)*y, 0, 0), p=0,
    # f=(cos(t)*y, 0, 0), rho=1. Material x=X+(1-cos(t))*Y.
    # This independently checks the expected order without a startup jump.
    cases = []
    for dt in (.05, .025, .0125):
        mesh = cube(2)
        initial = mesh.doflocs.copy()
        old = np.zeros(3*mesh.doflocs.shape[1])
        t = 0.
        for _ in range(round(.2/dt)):
            vb, pb = bases(mesh)
            M = mass.assemble(vb)
            K = viscous.assemble(vb, eta=.1)
            B = divergence.assemble(vb, pb)
            f = np.zeros(vb.global_coordinates().shape)
            f[0] = np.cos(t+dt)*np.asarray(vb.global_coordinates())[1]
            fixed = vb.get_dofs().all()
            trace = np.zeros(vb.N)
            trace[::3] = np.sin(t+dt)*mesh.doflocs[1]
            u, p, reaction, residual, _ = mixed_solve(
                M/dt+K, B, M @ old/dt+body_force.assemble(vb, force=f),
                fixed, trace[fixed], pressure_gauge=0)
            mesh = replace(mesh, doflocs=mesh.doflocs+dt*u.reshape(-1, 3).T)
            old, t = u, t+dt
        exact = initial.copy()
        exact[0] += (1-np.cos(t))*initial[1]
        error = float(np.linalg.norm(mesh.doflocs-exact)/np.sqrt(mesh.doflocs.shape[1]))
        cases.append(dict(dt=dt, rms_material_position_error=error))
    ratios = [a["rms_material_position_error"]/b["rms_material_position_error"] for a, b in zip(cases, cases[1:])]
    assert all(1.8 < ratio < 2.2 for ratio in ratios)
    return dict(cases=cases, error_ratios=ratios, observed_orders=[float(np.log2(r)) for r in ratios],
                exact_solution="u=(sin(t)*y,0,0), x=X+(1-cos(t))*Y, p=0, rho=1, mu=.1",
                scope="Smooth prescribed-boundary moving-domain benchmark; contact convergence is reported separately.")


def resume_check():
    config = dict(name="Checkpoint determinism", synthetic=True,
                  geometry=dict(radial=2, vertical=2, radius=.04, height=.06),
                  material=dict(yield_stress=300., yield_time=2.), gravity=9.81,
                  slip_beta=2000., dt=.0025,
                  schedule=[dict(time=0., wheel_omega=.5, platen_height=.06),
                            dict(time=.01, wheel_omega=.5, platen_height=.0598)])
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        run(config, base/"continuous", progress=False)
        run(config, base/"restart", stop_after=2, progress=False)
        run(config, base/"restart", resume=True, progress=False)
        with np.load(base/"continuous"/"episode.npz", allow_pickle=False) as a, np.load(base/"restart"/"episode.npz", allow_pickle=False) as b:
            errors = {k: float(np.max(abs(a[k]-b[k]))) for k in ("positions", "velocities", "pressures", "times")}
            assert max(errors.values()) == 0
            assert str(a["records"]) == str(b["records"])
    return dict(maximum_absolute_differences=errors, diagnostics_identical=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results/verification.json"))
    args = parser.parse_args()
    checks = [("constitutive_tangent_and_dissipation", constitutive_check),
              ("manufactured_steady_and_inertial_solutions", manufactured_check),
              ("manufactured_spatial_refinement", spatial_check),
              ("hydrostatic_gravity_and_support", hydrostatic_check),
              ("contact_release_power_and_shear_thinning", contact_check),
              ("moving_mesh_time_refinement", temporal_check),
              ("abrupt_contact_refinement_limits", contact_temporal_check),
              ("checkpoint_resume_determinism", resume_check)]
    results = []
    for name, function in checks:
        started = time.monotonic()
        try:
            detail = function()
            result = dict(name=name, passed=True, detail=detail)
        except Exception as error:
            import traceback
            result = dict(name=name, passed=False, error=str(error), traceback=traceback.format_exc())
        result["wall_seconds"] = time.monotonic()-started
        results.append(result)
        print(f"{name}: {'PASS' if result['passed'] else 'FAIL'} ({result['wall_seconds']:.1f}s)", flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(dict(all_passed=all(r["passed"] for r in results), solver_sha256=source_hash(), checks=results), indent=2)+"\n")
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
