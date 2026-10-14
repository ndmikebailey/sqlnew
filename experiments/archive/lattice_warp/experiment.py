"""Reversible lattice warping, measured codecs, and a local visual experiment.

Run: python experiment.py --open
Requires numpy and matplotlib. No server or network is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import webbrowser
import zlib

import numpy as np


HEADER = struct.Struct("<4sBHI")
MAGIC = b"LAT1"


def shift_map(n: int, a: int, b: int) -> np.ndarray:
    y, x = np.indices((n, n))
    return np.rint(a * np.sin(2 * np.pi * x / n)
                   + b * np.cos(2 * np.pi * y / n)).astype(np.int8)


def warp(data: np.ndarray, shifts: np.ndarray) -> np.ndarray:
    """Move source cell (z,y,x) to ((z-shifts[y,x]) % n,y,x)."""
    z = np.arange(data.shape[0])[:, None, None]
    return np.take_along_axis(data, (z + shifts) % len(data), axis=0)


def unwarp(data: np.ndarray, shifts: np.ndarray) -> np.ndarray:
    z = np.arange(data.shape[0])[:, None, None]
    return np.take_along_axis(data, (z - shifts) % len(data), axis=0)


def encode(data: np.ndarray, shifts: np.ndarray | None = None,
           raw: bool = False) -> bytes:
    n = len(data)
    if raw:
        return HEADER.pack(MAGIC, 0, n, 0) + data.tobytes()
    if shifts is None or not np.any(shifts):
        return HEADER.pack(MAGIC, 1, n, 0) + zlib.compress(data.tobytes(), 9)
    # Store the actual map: decoding never needs the generator or a fitted model.
    # This also avoids relying on identical floating-point sine implementations.
    map_bytes = zlib.compress(shifts.tobytes(), 9)
    payload = zlib.compress(warp(data, shifts).tobytes(), 9)
    return HEADER.pack(MAGIC, 2, n, len(map_bytes)) + map_bytes + payload


def decode(packet: bytes) -> np.ndarray:
    if len(packet) < HEADER.size:
        raise ValueError("Truncated packet")
    magic, mode, n, map_size = HEADER.unpack_from(packet)
    if magic != MAGIC or mode not in (0, 1, 2) or not 1 <= n <= 64:
        raise ValueError("Invalid LAT1 header")
    body = packet[HEADER.size:]
    if mode != 2 and map_size:
        raise ValueError("Unexpected shift map")
    if mode == 0:
        values = body
    else:
        values = zlib.decompress(body[map_size:])
    if len(values) != n ** 3:
        raise ValueError("Wrong number of cells")
    data = np.frombuffer(values, dtype=np.uint8).reshape((n, n, n))
    if mode == 2:
        map_bytes = zlib.decompress(body[:map_size])
        if len(map_bytes) != n ** 2:
            raise ValueError("Wrong shift-map dimensions")
        shifts = np.frombuffer(map_bytes, dtype=np.int8).reshape((n, n))
        return unwarp(data, shifts)
    return data.copy()


def datasets(n: int, seed: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    profile = rng.integers(0, 256, n, dtype=np.uint8)
    z = np.arange(n)[:, None, None]
    patterned = profile[(z - shift_map(n, 5, 3)) % n]
    noisy = patterned.copy()
    mask = rng.random(noisy.shape) < 0.1
    noisy[mask] = rng.integers(0, 256, np.count_nonzero(mask), dtype=np.uint8)
    return {
        "Patterned": patterned,
        "10% noise": noisy,
        "Random": rng.integers(0, 256, patterned.shape, dtype=np.uint8),
    }


def precision_experiment(n: int) -> list[dict]:
    z, y, x = np.indices((n, n, n))
    shifts = shift_map(n, 5, 3)
    positions = np.stack((x, y, z - shifts), axis=-1).reshape((-1, 3))
    rows = []
    for power in range(11):
        scale = 2.0 ** -power
        # Fix the coordinate grid at 1/8 of an original cell width.
        quantized = np.rint(positions * scale * 8).astype(np.int32)
        unique = len(np.unique(quantized, axis=0))
        rows.append({"power": power, "scale": scale,
                     "volume_fraction": scale ** 3,
                     "distinct_positions": unique,
                     "position_collisions": n ** 3 - unique,
                     "coordinate_grid_step": 0.125,
                     "value_bytes": n ** 3,
                     "explicit_float64_coordinate_bytes": n ** 3 * 3 * 8})
    return rows


def run(n: int, seed: int, output: Path, custom: Path | None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    source = datasets(n, seed)
    if custom:
        supplied = np.load(custom, allow_pickle=False)
        if supplied.dtype != np.uint8 or supplied.shape != (n, n, n):
            raise ValueError(f"Input must be a uint8 array of shape {(n, n, n)}")
        source["Your data"] = np.ascontiguousarray(supplied)
    maps = [shift_map(n, a, b) for a in range(9) for b in range(9)]
    records = []
    for title, data in source.items():
        candidates = []
        for index, shifts in enumerate(maps):
            packet = encode(data, shifts)
            restored = decode(packet)
            if not np.array_equal(data, restored):
                raise AssertionError(f"Round trip failed: {title}, map {index}")
            _, mode, _, map_size = HEADER.unpack_from(packet)
            candidates.append({"a": index // 9, "b": index % 9,
                               "bytes": len(packet), "map_bytes": map_size,
                               "header_bytes": HEADER.size,
                               "payload_bytes": len(packet) - map_size - HEADER.size,
                               "exact": True, "mode": mode})
        best_index = min(range(len(candidates)), key=lambda i: candidates[i]["bytes"])
        best = candidates[best_index]
        stem = title.lower().replace("%", "pct").replace(" ", "_")
        raw = encode(data, raw=True)
        baseline = encode(data)
        best_packet = encode(data, maps[best_index])
        for suffix, packet in (("raw", raw), ("zlib", baseline), ("warped", best_packet)):
            path = output / f"{stem}.{suffix}.lat"
            path.write_bytes(packet)
            if not np.array_equal(decode(path.read_bytes()), data):
                raise AssertionError(f"Independent file decode failed: {path}")
        # Test identities as well as values; repeated values cannot hide a swap.
        ids = np.arange(n ** 3).reshape((n, n, n))
        for shifts in maps:
            restored_ids = unwarp(warp(ids, shifts), shifts)
            if not np.array_equal(ids, restored_ids):
                raise AssertionError("A cell identity was lost")
        repeated = [len(best_packet)]
        for _ in range(5):
            best_packet = zlib.compress(best_packet, 9)
            repeated.append(len(best_packet))
        record = {"name": title, "stem": stem, "raw_value_bytes": data.nbytes,
                  "raw_packet_bytes": len(raw), "baseline_bytes": len(baseline),
                  "best_index": best_index, "best": best,
                  "raw_to_best_ratio": len(raw) / best["bytes"],
                  "baseline_to_best_ratio": len(baseline) / best["bytes"],
                  "sha256": hashlib.sha256(data.tobytes()).hexdigest(),
                  "cells_recovered": data.size,
                  "original_grid_edges_recoverable": 3 * n * n * (n - 1),
                  "candidates": candidates, "repeated_zlib_bytes": repeated,
                  "values": data.ravel().tolist()}
        records.append(record)
        print(f"{title}: raw packet {len(raw):,} B; zlib {len(baseline):,} B; "
              f"best warp {best['bytes']:,} B (A={best['a']}, B={best['b']}); "
              f"{len(candidates)} exact round trips passed", flush=True)
    report = {"size": n, "seed": seed, "header_bytes": HEADER.size,
              "search": "All 81 integer amplitude pairs A,B in 0..8, including identity",
              "axis_order": "z,y,x; x varies fastest in the stored bytes",
              "relationship_definition": "Original cell identities and six-neighbor grid adjacency; Euclidean distances change",
              "map_definition": "round(A*sin(2*pi*x/n) + B*cos(2*pi*y/n)); actual int8 map stored in each warped packet",
              "storage_warp": "(z,y,x) -> ((z-map[y,x]) mod n,y,x)",
              "visual_warp": "(x,y,z) -> (x,y,z-map[y,x]); unwrapped to keep original connections continuous",
              "datasets": records, "precision": precision_experiment(n),
              "maps": [m.ravel().tolist() for m in maps]}
    compact = {**report, "datasets": [{k: v for k, v in r.items() if k != "values"}
                                     for r in records]}
    compact.pop("maps")
    (output / "results.json").write_text(json.dumps(compact, indent=2), encoding="utf-8")
    template = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8")
    page = template.replace("__EXPERIMENT_DATA__", json.dumps(report, separators=(",", ":")))
    (output / "lattice.html").write_text(page, encoding="utf-8")
    write_report(report, output)
    return report


def write_report(report: dict, output: Path) -> None:
    n = report["size"]
    lines = ["# Lattice warp experiment", "",
             f"Seed: {report['seed']}. Each dataset is {n} x {n} x {n} uint8 cells.", "",
             "| Dataset | Raw packet | Ordinary zlib | Best warp + zlib | Best A, B | Exact recovery |",
             "|---|---:|---:|---:|---|---|"]
    for r in report["datasets"]:
        b = r["best"]
        lines.append(f"| {r['name']} | {r['raw_packet_bytes']:,} B | {r['baseline_bytes']:,} B | "
                     f"{b['bytes']:,} B | {b['a']}, {b['b']} | {r['cells_recovered']:,} cells |")
    lines += ["", "Every file size includes its header, dimensions, and any compressed shift map.",
              "The generic Python decoder is shared, just as a zlib decoder is shared; its source size is not charged per file.", "",
              "## What was actually tested", "",
              "The synthetic patterned data contains curved, periodic bands by construction. A second dataset replaces about 10% of cells with random bytes. A third consists of independent pseudorandom bytes. The encoder receives only each completed array; it does not receive the generating profile or its bend amplitudes.", "",
              "The search evaluates all 81 amplitude pairs from 0 through 8 and chooses the smallest complete packet. This is a finite search over a chosen warp family, not a globally optimal compressor. Results on the constructed example demonstrate this mechanism; they do not establish performance on arbitrary real data.", "",
              "A warp shifts each vertical column by an integer number of cells. The stored array wraps at its top and bottom, making the operation a permutation. The picture is unwrapped so the deformation can be seen without a discontinuous seam. The shift map is stored explicitly, compressed, so the decoder needs neither the original data nor knowledge of how the map was generated.", "",
              "All 81 candidate packets for every dataset were decoded and compared byte for byte. Unique cell IDs were separately warped and inverted for all maps; repeated values cannot hide swapped cells. Best, baseline, and raw packets were also written and independently decoded from disk.", "",
              f"Original grid adjacency ({3*n*n*(n-1):,} undirected edges) is recoverable from the restored cell indices and dimensions. It does not mean distances, angles, or geometric nearest neighbors remain unchanged. This is a kinematic deformation, not a mechanical bone or tension/compression simulation.", "",
              "## Squeezing and precision", "",
              "The squeeze control scales the displayed coordinates. The byte packets remain the same. A separate experiment rounds coordinates to a fixed grid spacing of 1/8 original cell width and counts distinct coordinate triples. Coincident triples are ambiguous if position alone must identify a cell; keeping separate cell IDs/values avoids that ambiguity but still requires storage. This measures coordinate collisions, not automatic corruption of the separately stored values.", "",
              "Repeated-zlib lengths in results.json measure five additional compression passes. They are observations for this codec, not a general impossibility proof. Likewise, the precision experiment describes its fixed-grid model; it does not rule out other coordinate encodings.", "",
              "A general lossless encoder must distinguish all possible inputs. There are 2^N strings of length N and only 2^N-1 strings of length less than N, even including the empty string. Therefore an injective encoder cannot shorten every N-bit input. This counting proof concerns arbitrary data; special growing families can have unbounded compression ratios. An experiment alone cannot establish or refute a universal compression theorem.", "",
              "## Reproduce or extend", "",
              "Run `python experiment.py --open` from the parent directory. Requires numpy and matplotlib. The standalone HTML needs no network. Change the seed with `--seed 123`, the cube size with `--size 24`, or add your own cubic uint8 NumPy array with `--input data.npy --size 32`. Array axes are z, y, x. Other types must be encoded deliberately; silently rounding them would change the experiment.", "",
              "The `.lat` files can be recovered independently using:", "", "```python",
              "from pathlib import Path", "import numpy as np", "from experiment import decode",
              'data = decode(Path("output/patterned.warped.lat").read_bytes())',
              'np.save("recovered.npy", data)', "```", "",
              "Useful extensions include other reversible warp families, a held-out real dataset, other codecs, and explicitly different definitions of which relationships must survive."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_figure(report: dict, output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import colors
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": "#203342",
                         "axes.labelcolor": "#203342", "axes.edgecolor": "#c5d3d9",
                         "xtick.color": "#465d6a", "ytick.color": "#465d6a"})
    n = report["size"]
    fig = plt.figure(figsize=(15, 10), facecolor="#f4f8fa")
    fig.suptitle("Can bending a data lattice make it compress better?", fontsize=21, x=.06, ha="left", y=.98)
    fig.text(.06, .931, f"Python experiment  /  {n**3:,} cells per dataset  /  every candidate reversed and verified", fontsize=12)
    data = np.array(report["datasets"][0]["values"], dtype=np.uint8).reshape((n,n,n))
    row = n // 2
    facecolors = plt.get_cmap("viridis")(colors.Normalize(0,255)(data[:,row,:])).reshape((-1,4))
    best = report["datasets"][0]["best"]
    smap = shift_map(n, best["a"], best["b"])[row]
    for panel, bent in enumerate((False, True), start=1):
        ax = fig.add_subplot(2, 2, panel, projection="3d", facecolor="#f4f8fa")
        polygons = []
        for z in range(n):
            for x in range(n):
                height = z - (int(smap[x]) if bent else 0)
                polygons.append([(x-.48,row,height-.48),(x+.48,row,height-.48),
                                 (x+.48,row,height+.48),(x-.48,row,height+.48)])
        ax.add_collection3d(Poly3DCollection(polygons, facecolors=facecolors,
                                             edgecolors=(.1,.2,.3,.3), linewidths=.18))
        ax.set(xlim=(-1,n), ylim=(-1,n), zlim=(-17,n+16), xlabel="Column x", ylabel="Row y", zlabel="Layer z")
        ax.view_init(elev=15, azim=-66)
        ax.set_box_aspect((1,.6,1.4))
        ax.set_title("After: curved bands line up" if bent else "Before: data in a flat grid", fontsize=14, pad=0)
        ax.tick_params(labelsize=8)
    ax = fig.add_subplot(2, 2, 3, facecolor="#f4f8fa")
    labels = [r["name"] for r in report["datasets"]]
    xx = np.arange(len(labels))
    for offset, key, label, color in ((-.25,"raw_packet_bytes","Raw", "#afbdc5"),
                                    (0,"baseline_bytes","Ordinary zlib", "#586c9c"),
                                    (.25,"best","Best warp + zlib", "#078477")):
        vals = [r[key]["bytes"] if key == "best" else r[key] for r in report["datasets"]]
        bars = ax.bar(xx+offset, vals, width=.24, label=label, color=color)
        ax.bar_label(bars, labels=[f"{v:,}" for v in vals], fontsize=8, padding=4)
    ax.set_yscale("log")
    ax.set_ylim(100, max(r["raw_packet_bytes"] for r in report["datasets"])*3)
    ax.set_xticks(xx, labels)
    ax.set_ylabel("Stored bytes (log scale)")
    ax.set_title("Measured files, including warp metadata", loc="left", fontsize=14, pad=18)
    ax.legend(loc="upper left", bbox_to_anchor=(0,-.12), ncol=3, frameon=False, fontsize=9)
    ax.spines[["top","right"]].set_visible(False)
    ax = fig.add_subplot(2, 2, 4, facecolor="#f4f8fa")
    precision = report["precision"]
    ax.plot([r["power"] for r in precision], [r["distinct_positions"] for r in precision],
            "o-", color="#078477", linewidth=2, label="Distinct rounded positions")
    ax.axhline(n**3, color="#586c9c", linestyle="--", label="Cells still stored")
    ax.set_xticks([0,2,4,6,8,10], ["1", "1/4", "1/16", "1/64", "1/256", "1/1024"])
    ax.set_xlabel("Geometric scale")
    ax.set_ylabel("Count")
    ax.set_title("Squeezing on a fixed coordinate grid", loc="left", fontsize=14, pad=18)
    ax.legend(loc="upper left", bbox_to_anchor=(0,-.12), frameon=False, fontsize=9)
    ax.spines[["top","right"]].set_visible(False)
    fig.subplots_adjust(left=.07, right=.95, bottom=.12, top=.87, hspace=.38, wspace=.3)
    fig.text(.06, .025, "Constructed example, chosen warp family. Fixed precision: 1/8 cell. Connectivity is recoverable; distances change.", fontsize=10)
    fig.savefig(output / "summary.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


def make_animation(report: dict, output: Path) -> None:
    """A directly viewable Python-rendered animation; no browser is needed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    n = report["size"]
    record = report["datasets"][0]
    row = n // 2
    values = np.array(record["values"], dtype=np.uint8).reshape((n,n,n))[:,row,:]
    colors = plt.get_cmap("viridis")(values.ravel()/255)
    fig = plt.figure(figsize=(11,6), facecolor="#f4f8fa")
    fig.suptitle("Warping a lattice of data", fontsize=20, y=.96)
    fig.text(.5,.88,"One row through a 32,768-cell cube. Each tile keeps its value and identity.".replace("32,768",f"{n**3:,}"),ha="center",fontsize=11)
    collections = []
    for index, title in enumerate(("Original positions", "Bend until bands align")):
        ax = fig.add_subplot(1,2,index+1,projection="3d",facecolor="#f4f8fa")
        collection = Poly3DCollection([],facecolors=colors,edgecolors=(.1,.2,.3,.25),linewidths=.2)
        ax.add_collection3d(collection)
        ax.set(xlim=(-1,n),ylim=(0,n),zlim=(-10,n+12),xlabel="x",zlabel="z")
        ax.set_yticks([])
        ax.view_init(elev=10,azim=-73)
        ax.set_box_aspect((1,.35,1.3))
        ax.set_title(title,fontsize=13)
        collections.append(collection)
    counter = fig.text(.5,.095,"",ha="center",fontsize=13)
    fig.text(.5,.035,"Bytes include the stored warp map. Every measured bend decodes exactly.",ha="center",fontsize=10)
    frames = [(round(5*t/10),round(3*t/10)) for t in range(11)]
    frames += [(5,3)]*6
    frames += list(reversed(frames[:11]))
    def polygons(shifts):
        return [[(x-.48,row,z-shifts[x]-.48),(x+.48,row,z-shifts[x]-.48),
                 (x+.48,row,z-shifts[x]+.48),(x-.48,row,z-shifts[x]+.48)]
                for z in range(n) for x in range(n)]
    collections[0].set_verts(polygons(np.zeros(n)))
    def update(frame):
        a,b=frames[frame]
        shifts=shift_map(n,a,b)[row].astype(float)
        collections[1].set_verts(polygons(shifts))
        size=record["candidates"][a*9+b]["bytes"]
        counter.set_text(f"Bend A={a}, B={b}     |     Original zlib: {record['baseline_bytes']:,} B     |     Warp + zlib: {size:,} B")
        return collections[1],counter
    fig.subplots_adjust(left=.035,right=.965,top=.8,bottom=.17,wspace=.05)
    animation=FuncAnimation(fig,update,frames=len(frames),interval=300,blit=False)
    animation.save(output/"warp.gif",writer=PillowWriter(fps=3),dpi=100)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=32)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("output"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    if not 8 <= args.size <= 64:
        parser.error("--size must be between 8 and 64")
    report = run(args.size, args.seed, args.output, args.input)
    if not args.no_plots:
        make_figure(report, args.output)
        make_animation(report, args.output)
    page = (args.output / "lattice.html").resolve()
    print(f"Interactive experiment: {page}")
    if args.open:
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
