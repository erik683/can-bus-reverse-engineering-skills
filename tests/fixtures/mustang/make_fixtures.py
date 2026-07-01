"""Regenerate the Mustang regression fixtures from the full local logs.

The fixtures are time-window subsets of two independent 2006 Ford Mustang GT
HS-CAN drives, each a (raw GVRET/SavvyCAN export, parallel HP Tuners log) pair.
The windows were chosen to contain strong RPM/speed dynamics, idle rest (for the
zero-anchor re-fit), and engine-overrun segments (pedal < 2 % with RPM > 1800 —
the divergence regime the torque negative-control test needs).

Full logs are NOT in the repo (~100 MB); this script needs them locally:
    python tests/fixtures/mustang/make_fixtures.py [--logs-dir mustang-test-logs]

Windows (CAN clock, seconds):
    drive1: [145, 255]  of 2006_Ford_Mustang_GT_HSCAN.csv       (Delta = +5.93 s)
    drive2: [575, 666]  of 2006_Ford_Mustang_GT_HSCAN_GVRET_2.csv (Delta = +3.15 s)
The HP Tuners slice is widened by +/-5 s (converted to the scan clock via Delta)
so align_reference has slack around the window edges.
"""
from __future__ import annotations

import argparse
import gzip
from pathlib import Path

HERE = Path(__file__).parent

# (gvret source, hpt source, can-clock window, known Delta, fixture stem)
DRIVES = [
    ("2006_Ford_Mustang_GT_HSCAN.csv", "2006_Ford_Mustang_GT_HSCAN_HPT.csv",
     (145.0, 255.0), 5.93, "drive1"),
    ("2006_Ford_Mustang_GT_HSCAN_GVRET_2.csv", "2006_Ford_Mustang_GT_HSCAN_HPT_2.csv",
     (575.0, 666.0), 3.15, "drive2"),
]
MARGIN = 5.0    # extra seconds of scan-tool data either side of the window


def slice_gvret(src: Path, out: Path, w0: float, w1: float) -> int:
    lines = src.read_text().splitlines()
    kept = [lines[0]]
    for line in lines[1:]:
        cell = line.split(",", 1)[0]
        try:
            t = int(cell) * 1e-6            # GVRET Time Stamp is microseconds
        except ValueError:
            continue
        if w0 <= t <= w1:
            kept.append(line)
    with gzip.open(out, "wt", newline="\n") as fo:
        fo.write("\n".join(kept) + "\n")
    return len(kept) - 1


def slice_hpt(src: Path, out: Path, lo: float, hi: float) -> int:
    lines = src.read_text().splitlines()
    data_start = next(i for i, l in enumerate(lines) if l.strip() == "[Channel Data]") + 1
    kept = lines[:data_start]
    n = 0
    for line in lines[data_start:]:
        cell = line.split(",", 1)[0]
        try:
            t = float(cell)                 # HPT Offset column is seconds
        except ValueError:
            continue
        if lo <= t <= hi:
            kept.append(line)
            n += 1
    with gzip.open(out, "wt", newline="\n") as fo:
        fo.write("\n".join(kept) + "\n")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--logs-dir", default="mustang-test-logs",
                    help="directory with the full source logs")
    args = ap.parse_args()
    logs = Path(args.logs_dir)

    for gvret, hpt, (w0, w1), delta, stem in DRIVES:
        n = slice_gvret(logs / gvret, HERE / f"{stem}_gvret.csv.gz", w0, w1)
        m = slice_hpt(logs / hpt, HERE / f"{stem}_hpt.csv.gz",
                      w0 - delta - MARGIN, w1 - delta + MARGIN)
        print(f"{stem}: {n} CAN frames [{w0:g},{w1:g}]s + {m} scan rows "
              f"(Delta {delta:+g}s) -> {stem}_gvret.csv.gz / {stem}_hpt.csv.gz")


if __name__ == "__main__":
    main()
