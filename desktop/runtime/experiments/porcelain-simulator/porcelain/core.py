"""SI units; quadratic isoparametric tetrahedra, P2 velocity / P1 pressure.

Stress = -p I + 2 eta D.  All geometric nodes move with the computed velocity:
inertial acceleration is a material time difference, without an ALE advection
term.  This is a first-order method on the geometry at the beginning of a step.
"""
from dataclasses import dataclass, replace
from numbers import Real
import warnings

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.sparse import bmat, csr_matrix
from scipy.sparse.linalg import spsolve, MatrixRankWarning
from skfem import (MeshTet, MeshTet2, Basis, FacetBasis, BilinearForm,
                   LinearForm, ElementVector, ElementTetP2, ElementTetP1)
from skfem.helpers import dot, ddot, sym_grad, div

from . import hands as _hands


@dataclass(frozen=True)
class Material:
    density: float = 1800.0
    consistency: float = 80.0
    exponent: float = 1.0
    yield_stress: float = 1200.0
    yield_time: float = 10.0
    rate_floor: float = 0.001

    def __post_init__(self):
        values = np.array(list(self.__dict__.values()))
        if not np.all(np.isfinite(values)) or self.density <= 0 or self.consistency <= 0:
            raise ValueError("Material coefficients must be finite; density and consistency positive.")
        if not (0 < self.exponent <= 1) or self.yield_stress < 0 or self.yield_time <= 0 or self.rate_floor <= 0:
            raise ValueError("Require 0 < n <= 1, tau_y >= 0, m > 0, rate_floor > 0.")

    def viscosity(self, rate):
        rate = np.asarray(rate)
        ratio = np.full_like(rate, self.yield_time, dtype=float)
        np.divide(-np.expm1(-self.yield_time * rate), rate, out=ratio, where=rate > 1e-14)
        return (self.consistency * (rate * rate + self.rate_floor**2)**((self.exponent - 1) / 2)
                + self.yield_stress * ratio)

    def viscosity_derivative(self, rate):
        r = np.asarray(rate)
        a = self.yield_time*r
        derivative = np.empty_like(r)
        small = a < 1e-4
        derivative[small] = self.yield_time**2*(-.5+a[small]/3-a[small]**2/8+a[small]**3/30)
        derivative[~small] = (a[~small]*np.exp(-a[~small])+np.expm1(-a[~small]))/r[~small]**2
        return (self.yield_stress*derivative + self.consistency*(self.exponent-1)*r
                *(r*r+self.rate_floor**2)**((self.exponent-3)/2))

    def potential(self, rate):
        r = np.asarray(rate)
        power = (self.exponent+1)/2
        visc = self.consistency/(self.exponent+1)*((r*r+self.rate_floor**2)**power-self.rate_floor**(self.exponent+1))
        return visc+self.yield_stress*(r+np.expm1(-self.yield_time*r)/self.yield_time)


def tetra_quadrature(order=3):
    """Positive-weight Duffy quadrature, integrates the reference tetrahedron."""
    q, w = leggauss(order)
    a, b, c = np.meshgrid((q + 1)/2, (q + 1)/2, (q + 1)/2, indexing="ij")
    wa, wb, wc = np.meshgrid(w/2, w/2, w/2, indexing="ij")
    return (np.array([a, (1-a)*b, (1-a)*(1-b)*c]).reshape(3, -1),
            (wa*wb*wc*(1-a)**2*(1-b)).ravel())


def cylinder_mesh(radial=3, vertical=3, radius=0.04, height=0.06):
    if radial < 2 or vertical < 1 or radius <= 0 or height <= 0:
        raise ValueError("Require radial >= 2, vertical >= 1 and positive dimensions.")
    mesh = MeshTet2.from_mesh(MeshTet.init_tensor(
        np.linspace(-1, 1, radial+1), np.linspace(-1, 1, radial+1),
        np.linspace(0, height, vertical+1)))
    x = mesh.doflocs.copy()
    s, t = x[0].copy(), x[1].copy()
    x[0] = radius*s*np.sqrt(1-t*t/2)
    x[1] = radius*t*np.sqrt(1-s*s/2)
    return replace(mesh, doflocs=x)


