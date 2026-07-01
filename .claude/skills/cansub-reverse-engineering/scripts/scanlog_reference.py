"""
scanlog_reference.py - turn one column of a scan-tool log into a reference sidecar.

The PARALLEL DECODED-LOG counterpart to decode_reference.py (on-bus DBC) and
vision_reference.py (video OCR): when the user has a SECOND, parallel recording of
the same drive from a scan tool (HP Tuners / "HPT", FORScan, Torque, a dyno) that
already lists the signal in PHYSICAL UNITS over time, extract one of its columns
into the standard kind=value sidecar. The correlate / bitsearch / build_dbc /
verify chain then runs UNCHANGED against the raw CAN log - WITHOUT --exclude-ids
(the reference is off-bus, nothing to self-match).

These logs are typically a wide CSV with signal names, sometimes a second units
row, and a leading time column in seconds or milliseconds; every other column is
an already-decoded channel. HP Tuners samples fast (~60 Hz) because it passively
decodes the OEM broadcast frames - the very frames being reverse-engineered - so
its values track the raw bus tightly, off only by a constant clock offset.

The two logs are on INDEPENDENT clocks: run align_reference.py once on a dramatic
channel (Engine RPM) to find the global offset, then pass it here as --offset for
every channel of that log pair (epoch = time + offset lands on the CAN clock).

Examples:
    # 1) list the columns this scan log offers (forgot the exact name / units?):
    python scanlog_reference.py --log mustang_hpt.csv
    # 2) extract one column into a sidecar on the CAN clock (offset from align_reference):
    python scanlog_reference.py --log mustang_hpt.csv --signal "Engine RPM" \
        --label engine_rpm_ref --offset 5.93 --out temp-output/sidecar_rpm.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def _float_or_none(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _read_rows(path: str, delimiter: str):
    with open(path, newline="") as fi:
        sample = fi.read(8192)
        fi.seek(0)
        if delimiter == "auto":
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                reader = csv.reader(fi, dialect)
            except csv.Error:
                first = sample.splitlines()[0] if sample else ""
                delim = ";" if first.count(";") > first.count(",") else ","
                reader = csv.reader(fi, delimiter=delim)
        else:
            delim = "\t" if delimiter == "tab" else delimiter
            reader = csv.reader(fi, delimiter=delim)
        return list(reader)


def _infer_layout(rows: list[list[str]]) -> tuple[int, int | None, int]:
    """Locate (names_row, units_row_or_None, data_start) in a scan-tool CSV.

    Handles a preamble before the header (an HP Tuners export leads with
    "[Log Information]" / a numeric channel-ID row / "[Channel Data]"): the data
    start is the first row opening a RUN of consecutive numeric-first rows of
    consistent width (a lone numeric-looking preamble row - the channel-ID line -
    is followed by the names row, so it never starts a run). The names/units rows
    are then the nearest data-width rows above it: one wide row = names only; two
    = names + units (the lower is units), skipping narrow section markers.
    """
    def numeric_first(r):
        return bool(r) and _float_or_none(r[0].strip()) is not None

    data_start = None
    for i, row in enumerate(rows):
        if not numeric_first(row) or len(row) < 2:
            continue
        run = min(5, len(rows) - i)
        if all(numeric_first(rows[j]) and abs(len(rows[j]) - len(row)) <= 1
               for j in range(i, i + run)):
            data_start = i
            break
    if data_start is None or data_start == 0:
        # No preamble/header structure found - fall back to the simple layout.
        if len(rows) >= 2 and rows[1] and not numeric_first(rows[1]):
            return 0, 1, 2
        return 0, None, 1

    width = len(rows[data_start])
    wide = [i for i in range(data_start - 1, -1, -1)
            if len(rows[i]) >= max(2, width // 2) and not numeric_first(rows[i])]
    if not wide:
        return 0, None, data_start
    if len(wide) >= 2 and wide[1] == wide[0] - 1:
        return wide[1], wide[0], data_start     # names above units
    return wide[0], None, data_start


def _infer_time_scale(name: str, unit: str) -> float:
    n = name.strip().lower()
    u = unit.strip().lower()
    if u in ("us", "usec", "microsecond", "microseconds") or "(us)" in n or "[us]" in n:
        return 1e-6
    if u in ("ms", "msec", "millisecond", "milliseconds") or "(ms)" in n or "[ms]" in n:
        return 1e-3
    return 1.0


def _resolve(names: list[str], query: str) -> int:
    q = query.strip().lower()
    exact = [i for i, n in enumerate(names) if n.strip().lower() == q]
    if exact:
        return exact[0]
    subs = [i for i, n in enumerate(names) if q in n.strip().lower()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        sys.exit(f"no column matches {query!r}; run without --signal to list columns")
    sys.exit(f"ambiguous {query!r}: {[names[i] for i in subs]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True, help="scan-tool CSV (e.g. an HP Tuners export)")
    ap.add_argument("--signal", help="column to extract (case-insensitive exact, "
                                     "else unique substring). Omit to LIST columns.")
    ap.add_argument("--label", default="ref", help="sidecar label (default 'ref')")
    ap.add_argument("--out", help="sidecar CSV (default temp-output/sidecar_<label>.csv)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds added to the time column (the align_reference.py Delta; "
                         "default 0 = build the baseline for alignment)")
    ap.add_argument("--header-rows", type=int, default=0,
                    help="header rows before data: 0 = auto, 2 = names+units, "
                         "1 = names only (default: 0)")
    ap.add_argument("--time-col", help="time-column name (default: first column)")
    ap.add_argument("--time-scale", type=float,
                    help="multiply the source time column to get seconds "
                         "(default: infer from time column name/unit; e.g. 1e-3 for ms)")
    ap.add_argument("--delimiter", default="auto", choices=["auto", ",", ";", "tab"],
                    help="CSV delimiter (default: auto-sniff comma/semicolon/tab)")
    args = ap.parse_args(argv)

    rows = _read_rows(args.log, args.delimiter)
    if args.header_rows == 0:
        names_i, units_i, data_start = _infer_layout(rows)
    elif args.header_rows >= 1:
        names_i, units_i, data_start = 0, (1 if args.header_rows >= 2 else None), args.header_rows
    else:
        sys.exit("--header-rows must be 0 (auto) or >=1")
    if len(rows) <= data_start:
        sys.exit("log has no data rows")
    names = rows[names_i]
    units = rows[units_i] if units_i is not None else [""] * len(names)
    data = rows[data_start:]

    if not args.signal:
        print(f"{len(data)} data rows; columns (name [unit]):")
        for i, n in enumerate(names):
            u = units[i].strip() if i < len(units) else ""
            print(f"  [{i:2}] {n.strip()}" + (f"  [{u}]" if u else ""))
        print("\nRe-run with --signal <name> (and --offset from align_reference.py).")
        return

    ti = _resolve(names, args.time_col) if args.time_col else 0
    ci = _resolve(names, args.signal)
    unit = units[ci].strip() if ci < len(units) else ""
    time_unit = units[ti].strip() if ti < len(units) else ""
    time_scale = args.time_scale if args.time_scale is not None else _infer_time_scale(
        names[ti], time_unit)

    out = args.out or f"temp-output/sidecar_{args.label}.csv"
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    pairs, skipped = [], 0
    for row in data:
        if not row or ci >= len(row) or ti >= len(row):
            continue
        try:
            t = float(row[ti]) * time_scale
            v = float(row[ci])
        except ValueError:
            skipped += 1                # text / blank cell (e.g. a label column)
            continue
        pairs.append((t + args.offset, v))
    pairs.sort()
    if not pairs:
        sys.exit(f"column {names[ci]!r} has no numeric rows")

    with open(out, "w", newline="") as fo:
        w = csv.writer(fo, delimiter=";")
        w.writerow(["epoch", "kind", "label", "value"])
        for t, v in pairs:
            w.writerow([f"{t:.6f}", "value", args.label, repr(v)])

    span = f"{pairs[0][0]:.3f}..{pairs[-1][0]:.3f}s"
    vals = [v for _, v in pairs]
    print(f"col[{ci}] {names[ci].strip()!r} ({unit or 'no unit'}) -> {len(pairs)} rows -> {out}")
    print(f"  time x{time_scale:g} + offset {args.offset:+g}s | epoch span {span} | "
          f"value {min(vals):g}..{max(vals):g}"
          + (f" | skipped {skipped} non-numeric rows" if skipped else ""))
    if args.offset == 0.0:
        print("  [offset 0] this is the BASELINE; run align_reference.py to find Delta, "
              "then re-run with --offset <Delta>.")


if __name__ == "__main__":
    main()
