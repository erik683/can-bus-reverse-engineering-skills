"""
align_reference.py - find the global clock offset between a webCAN trace and a
parallel reference sidecar, recorded on INDEPENDENT clocks (the PARALLEL
DECODED-LOG workflow).

A scan-tool log (HP Tuners, etc.) and the raw CAN log capture the same drive but
start their clocks independently, so the reference sidecar is shifted by an unknown
multi-second offset Delta. The per-signal lag search inside correlate/bitsearch/
verify is only a 21-point grid over +/-max_lag - fine for a sub-second residual,
far too coarse for that offset. This script pins Delta first.

Method: cross-correlate the reference series against EVERY candidate raw field
(id x byte-offset x width x endianness) over a WIDE lag range (FFT), pick the
global best field, and refine its lag with a direct Pearson scan. Use a dramatic
channel for the reference (Engine RPM is ideal: high variance, ~0 sensor lag). The
winning field's lag is Delta and the field itself independently confirms the
carrier; a clean lock reads r ~ 0.99. Then build EVERY sidecar for this log pair
with `scanlog_reference.py --offset <Delta>` and run the normal --max-lag 2 search
(which should report lag ~ 0, the proof Delta was right).

Examples:
    python align_reference.py --trace temp-output/trace_mustang.csv \
        --ref temp-output/sidecar_rpm_baseline.csv --exclude-ids 0x7e0,0x7e8
    # also write the aligned pilot sidecar in one go:
    python align_reference.py --trace ... --ref ...baseline.csv \
        --out temp-output/sidecar_rpm.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.signal import correlate

import common  # noqa: E402

HZ = common.GRID_HZ


def _decode(g, boff, w, order):
    mask = (1 << (8 * w)) - 1
    if order == "le":
        return ((g.le_int >> (boff * 8)) & mask).astype(float)
    shift = 8 * (g.length - boff - w)
    return ((g.be_int >> shift) & mask).astype(float)


def _zscore_on_grid(t, v, grid):
    out = np.interp(grid, t, v.astype(float), left=np.nan, right=np.nan)
    valid = np.isfinite(out)
    sd = np.nanstd(out)
    if not np.isfinite(sd) or sd == 0:
        return None, None
    z = (out - np.nanmean(out)) / sd
    z[~valid] = 0.0
    return z, valid


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True, help="webCAN CSV (raw bus)")
    ap.add_argument("--ref", required=True,
                    help="baseline reference sidecar (offset 0; e.g. RPM)")
    ap.add_argument("--exclude-ids", default="",
                    help="drop these IDs first, e.g. 0x7e0,0x7e8")
    ap.add_argument("--max-lag", type=float, default=60.0,
                    help="widest |offset| to search, seconds (default 60)")
    ap.add_argument("--widths", default="1,2",
                    help="candidate field widths in bytes (default 1,2)")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--out", help="write the reference sidecar shifted by Delta here "
                                  "(otherwise just report Delta)")
    args = ap.parse_args(argv)

    excl = {int(x, 16) for x in args.exclude_ids.replace("0x", "").split(",") if x.strip()}
    widths = tuple(int(x) for x in args.widths.split(",") if x.strip())

    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)
    sc = common.load_sidecar(args.ref)
    rt, rv = common.continuous_reference(sc)
    if rt.size == 0:
        sys.exit("reference sidecar has no kind=value rows")

    t0 = min(df["t"].min(), rt.min())
    t1 = max(df["t"].max(), rt.max())
    grid = np.arange(t0, t1, 1.0 / HZ)
    R, _ = _zscore_on_grid(rt, rv, grid)
    if R is None:
        sys.exit("reference is constant - pick a channel that actually varies")
    maxshift = int(args.max_lag * HZ)

    cands = []
    for cid, g in groups.items():
        if cid in excl:
            continue
        for w in widths:
            for boff in range(0, g.length - w + 1):
                for order in ("le", "be"):
                    raw = _decode(g, boff, w, order)
                    if np.nanstd(raw) == 0:
                        continue
                    C, _ = _zscore_on_grid(g.t, raw, grid)
                    if C is None:
                        continue
                    xc = correlate(C, R, mode="full", method="fft")
                    mid = len(R) - 1
                    lo = max(0, mid - maxshift)
                    hi = min(len(xc), mid + maxshift + 1)
                    if lo >= hi:
                        continue
                    seg = xc[lo:hi]
                    k = lo + int(np.argmax(np.abs(seg)))
                    norm = xc[k] / len(R)
                    cands.append((abs(norm), norm, (k - mid) / HZ,
                                  cid, boff, w, order))
    if not cands:
        sys.exit("no varying candidate fields - check the trace / --exclude-ids")
    cands.sort(reverse=True)

    print(f"Top {min(args.top, len(cands))} fields by |cross-correlation| with the reference:")
    print(f"{'rank':>4} {'|r|':>6} {'r':>7} {'lag_s':>8}  id      byte w  order")
    for i, (ab, nr, lag, cid, boff, w, order) in enumerate(cands[:args.top]):
        print(f"{i+1:>4} {ab:6.3f} {nr:7.3f} {lag:8.2f}  0x{cid:03x}  {boff:>3}  {w}  {order}")

    # refine the global best with a direct Pearson scan around its FFT lag
    _, _, lag, cid, boff, w, order = cands[0]
    raw = _decode(groups[cid], boff, w, order)
    C = np.interp(grid, groups[cid].t, raw, left=np.nan, right=np.nan)
    best_r, best_d = 0.0, lag
    for d in np.arange(lag - 3.0, lag + 3.0, 0.02):
        Rs = np.interp(grid, rt + d, rv, left=np.nan, right=np.nan)
        m = np.isfinite(C) & np.isfinite(Rs)
        if m.sum() < 100:
            continue
        r = np.corrcoef(C[m], Rs[m])[0, 1]
        if abs(r) > abs(best_r):
            best_r, best_d = r, d

    delta = round(float(best_d), 3)
    print(f"\nGLOBAL BEST: 0x{cid:03x} byte{boff} width{w} {order}  "
          f"=>  DELTA = {delta:+.3f} s   (pearson r={best_r:.4f})")
    if abs(best_r) < 0.9:
        print("  [!] weak lock (|r|<0.9): the logs may not be the same drive, or this "
              "reference channel is too flat - try a more dynamic channel (e.g. RPM).")
    print(f"  -> sidecar epoch = scan_time {delta:+.3f}s lands on the CAN clock.")
    print(f"     scanlog_reference.py --signal <name> --offset {delta} ...  (reuse for ALL channels)")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        import csv
        with open(args.out, "w", newline="") as fo:
            wr = csv.writer(fo, delimiter=";")
            wr.writerow(["epoch", "kind", "label", "value"])
            lbl = sc["label"].iloc[0] if "label" in sc and len(sc) else "ref"
            for t, v in zip(rt + delta, rv):
                wr.writerow([f"{t:.6f}", "value", lbl, repr(float(v))])
        print(f"  wrote aligned sidecar -> {args.out}")


if __name__ == "__main__":
    main()