def bases(mesh, order=3):
    velocity = Basis(mesh, ElementVector(ElementTetP2()), quadrature=tetra_quadrature(order))
    pressure = Basis(mesh, ElementTetP1(), quadrature=velocity.quadrature)
    return velocity, pressure


@BilinearForm
def mass(u, v, w):
    return dot(u, v)


@BilinearForm
def viscous(u, v, w):
    return 2*w.eta*ddot(sym_grad(u), sym_grad(v))


@BilinearForm
def tangent(u, v, w):
    return (2*w.eta*ddot(sym_grad(u), sym_grad(v))
            + w.slope*ddot(w.D, sym_grad(u))*ddot(w.D, sym_grad(v)))


@LinearForm
def stress_force(v, w):
    return 2*w.eta*ddot(w.D, sym_grad(v))


@BilinearForm
def divergence(u, q, w):
    return q*div(u)


@LinearForm
def body_force(v, w):
    return dot(w.force, v)


@BilinearForm
def slip(u, v, w):
    return w.beta*(dot(u, v)-u[2]*v[2])


def mixed_solve(A, B, force, fixed, prescribed, pressure_gauge=None,
                contact_matrix=None, contact_bound=None):
    """Symmetric saddle point solve. Residual at constrained DOFs is reaction."""
    nv, np_ = A.shape[0], B.shape[0]
    nc = 0 if contact_matrix is None else contact_matrix.shape[0]
    if nc:
        G = contact_matrix
        matrix = bmat([[A, -B.T, G.T], [-B, csr_matrix((np_, np_)), csr_matrix((np_, nc))],
                       [G, csr_matrix((nc, np_)), csr_matrix((nc, nc))]], format="csr")
        rhs = np.concatenate((force, np.zeros(np_), contact_bound))
    else:
        matrix = bmat([[A, -B.T], [-B, csr_matrix((np_, np_))]], format="csr")
        rhs = np.concatenate((force, np.zeros(np_)))
    fixed = np.asarray(fixed, dtype=int)
    prescribed = np.asarray(prescribed, dtype=float)
    if pressure_gauge is not None:
        fixed = np.r_[fixed, nv+pressure_gauge]
        prescribed = np.r_[prescribed, 0.0]
    if len(np.unique(fixed)) != len(fixed):
        raise ValueError("Conflicting boundary constraints.")
    free = np.setdiff1d(np.arange(nv+np_+nc), fixed)
    solution = np.zeros(nv+np_+nc)
    solution[fixed] = prescribed
    reduced_rhs = rhs[free] - matrix[free][:, fixed] @ prescribed
    with warnings.catch_warnings():
        warnings.simplefilter("error", MatrixRankWarning)
        solution[free] = spsolve(matrix[free][:, free].tocsc(), reduced_rhs)
    if not np.isfinite(solution).all():
        raise RuntimeError("Nonfinite mixed solution.")
    residual = matrix @ solution-rhs
    scale = max(np.linalg.norm(reduced_rhs), 1e-20)
    relative_residual = np.linalg.norm(residual[free])/scale
    if relative_residual > 1e-7:
        raise RuntimeError(f"Linear solve failed residual gate: {relative_residual:g}")
    return solution[:nv], solution[nv:nv+np_], residual[:nv], relative_residual, solution[nv+np_:]


def strain_rate(vbasis, velocity):
    grad = vbasis.interpolate(velocity).grad
    D = (grad+grad.swapaxes(0, 1))/2
    return np.sqrt(2*np.einsum("ij... , ij... -> ...", D, D))


