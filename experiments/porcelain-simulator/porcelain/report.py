"""Render recorded solver states; this module never invents or advances physics."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np


def setup_3d(ax, limit, height):
    ax.set(xlim=(-limit, limit), ylim=(-limit, limit), zlim=(0, height),
           xlabel="x (mm)", ylabel="y (mm)", zlabel="z (mm)")
    ax.set_box_aspect((2*limit, 2*limit, height))
    ax.view_init(elev=20, azim=-62)
    ax.tick_params(labelsize=8)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.pane.fill = False
    return ax


def surface(ax, positions, triangles):
    collection = Poly3DCollection(positions.T[triangles], facecolors=surface_colors(positions, triangles),
                                 edgecolors="#637b82", linewidths=.16,
                                 alpha=1.0, shade=False)
    ax.add_collection3d(collection)
    return collection


def surface_colors(positions, triangles):
    # Two-sided lighting: the stored facets have no winding convention.
    polygons = positions.T[triangles]
    normals = np.cross(polygons[:, 1]-polygons[:, 0], polygons[:, 2]-polygons[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-30)
    direction = np.array([.3, -.6, .74])
    direction /= np.linalg.norm(direction)
    light = .6+.4*abs(normals @ direction)
    return np.array(matplotlib.colors.to_rgb("#d5e6ea"))[None, :]*light[:, None]


def render(episode, destination, animate=False):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with np.load(episode, allow_pickle=False) as data:
        positions = data["positions"]*1000
        triangles = data["surface_triangles"]
        times = data["times"]
        records = json.loads(str(data["records"]))
        metadata = json.loads(str(data["metadata"]))
    if not records:
        raise ValueError("No accepted steps to render.")
    limit = max(45., float(abs(positions[:, :2]).max())*1.15)
    height = max(65., float(positions[:, 2].max())*1.05)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig = plt.figure(figsize=(14, 8.5), layout="constrained", facecolor="#f8fafb")
    # Reserve a right margin for projected 3D tick and axis labels.
    fig.get_layout_engine().set(rect=(0, 0, .955, 1))
    grid = fig.add_gridspec(2, 3, height_ratios=(1.25, .8))
    heights = np.array([float(positions[i, 2].max()) for i in range(len(times))])
    contact_steps = [i+1 for i, r in enumerate(records) if r["active_contact_nodes"] > 0]
    compressed = contact_steps[-1] if contact_steps else int(np.argmin(heights))
    for column, (index, title) in enumerate(((0, "Initial material"), (compressed, "End of compression"), (len(times)-1, "After hand release"))):
        ax = fig.add_subplot(grid[0, column], projection="3d")
        setup_3d(ax, limit, height)
        surface(ax, positions[index], triangles)
        ax.set_title(f"{title}\n{times[index]:.3f} s · height {heights[index]:.2f} mm", fontsize=12, loc="left")
    ax = fig.add_subplot(grid[1, 0])
    ax.plot(times, heights, color="#227d86", lw=2, label="Clay top")
    ht = [np.nan if r["platen_height"] is None else 1000*r["platen_height"] for r in records]
    ax.plot(times[1:], ht, color="#aa6226", ls="--", lw=1.5, label="Platen")
    ax.set(title="Prescribed action and resulting shape", xlabel="Time (s)", ylabel="Height (mm)")
    ax.legend(frameon=False)
    ax = fig.add_subplot(grid[1, 1])
    ax.plot(times[1:], [-r["hand_force_z"] for r in records], color="#aa6226", lw=2)
    ax.set(title="Compressive hand reaction", xlabel="Time (s)", ylabel="Force magnitude (N)")
    ax.set_ylim(bottom=0)
    ax = fig.add_subplot(grid[1, 2])
    ax.plot(times[1:], [100*r["volume_relative_drift"] for r in records], color="#4e6397", lw=2)
    ax.axhline(0, color="#bac2ce", lw=.8)
    ax.set(title="Measured numerical volume drift", xlabel="Time (s)", ylabel="Change from initial volume (%)")
    for ax in fig.axes[3:]:
        ax.grid(axis="y", alpha=.2)
    fig.suptitle("Porcelain · three-dimensional mechanics, v0.1\nSynthetic Bingham material · moving P2/P1 mesh · gravity, wheel, contact and release",
                 fontsize=16, x=.035, ha="left")
    fig.savefig(destination/"simulation_summary.png", dpi=160)
    plt.close(fig)
    if animate:
        fig = plt.figure(figsize=(6, 5.6), layout="constrained")
        ax = fig.add_subplot(projection="3d")
        setup_3d(ax, limit, height)
        shape = surface(ax, positions[0], triangles)
        title = ax.set_title("")
        frame_indices = np.unique(np.linspace(0, len(times)-1, min(len(times), 41)).astype(int))
        def update(i):
            shape.set_verts(positions[i].T[triangles])
            shape.set_facecolors(surface_colors(positions[i], triangles))
            title.set_text(f"Computed material surface · t = {times[i]:.3f} s\nSynthetic example; playback slowed down")
            return shape, title
        animation = FuncAnimation(fig, update, frames=frame_indices, interval=110, blit=False)
        animation.save(destination/"simulation.gif", writer=PillowWriter(fps=9), dpi=95)
        plt.close(fig)
    print(destination/"simulation_summary.png")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("episode", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--animate", action="store_true")
    args = p.parse_args()
    render(args.episode, args.out, args.animate)


if __name__ == "__main__":
    main()
