"""Render a paired teaching comparison: shapes, hand forces, wall thickness, asymmetry, offset.

Reads the two scene directories written by porcelain.pair (episode.npz each) and the
pair's comparison.json. Draws only recorded values; it never advances physics.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter

from .report import setup_3d, surface, surface_colors


def load(directory):
    with np.load(Path(directory) / "episode.npz", allow_pickle=False) as data:
        return dict(positions=data["positions"] * 1000, triangles=data["surface_triangles"], times=data["times"],
                    records=json.loads(str(data["records"])), metadata=json.loads(str(data["metadata"])))


def series(records, key, index=None):
    out = []
    for r in records:
        v = r.get(key)
        out.append(np.nan if v is None else (v[index] if index is not None else v))
    return np.array(out, dtype=float)


def render(pair_dir, animate=False):
    pair_dir = Path(pair_dir)
    scenes = {label: load(pair_dir / label) for label in ("a", "b") if (pair_dir / label / "episode.npz").exists()}
    if not scenes:
        raise ValueError("No scene episodes found.")
    comparison = json.loads((pair_dir / "comparison.json").read_text()) if (pair_dir / "comparison.json").exists() else None
    colors = {"a": "#227d86", "b": "#aa6226"}
    limit = max(50., max(float(abs(s["positions"][:, :2]).max()) for s in scenes.values()) * 1.15)
    height = max(85., max(float(s["positions"][:, 2].max()) for s in scenes.values()) * 1.05)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig = plt.figure(figsize=(15, 10), layout="constrained", facecolor="#f8fafb")
    fig.get_layout_engine().set(rect=(0, 0, .96, 1))
    grid = fig.add_gridspec(3, 3, height_ratios=(1.2, .7, .7))
    for column, label in enumerate(("a", "b")):
        if label not in scenes:
            continue
        s = scenes[label]
        ax = fig.add_subplot(grid[0, column], projection="3d")
        setup_3d(ax, limit, height)
        surface(ax, s["positions"][-1], s["triangles"])
        name = s["metadata"]["config"]["name"]
        ax.set_title(f"{label}: {name}\nfinal shape at {s['times'][-1]:.3f} s", fontsize=11, loc="left")
    ax = fig.add_subplot(grid[0, 2], projection="3d")
    setup_3d(ax, limit, height)
    first = next(iter(scenes.values()))
    surface(ax, first["positions"][0], first["triangles"])
    ax.set_title("shared initial hollow wall", fontsize=11, loc="left")

    panels = [("hand_radial_force_n", "Hand radial force (N, compressive negative)", True),
              ("wall_thickness_mean_m", "Mean wall thickness (mm)", False),
              ("wall_thickness_asymmetry_max_m", "Thickness asymmetry, max over bands (mm)", False),
              ("outer_centroid_offset_max_m", "Outer ring centroid offset, max (mm)", False),
              ("volume_relative_drift", "Volume drift (%)", False),
              ("bulk_dissipation", "Bulk dissipation rate (W)", False)]
    for i, (key, title, per_hand) in enumerate(panels):
        ax = fig.add_subplot(grid[1 + i // 3, i % 3])
        for label, s in scenes.items():
            t = s["times"][1:]
            if per_hand:
                names = s["records"][0].get("hand_names", [])
                for h, hand in enumerate(names):
                    ax.plot(t, series(s["records"], key, h), color=colors[label], lw=1.8, ls="-" if h == 0 else "--", label=f"{label}: {hand}")
            else:
                scale = 1000 if key.endswith("_m") else (100 if key == "volume_relative_drift" else 1)
                ax.plot(t, scale * series(s["records"], key), color=colors[label], lw=1.8, label=label)
        ax.set(title=title, xlabel="Time (s)")
        ax.grid(axis="y", alpha=.2)
        ax.legend(frameon=False, fontsize=8)
    declared = comparison["declared_difference"] if comparison else "comparison.json not yet written"
    fig.suptitle(f"Porcelain · paired teaching comparison '{pair_dir.name}' (v0.2, synthetic, uncalibrated)\n{declared}",
                 fontsize=13, x=.03, ha="left")
    fig.savefig(pair_dir / "pair_summary.png", dpi=150)
    plt.close(fig)
    if animate:
        for label, s in scenes.items():
            fig = plt.figure(figsize=(6, 5.6), layout="constrained")
            ax = fig.add_subplot(projection="3d")
            setup_3d(ax, limit, height)
            shape = surface(ax, s["positions"][0], s["triangles"])
            title = ax.set_title("")
            frames = np.unique(np.linspace(0, len(s["times"]) - 1, min(len(s["times"]), 41)).astype(int))
            def update(i, s=s, shape=shape, title=title, label=label):
                shape.set_verts(s["positions"][i].T[s["triangles"]])
                shape.set_facecolors(surface_colors(s["positions"][i], s["triangles"]))
                title.set_text(f"scene {label} · t = {s['times'][i]:.3f} s\nsynthetic; playback slowed")
                return shape, title
            FuncAnimation(fig, update, frames=frames, interval=110, blit=False).save(pair_dir / f"scene_{label}.gif", writer=PillowWriter(fps=9), dpi=90)
            plt.close(fig)
    print(pair_dir / "pair_summary.png")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pair_dir", type=Path)
    p.add_argument("--animate", action="store_true")
    args = p.parse_args()
    render(args.pair_dir, args.animate)


if __name__ == "__main__":
    main()
