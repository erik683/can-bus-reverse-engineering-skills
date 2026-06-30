"""
savvycan_to_webcan.py - convert a GVRET / SavvyCAN CSV export to webCAN CSV.

The RE pipeline (survey / correlate / bitsearch / build_dbc / verify) reads only
the webCAN CSV that python-can-cansub writes. Other loggers export different
layouts; the most common is a GVRET / SavvyCAN export:

    Time Stamp,ID,Extended,Dir,Bus,LEN,D1,D2,D3,D4,D5,D6,D7,D8
    4481122,00000215,false,Rx,0,8,27,60,27,60,27,60,27,60,

(comma-separated, timestamp in MICROSECONDS, one hex byte per Dn column). This
script rewrites it as webCAN:

    TimestampEpoch;BusChannel;ID;IDE;DLC;DataLength;Dir;EDL;BRS;ESI;RTR;DataBytes

(semicolon-separated, epoch in SECONDS, D1..D{LEN} concatenated into one hex
DataBytes string). Classical CAN only: EDL/BRS/ESI/RTR are written 0. Columns are
located by header name (case-insensitive), so minor column-order variations are
tolerated. The result loads with common.load_trace (which sorts by time, so an
unsorted source export is fine).

Example:
    python savvycan_to_webcan.py --input 2006_Mustang_HSCAN.csv \
        --out temp-output/trace_mustang.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import common  # noqa: E402  (validates the output by re-loading it)

WEBCAN_HEADER = common.WEBCAN_COLUMNS


def _colmap(header: list[str]) -> dict[str, int]:
    """name(lower, stripped) -> index."""
    return {h.strip().lower(): i for i, h in enumerate(header)}


def _find(cmap: dict[str, int], *names: str) -> int | None:
    for n in names:
        if n in cmap:
            return cmap[n]
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="GVRET/SavvyCAN CSV export")
    ap.add_argument("--out", help="webCAN CSV (default temp-output/trace_<stem>.csv)")
    ap.add_argument("--ts-scale", type=float, default=1e-6,
                    help="multiply the source timestamp to get seconds "
                         "(default 1e-6 = microseconds; use 1e-3 for ms, 1 for s)")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip the common.load_trace round-trip check")
    args = ap.parse_args(argv)

    out = args.out or f"temp-output/trace_{Path(args.input).stem}.csv"
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    with open(args.input, newline="") as fi:
        r = csv.reader(fi)
        header = next(r)
        cmap = _colmap(header)
        i_ts = _find(cmap, "time stamp", "timestamp", "time")
        i_id = _find(cmap, "id")
        i_ext = _find(cmap, "extended", "ide")
        i_dir = _find(cmap, "dir", "direction")
        i_bus = _find(cmap, "bus", "buschannel", "channel")
        i_len = _find(cmap, "len", "dlc", "datalength")
        d_cols = sorted((int(m.group(1)), i) for h, i in cmap.items()
                        if (m := re.fullmatch(r"d\s*(\d+)", h)))
        d_idx = [i for _, i in d_cols]
        if None in (i_ts, i_id, i_len) or not d_idx:
            sys.exit(f"unrecognised header (need Time Stamp / ID / LEN / Dn): {header}")

        n = 0
        with open(out, "w", newline="") as fo:
            w = csv.writer(fo, delimiter=";")
            w.writerow(WEBCAN_HEADER)
            for row in r:
                if not row or not row[i_ts].strip():
                    continue
                ts = float(row[i_ts]) * args.ts_scale
                can_id = row[i_id].strip().lstrip("0") or "0"        # hex, no 0x
                ext = "1" if (i_ext is not None and
                              row[i_ext].strip().lower() in ("true", "1")) else "0"
                direction = "1" if (i_dir is not None and
                                    row[i_dir].strip().lower() in ("tx", "1")) else "0"
                bus = (row[i_bus].strip() if i_bus is not None else "") or "0"
                ln = int(row[i_len])
                data = "".join(row[i].strip() for i in d_idx[:ln] if i < len(row))
                w.writerow([f"{ts:.6f}", bus, can_id, ext, ln, ln,
                            direction, "0", "0", "0", "0", data])
                n += 1

    print(f"wrote {n} frames -> {out}  (ts x{args.ts_scale:g} -> seconds)")
    if not args.no_validate:
        df = common.load_trace(out)
        ids = ", ".join(sorted(f"0x{i:x}" for i in df["id"].unique()))
        print(f"validated: {len(df)} Rx frames, span {df['t'].min():.3f}..{df['t'].max():.3f}s, "
              f"{df['id'].nunique()} IDs [{ids}]")


if __name__ == "__main__":
    main()