def mesh_quality(mesh, reference_signs, quadrature):
    vb = Basis(mesh, ElementTetP2(), quadrature=quadrature)
    det = vb.mapping.detDF(vb.X)
    signed = det*reference_signs[:, None]
    # Also sample near faces/vertices; integration points alone miss some folds.
    points = np.array([[i, j, k] for i in np.linspace(0, 1, 5)
                       for j in np.linspace(0, 1, 5) for k in np.linspace(0, 1, 5)
                       if i+j+k <= 1+1e-12]).T
    probe = vb.mapping.detDF(points)*reference_signs[:, None]
    if not np.isfinite(signed).all() or not np.isfinite(probe).all():
        raise RuntimeError("Nonfinite sampled mesh Jacobian.")
    minimum = min(float(signed.min()), float(probe.min()))
    if minimum <= 0:
        raise RuntimeError(f"Inverted or degenerate mesh: sampled Jacobian {minimum:g}")
    volume = float(vb.dx.sum())
    if not np.isfinite(volume):
        raise RuntimeError("Nonfinite integrated mesh volume.")
    return volume, minimum


class Solver:
    """Moving clay mesh with a driven base and an optional flat, frictional platen.

    The platen is the first hand-contact primitive: horizontal, rigid, unilateral,
    and nonadhesive. Contact is collocated at quadratic surface nodes. The active
    set checks compressive multipliers. Constraints bound the quadratic surface's
    Bernstein control points, preventing between-node penetration as well. This
    is a conservative discrete contact approximation; it can activate early on
    a curved face. Tangential Navier slip uses fully contacting top facets.
    This does not implement arbitrary curved hands.
    """
    def __init__(self, mesh, material=None, gravity=9.81, slip_beta=2000.,
                 nonlinear_tol=1e-8, max_nonlinear=80, max_volume_drift=0.005,
                 hands=(), inner_radius=None, outer_radius=None):
        if (isinstance(max_volume_drift, bool) or not isinstance(max_volume_drift, Real)
                or not 0 <= max_volume_drift < float("inf")):
            raise ValueError("Maximum volume drift must be a finite nonnegative real scalar.")
        self.mesh = mesh
        # v0.2: hand patches on a hollow wall (see hands.py). Empty tuple keeps v0.1 behaviour.
        self.hands = tuple(hands)
        self.hand_force_previous = np.zeros(len(self.hands))
        self.hand_contact_previous = [None]*len(self.hands)
        self.compliance_relaxation = 0.5  # declared: contact radius moves halfway to r_ref + c*F_prev per step
        self.wall_sets = None
        if self.hands:
            if inner_radius is None or outer_radius is None:
                raise ValueError("Hands need the initial inner and outer wall radii.")
            self.wall_sets = {"inner": _hands.wall_nodes(mesh, "inner", inner_radius, outer_radius),
                              "outer": _hands.wall_nodes(mesh, "outer", inner_radius, outer_radius)}
            # Wall facets by topology: a boundary facet belongs to a wall when all six of its quadratic
            # nodes are wall nodes. (facets_satisfying tests the vertex centroid, which lies inside the
            # chord of a curved wall and would miss the boundary.)
            boundary = mesh.boundary_facets()
            self.wall_facets = {side: _hands.contacting_wall_facets(mesh, boundary, self.wall_sets[side])
                                for side in ("inner", "outer")}
        self.material = material or Material()
        self.gravity = float(gravity)
        self.slip_beta = float(slip_beta)
        self.nonlinear_tol = nonlinear_tol
        self.max_nonlinear = max_nonlinear
        self.max_volume_drift = max_volume_drift
        if not np.isfinite([gravity, slip_beta]).all() or gravity < 0 or slip_beta < 0:
            raise ValueError("Gravity and slip coefficient must be finite and nonnegative.")
        vb, pb = bases(mesh)
        self.u = np.zeros(vb.N)
        self.p = np.zeros(pb.N)
        self.t = 0.0
        self.step_index = 0
        z = mesh.doflocs[2]
        self.bottom = np.flatnonzero(np.isclose(z, z.min(), atol=1e-12))
        self.top = np.flatnonzero(np.isclose(z, z.max(), atol=1e-12))
        self.top_facets = mesh.facets_satisfying(lambda x: np.isclose(x[2], z.max(), atol=1e-12), boundaries_only=True)
        # Quadratic Lagrange values -> quadratic Bernstein control values.
        # At edge ij: b_ij = 2*u_mid - (u_i+u_j)/2.  Bernstein basis >= 0.
        G = csr_matrix((np.ones(len(self.top)), (np.arange(len(self.top)), 3*self.top+2)),
                       shape=(len(self.top), vb.N)).tolil()
        edge_lookup = {int(node): mesh.edges[:, edge] for edge, node in enumerate(mesh.dofs.edge_dofs[0])}
        for row, node in enumerate(self.top):
            if node in edge_lookup:
                G[row, 3*node+2] = 2.
                for vertex in edge_lookup[node]:
                    G[row, 3*vertex+2] = -.5
        self.contact_G = G.tocsr()
        self.initial_signs = np.sign(vb.mapping.detDF(vb.X)[:, 0])
        self.initial_volume, self.initial_min_det = mesh_quality(mesh, self.initial_signs, vb.quadrature)
        self.initial_height = float(z.max()-z.min())

    def step(self, dt, omega=0., platen_height=None, hand_radii=None):
        """Advance one step. hand_radii: reference radius per hand for this step (metres), or None."""
        if not np.isfinite([dt, omega]).all() or dt <= 0:
            raise ValueError("dt must be positive and drive finite.")
        if self.hands and (hand_radii is None or len(hand_radii) != len(self.hands)):
            raise ValueError("One reference radius per hand is required.")
        if platen_height is not None and (not np.isfinite(platen_height) or platen_height <= self.mesh.doflocs[2, self.bottom].max()):
            raise ValueError("Platen must remain above the base.")
        mesh = self.mesh
        vb, pb = bases(mesh)
        mat = self.material
        M = mass.assemble(vb)
        B = divergence.assemble(vb, pb)
        xyz = vb.global_coordinates()
        f = np.zeros(xyz.shape)
        f[2] = -mat.density*self.gravity
        load = body_force.assemble(vb, force=f)
        rhs = mat.density/dt*(M @ self.u)+load
        # Exact base rotation: the prescribed discrete velocity is its chord/dt.
        bcoords = mesh.doflocs[:, self.bottom]
        angle = omega*dt
        rotated = bcoords.copy()
        rotated[0] = np.cos(angle)*bcoords[0]-np.sin(angle)*bcoords[1]
        rotated[1] = np.sin(angle)*bcoords[0]+np.cos(angle)*bcoords[1]
        bvel = (rotated-bcoords)/dt
        base_dofs = (3*self.bottom[:, None]+np.arange(3)).ravel()
        base_values = bvel.T.ravel()
        upper = None if platen_height is None else (platen_height-self.contact_G @ mesh.doflocs.T.ravel())/dt
        active = np.zeros(len(self.top), dtype=bool)
        if upper is not None:
            active = self.contact_G @ self.u > upper
        # Hand patches: declared motion law with explicit compliance from the previous step's force.
        hand_G = csr_matrix((0, vb.N)); hand_bound = np.zeros(0); hand_owner = np.zeros(0, dtype=int); hand_nodes = np.zeros(0, dtype=int)
        hand_contact_radii = []
        if self.hands:
            t_next = self.t+dt
            for h, hand in enumerate(self.hands):
                r_ref = hand.reference_radius(float(hand_radii[h]), t_next)
                target = r_ref+hand.compliance*self.hand_force_previous[h]
                previous = self.hand_contact_previous[h]
                hand_contact_radii.append(target if previous is None or hand.compliance == 0
                                          else previous+self.compliance_relaxation*(target-previous))
            predicted = mesh.doflocs+dt*self.u.reshape(-1, 3).T
            hand_G, hand_bound, hand_owner, hand_nodes = _hands.hand_constraints(
                mesh, self.hands, self.wall_sets, hand_contact_radii, dt, vb.N, predicted=predicted, exclude=self.bottom)
        hand_active = hand_G @ self.u > hand_bound if hand_G.shape[0] else np.zeros(0, dtype=bool)
        iterate = self.u.copy()
        contact_iterations = 0
        total_iterations = 0
        for contact_iterations in range(1, 31):
            fixed = base_dofs
            prescribed = base_values
            Gactive = self.contact_G[active]
            if hand_G.shape[0]:
                Gactive = bmat([[Gactive], [hand_G[hand_active]]], format="csr") if Gactive.shape[0] else hand_G[hand_active]
            n_platen_active = int(active.sum())
            C = csr_matrix(M.shape)
            if hand_G.shape[0] and self.slip_beta:
                for side in ("inner", "outer"):
                    side_mask = np.array([self.hands[o].side == side for o in hand_owner[hand_active]], dtype=bool) if hand_active.any() else np.zeros(0, dtype=bool)
                    nodes_side = hand_nodes[hand_active][side_mask] if hand_active.any() else np.zeros(0, dtype=int)
                    facets = _hands.contacting_wall_facets(mesh, self.wall_facets[side], nodes_side, minimum_fraction=0.5) if len(nodes_side) else np.zeros(0, dtype=int)
                    C = C+_hands.slip_matrix(mesh, facets, self.slip_beta, M.shape)
            if np.any(active) and self.slip_beta:
                top_set = set(self.top[active].tolist())
                facet_nodes = mesh.dofs.get_facet_dofs
                facets = [i for i in self.top_facets
                          if set(facet_nodes(np.array([i])).flatten().tolist()).issubset(top_set)]
                if facets:
                    fb = FacetBasis(mesh, ElementVector(ElementTetP2()), facets=np.asarray(facets), intorder=4)
                    C = C+slip.assemble(fb, beta=self.slip_beta)
            def objective(velocity):
                d = velocity-self.u
                return (mat.density/(2*dt)*float(d @ (M @ d))
                        + float(np.sum(mat.potential(strain_rate(vb, velocity))*vb.dx))
                        + .5*float(velocity @ (C @ velocity))-float(load @ velocity))

            previous_pressure = self.p.copy()
            previous_multipliers = np.zeros(Gactive.shape[0])
            for iteration in range(1, self.max_nonlinear+1):
                grad = vb.interpolate(iterate).grad
                D = (grad+grad.swapaxes(0, 1))/2
                rate_iter = strain_rate(vb, iterate)
                eta = mat.viscosity(rate_iter)
                slope = np.zeros_like(rate_iter)
                np.divide(4*mat.viscosity_derivative(rate_iter), rate_iter, out=slope, where=rate_iter > 1e-14)
                J = tangent.assemble(vb, eta=eta, slope=slope, D=D)
                internal = stress_force.assemble(vb, eta=eta, D=D)
                A = mat.density/dt*M+J+C
                bound_active = None
                if Gactive.shape[0]:
                    parts = []
                    if upper is not None:
                        parts.append(upper[active])
                    if hand_G.shape[0]:
                        parts.append(hand_bound[hand_active])
                    bound_active = np.concatenate(parts) if parts else None
                u, p, reaction, lin_residual, multipliers = mixed_solve(
                    A, B, rhs+J @ iterate-internal, fixed, prescribed,
                    contact_matrix=Gactive if Gactive.shape[0] else None, contact_bound=bound_active)
                # First iterate enforces the current constraints. Subsequent
                # steps stay feasible and decrease the convex increment potential.
                full_delta = u-iterate
                full_change = np.sqrt(max(float(full_delta @ (M @ full_delta)), 0.0))/max(np.sqrt(max(float(u @ (M @ u)), 0.0)), 1e-12)
                if iteration > 1 and full_change > 1e-5:
                    old_energy = objective(iterate)
                    alpha = 1.
                    candidate = u
                    while objective(candidate) > old_energy+1e-12*max(abs(old_energy), 1e-12):
                        alpha *= .5
                        if alpha < 2**-20:
                            raise RuntimeError("Constitutive Newton line search failed.")
                        candidate = iterate+alpha*(u-iterate)
                    u = candidate
                    p = previous_pressure+alpha*(p-previous_pressure)
                    multipliers = previous_multipliers+alpha*(multipliers-previous_multipliers)
                delta = u-iterate
                relative_change = np.sqrt(max(float(delta @ (M @ delta)), 0.0))/max(np.sqrt(max(float(u @ (M @ u)), 0.0)), 1e-12)
                iterate = u
                previous_pressure, previous_multipliers = p, multipliers
                if relative_change < self.nonlinear_tol:
                    grad_check = vb.interpolate(u).grad
                    Dcheck = (grad_check+grad_check.swapaxes(0, 1))/2
                    internal_check = stress_force.assemble(vb, eta=mat.viscosity(strain_rate(vb, u)), D=Dcheck)
                    check = mat.density/dt*(M @ u)+internal_check+C @ u-B.T @ p-rhs+Gactive.T @ multipliers
                    free_check = np.setdiff1d(np.arange(vb.N), fixed)
                    scale_check = max(np.linalg.norm(rhs), np.linalg.norm(mat.density/dt*(M @ u)+internal_check+C @ u), 1e-12)
                    if np.linalg.norm(check[free_check])/scale_check < 1e-8:
                        break
            else:
                raise RuntimeError(f"Constitutive iteration did not converge ({relative_change:g}); reduce dt/drive or regularization.")
            total_iterations += iteration
            if upper is None and not hand_G.shape[0]:
                break
            force_tol = 1e-9*max(1., np.linalg.norm(multipliers))
            platen_multipliers = multipliers[:n_platen_active]
            hand_multipliers = multipliers[n_platen_active:]
            new_active = active.copy()
            if upper is not None:
                new_active[np.flatnonzero(active)[platen_multipliers < -force_tol]] = False
                new_active[~active & (self.contact_G @ u > upper+1e-9)] = True
            new_hand_active = hand_active.copy()
            if hand_G.shape[0]:
                new_hand_active[np.flatnonzero(hand_active)[hand_multipliers < -force_tol]] = False
                new_hand_active[~hand_active & (hand_G @ u > hand_bound+1e-9)] = True
            if np.array_equal(active, new_active) and np.array_equal(hand_active, new_hand_active):
                break
            active, hand_active = new_active, new_hand_active
        else:
            raise RuntimeError("Contact active set did not converge.")

        # Reassemble at final strain rate to check the actual nonlinear equation.
        eta_final = mat.viscosity(strain_rate(vb, u))
        Kfinal = viscous.assemble(vb, eta=eta_final)
        momentum = (mat.density/dt*M+Kfinal+C) @ u-B.T @ p-rhs+Gactive.T @ multipliers
        free = np.setdiff1d(np.arange(vb.N), fixed)
        scale = max(np.linalg.norm(rhs), np.linalg.norm((mat.density/dt*M+Kfinal+C) @ u), 1e-12)
        nonlinear_residual = np.linalg.norm(momentum[free])/scale
        if nonlinear_residual > 1e-6:
            raise RuntimeError(f"Nonlinear momentum residual {nonlinear_residual:g} exceeds gate.")

        newmesh = replace(mesh, doflocs=mesh.doflocs+dt*u.reshape(-1, 3).T)
        volume, min_det = mesh_quality(newmesh, self.initial_signs, vb.quadrature)
        volume_drift = volume/self.initial_volume-1
        if abs(volume_drift) > self.max_volume_drift:
            raise RuntimeError(f"Volume drift {volume_drift:.3%} exceeds configured limit; refine dt/mesh.")
        rate = strain_rate(vb, u)
        divergence_field = np.einsum("ii...->...", vb.interpolate(u).grad)
        kinetic_old_geometry = 0.5*mat.density*(u @ (M @ u))
        kinetic_before = 0.5*mat.density*(self.u @ (M @ self.u))
        numerical_dissipation = 0.5*mat.density/dt*((u-self.u) @ (M @ (u-self.u)))
        bulk_power = float(u @ (Kfinal @ u))
        slip_power = float(u @ (C @ u))
        boundary_power = float(u[fixed] @ momentum[fixed]-(Gactive @ u) @ multipliers)
        body_power = float(u @ load)
        power_balance = ((kinetic_old_geometry-kinetic_before)/dt+numerical_dissipation
                         +bulk_power+slip_power-boundary_power-body_power)
        platen_multipliers = multipliers[:n_platen_active] if Gactive.shape[0] else multipliers
        force_hand = -float(platen_multipliers.sum())
        hand_forces = np.zeros(len(self.hands))
        if hand_G.shape[0] and hand_active.any():
            owners_active = hand_owner[hand_active]
            for h in range(len(self.hands)):
                hand_forces[h] = -float(multipliers[n_platen_active:][owners_active == h].sum())
        bottom_forces = momentum[base_dofs].reshape(-1, 3)
        torque = float(np.sum(bcoords[0]*bottom_forces[:, 1]-bcoords[1]*bottom_forces[:, 0]))
        newvb, _ = bases(newmesh)
        nu = newvb.interpolate(u)
        nx = newvb.global_coordinates()
        kinetic = float(0.5*mat.density*np.sum(np.sum(nu**2, axis=0)*newvb.dx))
        potential = float(mat.density*self.gravity*np.sum(nx[2]*newvb.dx))
        angular_momentum = float(mat.density*np.sum((nx[0]*nu[1]-nx[1]*nu[0])*newvb.dx))
        # Mid-edge curvature can overshoot nodal contacts; sample the surface too.
        gap_sample = None
        if platen_height is not None:
            fb = FacetBasis(newmesh, ElementVector(ElementTetP2()), facets=self.top_facets, intorder=6)
            gap_sample = float(platen_height-fb.global_coordinates()[2].max())
            if gap_sample < -1e-6*self.initial_height:
                raise RuntimeError(f"Platen penetration between contact nodes: {-gap_sample:g} m.")
        hand_record = {}
        if self.hands:
            newx = newmesh.doflocs
            hand_gaps, hand_tolerances = [], []
            velocity_nodes = u.reshape(-1, 3).T
            hand_entry = []
            for h, hand in enumerate(self.hands):
                nodes_h = self.wall_sets[hand.side]
                constrained = hand_nodes[hand_owner == h]
                sel = constrained
                r_new = np.hypot(newx[0, sel], newx[1, sel])
                # Nodes now inside the patch that were not constrained this step (entered beyond the
                # prediction): their overlap is recorded as unconstrained entry, resolved next step.
                in_now = nodes_h[hand.in_patch(newx[:, nodes_h])]
                entrants = np.setdiff1d(in_now, constrained)
                if entrants.size:
                    r_e = np.hypot(newx[0, entrants], newx[1, entrants])
                    overlap = (r_e.max()-hand_contact_radii[h]) if hand.side == "outer" else (hand_contact_radii[h]-r_e.min())
                    hand_entry.append(float(max(overlap, 0.)))
                else:
                    hand_entry.append(0.)
                # The constraint is linear in u along the radial direction at the start of the
                # step; a node moving tangentially (the wheel) gains radius at second order,
                # dt^2 |u|^2 / (2 r). That chord error is the declared tolerance, not a loosening.
                speed2 = np.sum(velocity_nodes[:2, sel]**2, axis=0) if sel.size else np.zeros(0)
                r_old = np.hypot(mesh.doflocs[0, sel], mesh.doflocs[1, sel]) if sel.size else np.ones(0)
                chord = float((dt*dt*speed2/(2*np.maximum(r_old, 1e-12))).max()) if sel.size else 0.
                tolerance = 1e-6*self.initial_height+1.5*chord
                if r_new.size == 0:
                    gap = 0.  # empty patch this step (no wall node inside the sector/band): no contact, no gate
                elif hand.side == "outer":
                    gap = hand_contact_radii[h]-r_new.max()
                else:
                    gap = r_new.min()-hand_contact_radii[h]
                hand_gaps.append(float(gap)); hand_tolerances.append(float(tolerance))
                if r_new.size and gap < -tolerance:
                    raise RuntimeError(f"Hand '{hand.name}' penetration between contact nodes: {-gap:g} m exceeds chord tolerance {tolerance:g} m.")
            profile = _hands.wall_profile(newmesh, self.wall_sets["inner"], self.wall_sets["outer"])
            hand_record = dict(hand_names=[h.name for h in self.hands],
                               hand_reference_radii=[float(r) for r in hand_radii],
                               hand_contact_radii=[float(r) for r in hand_contact_radii],
                               hand_radial_force_n=[float(f) for f in hand_forces],
                               hand_active_nodes=[int(((hand_owner == h) & hand_active).sum()) for h in range(len(self.hands))],
                               hand_min_gap_m=hand_gaps, hand_gap_tolerance_m=hand_tolerances,
                               hand_unconstrained_entry_m=hand_entry,
                               wall_profile=profile,
                               wall_thickness_mean_m=float(np.nanmean([b.get("thickness_mean_m", np.nan) for b in profile["bands"]])),
                               wall_thickness_asymmetry_max_m=float(np.nanmax([b.get("thickness_asymmetry_m", np.nan) for b in profile["bands"]])),
                               outer_centroid_offset_max_m=float(np.nanmax([b.get("outer_centroid_offset_m", np.nan) for b in profile["bands"]])))
            self.hand_force_previous = hand_forces
            self.hand_contact_previous = list(hand_contact_radii)
        record = dict(step=self.step_index+1, time=self.t+dt, dt=dt, wheel_omega=omega,
                      platen_height=platen_height, volume=volume, volume_relative_drift=volume_drift,
                      **hand_record,
                      mass=mat.density*volume, height=float(newmesh.doflocs[2].max()),
                      radius_max=float(np.hypot(*newmesh.doflocs[:2]).max()),
                      kinetic_energy=kinetic, gravitational_potential=potential,
                      angular_momentum_z=angular_momentum, wheel_torque_z=torque,
                      hand_force_z=force_hand, active_contact_nodes=int(active.sum()),
                      min_contact_multiplier=float(multipliers.min()) if len(multipliers) else None,
                      sampled_min_contact_gap=gap_sample, bulk_dissipation=bulk_power,
                      slip_dissipation=slip_power, body_power=body_power,
                      boundary_power=boundary_power, backward_euler_dissipation=float(numerical_dissipation),
                      frozen_geometry_power_residual=float(power_balance),
                      linear_residual=float(lin_residual), nonlinear_residual=float(nonlinear_residual),
                      weak_divergence_residual=float(np.linalg.norm(B @ u)),
                      rms_pointwise_divergence=float(np.sqrt(np.sum(divergence_field**2*vb.dx)/vb.dx.sum())),
                      max_shear_rate=float(rate.max()), eta_min=float(eta_final.min()),
                      eta_max=float(eta_final.max()), min_sampled_jacobian=min_det,
                      contact_iterations=contact_iterations, nonlinear_iterations=total_iterations)
        self.mesh, self.u, self.p = newmesh, u, p
        self.t += dt
        self.step_index += 1
        return record
