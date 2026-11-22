"""Hollow clay and hand contact patches: v0.2 extension of the v0.1 platen baseline.

Geometry: an annular (hollow) cylinder so that wall thickness exists. Hands: rigid
contact patches on the inner or outer wall, each an angular sector times a height
band, moving on a declared motion law with explicit compliance. Contact is a
linearised unilateral constraint on the radial coordinate of the quadratic surface
nodes inside the patch, enforced through the same saddle-point solver as the platen.

Declared laws (illustrative, not calibrated against measured clay):
- hand reference radius r_ref(t) follows the schedule, plus an optional sinusoidal
  off-axis disturbance;
- actual contact radius for a step is r_ref(t) + compliance * F_prev, where F_prev is
  the total radial contact force the patch carried on the previous accepted step
  (explicit, first-order compliance; a stiffer "supported" wrist has lower compliance);
- tangential Navier slip acts on contacting wall facets, removing only the normal
  component of the drag (the wheel still carries the clay under a light hold).

Collocation at nodes is a conservative approximation; mid-edge curvature can
overshoot, so the step records a sampled surface gap and refuses large penetration,
exactly as the platen does. Nothing here hard-codes a teaching outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import itertools

import numpy as np
from scipy.sparse import csr_matrix
from skfem import MeshTet, MeshTet2, FacetBasis, BilinearForm, ElementVector, ElementTetP2
from skfem.helpers import dot


# ----------------------------------------------------------------------------- geometry

def annulus_mesh(radial=2, angular=12, vertical=4, inner_radius=0.03, outer_radius=0.045, height=0.08):
    """Hollow cylinder of quadratic tetrahedra; periodic in angle, Freudenthal subdivision.

    Nodes on an (radial+1) x angular x (vertical+1) grid; each hexahedral cell is cut
    into six tetrahedra along its main diagonal, which is conforming across every
    face, including the angular seam.
    """
    if radial < 1 or angular < 6 or vertical < 1 or not 0 < inner_radius < outer_radius or height <= 0:
        raise ValueError("Require radial >= 1, angular >= 6, vertical >= 1, 0 < inner < outer radius, height > 0.")
    rs = np.linspace(inner_radius, outer_radius, radial + 1)
    ths = np.arange(angular) * (2 * np.pi / angular)
    zs = np.linspace(0, height, vertical + 1)
    def node(i, j, k):
        return (i * angular + (j % angular)) * (vertical + 1) + k
    points = np.zeros((3, (radial + 1) * angular * (vertical + 1)))
    for i, r in enumerate(rs):
        for j, th in enumerate(ths):
            for k, z in enumerate(zs):
                points[:, node(i, j, k)] = (r * np.cos(th), r * np.sin(th), z)
    tets = []
    for i in range(radial):
        for j in range(angular):
            for k in range(vertical):
                corner = {(a, b, c): node(i + a, j + b, k + c) for a in (0, 1) for b in (0, 1) for c in (0, 1)}
                for perm in itertools.permutations(range(3)):
                    path = [(0, 0, 0)]
                    for axis in perm:
                        last = list(path[-1]); last[axis] = 1; path.append(tuple(last))
                    tets.append([corner[p] for p in path])
    tets = np.array(tets).T
    # Orient every tetrahedron positively.
    a, b, c, d = (points[:, tets[i]] for i in range(4))
    signed = np.einsum("ij,ij->j", np.cross((b - a).T, (c - a).T).T, d - a)
    flip = signed < 0
    tets[[1, 2], :][:, flip] = tets[[2, 1], :][:, flip]
    t = tets.copy(); t[1, flip], t[2, flip] = tets[2, flip], tets[1, flip]
    mesh = MeshTet2.from_mesh(MeshTet(points, t))
    # Curved walls: a quadratic mid-edge node between two vertices of equal radius is
    # moved onto that circle, so inner and outer surfaces are isoparametric arcs.
    x = mesh.doflocs.copy()
    vertex_r = np.hypot(points[0], points[1])
    for edge, node in enumerate(mesh.dofs.edge_dofs[0]):
        a, b = mesh.edges[:, edge]
        if np.isclose(vertex_r[a], vertex_r[b], rtol=0, atol=1e-12 * outer_radius):
            r_mid = np.hypot(x[0, node], x[1, node])
            if r_mid > 1e-15:
                scale = vertex_r[a] / r_mid
                x[0, node] *= scale; x[1, node] *= scale
    return replace(mesh, doflocs=x)


def wall_nodes(mesh, side, inner_radius, outer_radius, tol=None):
    """Quadratic surface nodes on the inner or outer wall at the initial geometry."""
    r = np.hypot(mesh.doflocs[0], mesh.doflocs[1])
    tol = tol or 1e-6 * outer_radius
    target = inner_radius if side == "inner" else outer_radius
    return np.flatnonzero(np.isclose(r, target, atol=tol))


# ----------------------------------------------------------------------------- hands

@dataclass(frozen=True)
class Hand:
    """One rigid contact patch on a wall.

    side: "inner" (hooked finger, pushes outward) or "outer" (squeeze, pushes inward).
    angle_center / angle_width: sector in radians, in the laboratory frame.
    z_low / z_high: height band in metres.
    compliance: metres per newton of radial contact force (explicit law; 0 = rigidly held).
    disturbance_amplitude / disturbance_hz: optional sinusoidal radial jitter of the reference.
    """
    name: str
    side: str
    angle_center: float
    angle_width: float
    z_low: float
    z_high: float
    compliance: float = 0.0
    disturbance_amplitude: float = 0.0
    disturbance_hz: float = 0.0

    def __post_init__(self):
        if self.side not in ("inner", "outer"):
            raise ValueError("Hand side must be inner or outer.")
        values = [self.angle_center, self.angle_width, self.z_low, self.z_high, self.compliance,
                  self.disturbance_amplitude, self.disturbance_hz]
        if not np.isfinite(values).all() or self.angle_width <= 0 or self.z_high <= self.z_low or self.compliance < 0:
            raise ValueError("Hand sector, band and compliance must be finite and well ordered.")

    def reference_radius(self, r_ref, t):
        return r_ref + self.disturbance_amplitude * np.sin(2 * np.pi * self.disturbance_hz * t)

    def in_patch(self, xyz):
        """Boolean mask of nodes (columns of xyz) inside the sector and band at the current geometry."""
        theta = np.arctan2(xyz[1], xyz[0])
        d = np.angle(np.exp(1j * (theta - self.angle_center)))
        return (np.abs(d) <= self.angle_width / 2) & (xyz[2] >= self.z_low - 1e-12) & (xyz[2] <= self.z_high + 1e-12)


def hand_constraints(mesh, hands, wall_sets, radii, dt, n_dofs, predicted=None, exclude=()):
    """Linearised radial constraints G u <= bound for every hand; rows carry the hand index.

    For an outer squeeze at radius R: n.(x + dt u) <= R  ->  (n.u) <= (R - n.x)/dt.
    For an inner hook at radius R: n.(x + dt u) >= R   -> (-n.u) <= (n.x - R)/dt.
    n is the outward radial unit vector at the node's current position. Patch
    membership uses the predicted end-of-step position (x + dt * previous velocity)
    when given, so material the wheel carries into a stationary hand is constrained.
    A node inside two patches (a shared closed boundary, e.g. theta = 0 between
    adjacent halves) is constrained once, by the first-listed hand, and its force is
    attributed to that hand; duplicate rows would make the saddle-point system singular.
    Each node therefore has at most one constraint row.
    """
    rows, cols, vals, bounds, owner, nodes_used = [], [], [], [], [], []
    row = 0
    excluded = set(int(n) for n in exclude)  # base nodes are prescribed by the wheel; a hand never touches them
    claimed = set()  # declared attribution: a node on a shared patch boundary belongs to the first-listed hand
    for h, hand in enumerate(hands):
        candidates = np.array([n for n in wall_sets[hand.side] if int(n) not in excluded and int(n) not in claimed], dtype=int)
        xyz = mesh.doflocs[:, candidates]
        mask = hand.in_patch(predicted[:, candidates] if predicted is not None else xyz)
        for node, x, y in zip(candidates[mask], xyz[0][mask], xyz[1][mask]):
            r = np.hypot(x, y)
            if r < 1e-12:
                continue
            nx, ny = x / r, y / r
            sign = 1.0 if hand.side == "outer" else -1.0
            rows += [row, row]; cols += [3 * node, 3 * node + 1]; vals += [sign * nx, sign * ny]
            bounds.append(sign * (radii[h] - r) / dt)
            owner.append(h); nodes_used.append(int(node)); claimed.add(int(node))
            row += 1
    G = csr_matrix((vals, (rows, cols)), shape=(row, n_dofs))
    return G, np.array(bounds), np.array(owner, dtype=int), np.array(nodes_used, dtype=int)


@BilinearForm
def tangential_slip(u, v, w):
    """Navier slip on a wall facet: drag on the tangential velocity only."""
    n = w.n
    return w.beta * (dot(u, v) - dot(u, n) * dot(v, n))


def contacting_wall_facets(mesh, boundary_facets, active_nodes, minimum_fraction=1.0):
    """Boundary facets whose quadratic nodes are in the given set.

    minimum_fraction 1.0 requires all six nodes (used to bind wall facets by topology).
    For slip assembly the solver uses a declared majority rule (0.5): a facet is treated
    as contacting when at least half of its nodes are in active contact, because the
    free rim nodes of a pressed wall release first and the all-six rule then leaves no
    facet at all (the rule is declared, not derived).
    """
    active = set(int(n) for n in active_nodes)
    get = mesh.dofs.get_facet_dofs
    out = []
    for f in boundary_facets:
        dofs = get(np.array([f])).flatten().tolist()
        if sum(1 for d in dofs if d in active) >= minimum_fraction * len(dofs):
            out.append(f)
    return np.array(out, dtype=int)


def slip_matrix(mesh, facets, beta, shape):
    if len(facets) == 0 or beta == 0:
        return csr_matrix(shape)
    fb = FacetBasis(mesh, ElementVector(ElementTetP2()), facets=np.asarray(facets), intorder=4)
    return tangential_slip.assemble(fb, beta=beta)


# ----------------------------------------------------------------------------- measurements

def wall_profile(mesh, inner_nodes, outer_nodes, bands=4, sectors=12):
    """Wall thickness and ring centroid offset per height band and angular sector.

    Thickness in a (band, sector) cell is the mean outer radius minus the mean inner
    radius of the nodes falling in it; asymmetry per band is the spread of thickness
    across sectors; off-axis offset per band is the centroid of the outer ring.
    """
    x = mesh.doflocs
    z_low, z_high = float(x[2].min()), float(x[2].max())
    edges = np.linspace(z_low, z_high, bands + 1)
    out = {"bands": [], "z_edges": edges.tolist(), "sectors": sectors}
    for b in range(bands):
        lo, hi = edges[b], edges[b + 1]
        band = {"z_low": float(lo), "z_high": float(hi)}
        rings = {}
        for name, nodes in (("inner", inner_nodes), ("outer", outer_nodes)):
            p = x[:, nodes]
            sel = (p[2] >= lo - 1e-12) & (p[2] <= hi + 1e-12)
            p = p[:, sel]
            r = np.hypot(p[0], p[1]); th = np.arctan2(p[1], p[0])
            sector = ((th + np.pi) / (2 * np.pi) * sectors).astype(int) % sectors
            means = np.full(sectors, np.nan)
            for s in range(sectors):
                if np.any(sector == s):
                    means[s] = r[sector == s].mean()
            rings[name] = means
            if name == "outer" and p.shape[1]:
                band["outer_centroid_offset_m"] = float(np.hypot(p[0].mean(), p[1].mean()))
                band["outer_radius_mean_m"] = float(r.mean())
        thickness = rings["outer"] - rings["inner"]
        valid = np.isfinite(thickness)
        band["thickness_by_sector_m"] = [None if not v else float(t) for t, v in zip(thickness, valid)]
        if valid.any():
            band["thickness_mean_m"] = float(thickness[valid].mean())
            band["thickness_min_m"] = float(thickness[valid].min())
            band["thickness_max_m"] = float(thickness[valid].max())
            band["thickness_asymmetry_m"] = float(thickness[valid].max() - thickness[valid].min())
        out["bands"].append(band)
    return out
