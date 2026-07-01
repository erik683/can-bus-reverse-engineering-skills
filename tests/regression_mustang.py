"""End-to-end regression harness for the cansub-reverse-engineering skill.

Runs the real script chain (savvycan_to_webcan -> scanlog_reference ->
align_reference -> survey -> correlate -> bitsearch -> build_dbc -> verify ->
filter_regime) against two independent 2006 Ford Mustang GT HS-CAN drives with
KNOWN ground truth, and asserts the known answers:

    0x201  engine_rpm      big-endian bytes 0-1, x0.25 rpm
    0x201  vehicle_speed   big-endian bytes 4-5, x0.01 km/h - 100 (~0.00621 mph)
    0x201  accel_pedal     byte 6, x0.5 %
    0x215  wheel speeds    four 16-bit BE fields (tie-group behind vehicle_speed)
    torque (ETC Torque Request)  NOT broadcast: nothing locks inside the
           overrun divergence regime, and the airflow proxies get flagged.

Known-broken behaviors are encoded as XFAIL (expected failures). When a fix
lands and an XFAIL starts passing, it reports XPASS and the suite fails until
the expectation is flipped - that is intentional: the xfails are the
acceptance tests for planned refinements (big-endian bitsearch parity, the
over-wide parsimony guard against a co-varying neighbor byte).

Usage (from the repo root, venv python):
    .venv/bin/python tests/regression_mustang.py            # committed fixtures
    .venv/bin/python tests/regression_mustang.py --full     # full local logs
    .venv/bin/python tests/regression_mustang.py --skip-selftests

Fixtures: tests/fixtures/mustang/*.csv.gz (see make_fixtures.py there for
provenance). --full needs the full logs in mustang-test-logs/ and skips the
window-calibrated divergence-regime tier. Exit code 0 = all green.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude/skills/cansub-reverse-engineering/scripts"
FIXTURES = ROOT / "tests/fixtures/mustang"
WORK = ROOT / "temp-output/regression"
FULL_LOGS = ROOT / "mustang-test-logs"

# ---------------------------------------------------------------- ground truth
DRIVES = {
    # stem -> (full gvret log, full hpt log, expected Delta seconds)
    "drive1": ("2006_Ford_Mustang_GT_HSCAN.csv",
               "2006_Ford_Mustang_GT_HSCAN_HPT.csv", 5.93),
    "drive2": ("2006_Ford_Mustang_GT_HSCAN_GVRET_2.csv",
               "2006_Ford_Mustang_GT_HSCAN_HPT_2.csv", 3.15),
}
DELTA_TOL = 0.25          # s; correlate's lag search absorbs the residual
MPH_PER_BIT = 0.01 / 1.609344   # the 0x201/0x215 speed LSB (0.01 km/h) in mph

CHANNELS = {              # HPT column -> sidecar label
    "Engine RPM": "rpm",
    "Vehicle Speed": "speed",
    "Accelerator Pedal Position": "pedal",
    "ETC Torque Request": "torque",
    "Calculated Manifold Absolute Pressure": "map",
}

results: list[tuple[str, str, str]] = []      # (status, name, detail)


def check(name: str, ok: bool, detail: str = "", xfail: str | None = None):
    """Record one assertion. xfail = reason the failure is EXPECTED today."""
    if xfail is None:
        status = "PASS" if ok else "FAIL"
    else:
        status = "XPASS" if ok else "XFAIL"
        detail = (detail + f"  [{xfail}]").strip()
    results.append((status, name, detail))
    mark = {"PASS": " ok ", "FAIL": "FAIL", "XFAIL": "xfail", "XPASS": "XPASS"}[status]
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and status != "PASS" else ""))


def run(script: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS / script), *map(str, args)]
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT)


def gunzip(src: Path, dst: Path) -> Path:
    with gzip.open(src, "rt") as fi, open(dst, "w", newline="") as fo:
        shutil.copyfileobj(fi, fo)
    return dst


def parse_dbc_signal(dbc: Path):
    """-> (name, start_bit, length, order, signed, scale, offset) of the one SG_."""
    m = re.search(r'SG_ (\w+) : (\d+)\|(\d+)@([01])([+-]) \(([^,]+),([^)]+)\)',
                  dbc.read_text())
    if not m:
        return None
    return (m[1], int(m[2]), int(m[3]), "little" if m[4] == "1" else "big",
            m[5] == "-", float(m[6]), float(m[7]))


def top(cands: list[dict]) -> dict:
    return cands[0]


def is_geom(c: dict, id_hex: str, byte: int, width: int, order: str) -> bool:
    return (c["id_hex"] == id_hex and c["byte"] == byte
            and c["width"] == width and c["order"] == order)


# ------------------------------------------------------------------ the tiers
def prepare_drive(stem: str, full: bool) -> dict | None:
    """Convert + list-check + align + build all sidecars. Returns stage paths."""
    print(f"\n== {stem}: convert, parse scan log, align ==")
    if full:
        gvret = FULL_LOGS / DRIVES[stem][0]
        hpt = FULL_LOGS / DRIVES[stem][1]
        if not gvret.exists() or not hpt.exists():
            check(f"{stem} full logs present", False, f"missing under {FULL_LOGS}")
            return None
    else:
        gvret = gunzip(FIXTURES / f"{stem}_gvret.csv.gz", WORK / f"{stem}_gvret.csv")
        hpt = gunzip(FIXTURES / f"{stem}_hpt.csv.gz", WORK / f"{stem}_hpt.csv")

    trace = WORK / f"trace_{stem}.csv"
    p = run("savvycan_to_webcan.py", "--input", gvret, "--out", trace)
    m = re.search(r"validated: (\d+) Rx frames.*?(\d+) IDs \[([^\]]+)\]", p.stdout)
    ids = set(m[3].split(", ")) if m else set()
    check(f"{stem} GVRET->webCAN converts", p.returncode == 0 and m is not None,
          (p.stderr or p.stdout).strip()[-200:] if p.returncode else "")
    check(f"{stem} trace has the known IDs",
          m is not None and int(m[1]) > 10000
          and {"0x200", "0x201", "0x215", "0x420"} <= ids,
          f"{m[1] if m else '?'} frames, IDs {sorted(ids)}")

    # list mode must read a RAW HP Tuners export (preamble + [Channel Data])
    p = run("scanlog_reference.py", "--log", hpt)
    check(f"{stem} raw HPT export parses (list mode)",
          p.returncode == 0 and all(ch in p.stdout for ch in CHANNELS),
          (p.stdout or p.stderr).strip()[:200])

    base = WORK / f"sidecar_{stem}_rpm0.csv"
    p = run("scanlog_reference.py", "--log", hpt, "--signal", "Engine RPM",
            "--label", "rpm", "--offset", "0", "--out", base)
    check(f"{stem} RPM baseline sidecar", p.returncode == 0, p.stderr.strip()[:200])

    p = run("align_reference.py", "--trace", trace, "--ref", base,
            "--exclude-ids", "0x7e0,0x7e8")
    md = re.search(r"DELTA = ([+-][\d.]+) s\s+\(pearson r=([\d.]+)\)", p.stdout)
    delta = float(md[1]) if md else None
    exp = DRIVES[stem][2]
    check(f"{stem} align solves Delta ~ {exp:+g}s",
          md is not None and abs(delta - exp) <= DELTA_TOL and float(md[2]) >= 0.99,
          f"DELTA={md[1] if md else '?'} r={md[2] if md else '?'}")
    check(f"{stem} align pilot lands on 0x201", "GLOBAL BEST: 0x201" in p.stdout)
    if delta is None:
        return None

    sidecars = {}
    for col, lab in CHANNELS.items():
        out = WORK / f"sidecar_{stem}_{lab}.csv"
        p = run("scanlog_reference.py", "--log", hpt, "--signal", col,
                "--label", lab, "--offset", f"{delta}", "--out", out)
        if p.returncode != 0:
            check(f"{stem} sidecar {lab}", False, p.stderr.strip()[:200])
            return None
        sidecars[lab] = out
    return {"trace": trace, "delta": delta, **sidecars}


def correlate(trace: Path, sidecar: Path, tag: str, *extra: str) -> list[dict]:
    out = WORK / f"candidates_{tag}.json"
    p = run("correlate.py", "--trace", trace, "--sidecar", sidecar,
            "--type", "continuous", "--json", out, "--no-plots", *extra)
    if p.returncode != 0 or not out.exists():
        check(f"correlate {tag} runs", False, (p.stderr or p.stdout).strip()[-200:])
        return []
    return json.loads(out.read_text())


def tier_identification(d1: dict):
    print("\n== drive1: survey + correlate (identification) ==")
    p = run("survey.py", "--trace", d1["trace"], "--no-plots")
    ms = re.search(r"== (\d+) unique IDs", p.stdout)
    check("survey runs and counts IDs",
          p.returncode == 0 and ms is not None and int(ms[1]) >= 12,
          f"IDs={ms[1] if ms else '?'}")

    c = correlate(d1["trace"], d1["rpm"], "rpm")
    if c:
        t = top(c)
        check("RPM -> 0x201 bytes0-1 big-endian", is_geom(t, "201", 0, 2, "big"),
              f"top={t['id_hex']} b{t['byte']} w{t['width']} {t['order']}")
        check("RPM top fit is tight (r2>=0.99, lag~0)",
              t["r2"] >= 0.99 and abs(t["lag_s"]) <= 0.3 and not t["proxy_suspect"],
              f"r2={t['r2']} lag={t['lag_s']}")

    c = correlate(d1["trace"], d1["speed"], "speed")
    if c:
        t = top(c)
        check("speed -> 0x201 bytes4-5 big-endian", is_geom(t, "201", 4, 2, "big"),
              f"top={t['id_hex']} b{t['byte']} w{t['width']} {t['order']}")
        wheels = [x for x in c if x["id_hex"] == "215" and x["width"] == 2
                  and x["order"] == "big" and x["r2"] >= 0.97]
        check("speed tie-group: 0x215 wheel speeds present (>=3 @ r2>=0.97)",
              len(wheels) >= 3, f"found {len(wheels)}")

    c = correlate(d1["trace"], d1["pedal"], "pedal")
    if c:
        t = top(c)
        check("pedal -> 0x201 byte6", t["id_hex"] == "201" and t["byte"] == 6,
              f"top={t['id_hex']} b{t['byte']} w{t['width']} {t['order']}")
        check("pedal top fit is tight (r2>=0.99)", t["r2"] >= 0.99, f"r2={t['r2']}")


def tier_bitsearch(d1: dict):
    print("\n== drive1: bitsearch (field of record; known gaps are XFAIL) ==")
    out = WORK / "bitsearch_rpm.json"
    p = run("bitsearch.py", "--trace", d1["trace"], "--sidecar", d1["rpm"],
            "--id", "0x201", "--json", out, "--no-plots")
    if p.returncode != 0:
        check("bitsearch rpm runs", False, (p.stderr or p.stdout).strip()[-200:])
    else:
        t = json.loads(out.read_text())[0]
        ok = (t["length"] >= 14 and abs(t["scale"] - 0.25) / 0.25 <= 0.05)
        check("bitsearch finds the FULL 16-bit BE RPM field (scale~0.25)", ok,
              f"top={t['order']} {t['start_bit']}|{t['length']} scale={t['scale']:.4g}",
              xfail="Intel-first start-bit search under-reads a wide BE field "
                    "as its high active bits; see SKILL.md big-endian note")

    out = WORK / "bitsearch_pedal.json"
    p = run("bitsearch.py", "--trace", d1["trace"], "--sidecar", d1["pedal"],
            "--id", "0x201", "--json", out, "--no-plots")
    if p.returncode != 0:
        check("bitsearch pedal runs", False, (p.stderr or p.stdout).strip()[-200:])
    else:
        t = json.loads(out.read_text())[0]
        ok = (t["start_bit"] >= 48 and t["start_bit"] + t["length"] <= 56
              and abs(t["scale"] - 0.5) / 0.5 <= 0.05)
        check("bitsearch keeps pedal inside byte6 (scale~0.5)", ok,
              f"top={t['order']} {t['start_bit']}|{t['length']} scale={t['scale']:.4g}",
              xfail="over-wide read swallows the co-varying speed low byte "
                    "(scale ~0.5/256); parsimony/cascade guard doesn't fire")


def build_and_verify(d1: dict) -> Path | None:
    print("\n== drive1: build_dbc + verify (calibration + gate) ==")
    specs = [
        # tag, sidecar, byte, width, order, unit, scale check, offset check
        ("engine_rpm", "rpm", 0, 2, "big", "rpm",
         lambda s: abs(s - 0.25) < 1e-6, lambda o: abs(o) < 1e-6),
        ("vehicle_speed", "speed", 4, 2, "big", "mph",
         lambda s: abs(s - MPH_PER_BIT) / MPH_PER_BIT <= 0.015,
         lambda o: -63.5 <= o <= -60.5),
        ("accel_pedal", "pedal", 6, 1, "little", "%",
         lambda s: abs(s - 0.5) < 1e-6, lambda o: abs(o) < 1e-6),
    ]
    rpm_dbc = None
    for tag, lab, byte, width, order, unit, sc_ok, off_ok in specs:
        dbc = WORK / f"{tag}.dbc"
        p = run("build_dbc.py", "--trace", d1["trace"], "--sidecar", d1[lab],
                "--id", "0x201", "--byte", byte, "--width", width,
                "--order", order, "--lag", "0", "--name", tag, "--unit", unit,
                "--out", dbc, "--no-plots")
        sig = parse_dbc_signal(dbc) if dbc.exists() else None
        if p.returncode != 0 or sig is None:
            check(f"build_dbc {tag}", False, (p.stderr or p.stdout).strip()[-200:])
            continue
        name, start, length, sorder, signed, scale, offset = sig
        check(f"build_dbc {tag}: geometry kept",
              length == width * 8 and sorder == order and not signed,
              f"SG {start}|{length} {sorder}")
        check(f"build_dbc {tag}: scale/offset correct",
              sc_ok(scale) and off_ok(offset), f"scale={scale} offset={offset}")
        if tag == "engine_rpm":
            rpm_dbc = dbc

        p = run("verify.py", "--trace", d1["trace"], "--dbc", dbc,
                "--sidecar", d1[lab], "--png", WORK / f"verify_{tag}.png")
        sp = re.search(r"Spearman_overall=([\d.]+)", p.stdout)
        sl = re.search(r"slope ([-\d.]+)", p.stdout)
        check(f"verify {tag}: PASS gate",
              p.returncode == 0 and "VERDICT: PASS" in p.stdout,
              f"exit={p.returncode}")
        check(f"verify {tag}: agreement (Spearman>=0.98, slope~1)",
              sp is not None and float(sp[1]) >= 0.98
              and sl is not None and 0.97 <= float(sl[1]) <= 1.03,
              f"spearman={sp[1] if sp else '?'} slope={sl[1] if sl else '?'}")
    return rpm_dbc


def tier_divergence(d2: dict):
    print("\n== drive2: divergence-regime proxy test (torque negative control) ==")
    # Positive control first: with MAP as a co-variate, the true RPM field must
    # still win with a strongly positive margin (the margin sign convention).
    c = correlate(d2["trace"], d2["rpm"], "rpm2_cov",
                  "--covariate", f"map={d2['map']}")
    if c:
        t = top(c)
        check("positive control: RPM wins with margin >= +0.3 vs MAP",
              is_geom(t, "201", 0, 2, "big") and t["r2"] >= 0.99
              and t.get("margin", 0) >= 0.3,
              f"top={t['id_hex']} b{t['byte']} margin={t.get('margin')}")

    ftrace = WORK / "trace_drive2_overrun.csv"
    fside = WORK / "sidecar_drive2_torque_overrun.csv"
    p = run("filter_regime.py", "--trace", d2["trace"], "--sidecar", d2["torque"],
            "--ref", f"rpm={d2['rpm']}", "--ref", f"pedal={d2['pedal']}",
            "--where", "rpm > 1800 and pedal < 2",
            "--out-trace", ftrace, "--out-sidecar", fside)
    mk = re.search(r"kept (\d+) / (\d+) frames \(([\d.]+)%\)", p.stdout)
    mi = re.search(r"(\d+) interval\(s\)", p.stdout)
    mr = re.search(r"rpm\s+inside\s+([\d.]+)\.\.", p.stdout)
    mp = re.search(r"pedal\s+inside\s+[\d.]+\.\.([\d.]+)", p.stdout)
    check("filter_regime slices the overrun window",
          p.returncode == 0 and mk is not None and 15 <= float(mk[3]) <= 45
          and mi is not None and int(mi[1]) >= 5,
          f"kept={mk[3] if mk else '?'}% intervals={mi[1] if mi else '?'}")
    check("filter_regime honors the mask (rpm>=1800, pedal<2 inside)",
          mr is not None and float(mr[1]) >= 1800.0
          and mp is not None and float(mp[1]) < 2.0,
          f"rpm_min={mr[1] if mr else '?'} pedal_max={mp[1] if mp else '?'}")

    c = correlate(ftrace, fside, "torque2_overrun",
                  "--covariate", f"map={d2['map']}", "--covariate", f"rpm={d2['rpm']}")
    if c:
        t = top(c)
        check("covariate margins are computed for every candidate",
              all("margin" in x and "cov_name" in x for x in c))
        check("negative control: nothing LOCKS on torque in the regime (r2<0.9)",
              max(x["r2"] for x in c) < 0.9,
              f"max r2={max(x['r2'] for x in c)}")
        check("negative control: no confident torque lean (r2>=0.8 & margin>=0.5)",
              not any(x["r2"] >= 0.8 and x.get("margin", 0) >= 0.5 for x in c))
        check("negative control: winner is proxy-flagged",
              t["proxy_suspect"] is True,
              f"top={t['id_hex']} b{t['byte']} r2={t['r2']} spear={t['score']}")
        check("negative control: proxy tell fires broadly (>=5 flagged)",
              sum(1 for x in c if x["proxy_suspect"]) >= 5,
              f"flagged={sum(1 for x in c if x['proxy_suspect'])}")


def tier_cross_capture(rpm_dbc: Path, d2: dict):
    print("\n== cross-capture: drive1 RPM DBC vs drive2 ==")
    p = run("verify.py", "--trace", d2["trace"], "--dbc", rpm_dbc,
            "--sidecar", d2["rpm"], "--png", WORK / "verify_xcap_rpm.png")
    sp = re.search(r"Spearman_overall=([\d.]+)", p.stdout)
    sl = re.search(r"slope ([-\d.]+)", p.stdout)
    check("drive1 DBC re-decodes drive2: PASS",
          p.returncode == 0 and "VERDICT: PASS" in p.stdout, f"exit={p.returncode}")
    check("cross-capture agreement (Spearman>=0.99, slope~1)",
          sp is not None and float(sp[1]) >= 0.99
          and sl is not None and 0.98 <= float(sl[1]) <= 1.02,
          f"spearman={sp[1] if sp else '?'} slope={sl[1] if sl else '?'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="run against the full logs in mustang-test-logs/ "
                         "(skips the window-calibrated divergence tier)")
    ap.add_argument("--skip-selftests", action="store_true",
                    help="skip common.py/bitsearch.py --selftest")
    args = ap.parse_args()

    t0 = time.time()
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    if not args.skip_selftests:
        print("== selftests ==")
        for script in ("common.py", "bitsearch.py"):
            p = run(script, "--selftest")
            check(f"{script} --selftest", p.returncode == 0,
                  (p.stderr or p.stdout).strip()[-200:] if p.returncode else "")

    d1 = prepare_drive("drive1", args.full)
    if d1:
        tier_identification(d1)
        tier_bitsearch(d1)
        rpm_dbc = build_and_verify(d1)
    else:
        rpm_dbc = None

    d2 = prepare_drive("drive2", args.full)
    if d2:
        if args.full:
            print("\n(--full: skipping the window-calibrated divergence tier)")
        else:
            tier_divergence(d2)
        if rpm_dbc:
            tier_cross_capture(rpm_dbc, d2)

    # ------------------------------------------------------------- summary
    counts = {s: sum(1 for r in results if r[0] == s)
              for s in ("PASS", "FAIL", "XFAIL", "XPASS")}
    print(f"\n{'=' * 70}")
    print(f"{counts['PASS']} passed, {counts['FAIL']} failed, "
          f"{counts['XFAIL']} expected failures (known gaps), "
          f"{counts['XPASS']} unexpectedly passed  [{time.time() - t0:.0f}s]")
    for status, name, detail in results:
        if status == "FAIL":
            print(f"  FAIL:  {name}  {detail}")
        elif status == "XPASS":
            print(f"  XPASS: {name} - the known gap seems FIXED; flip its "
                  f"xfail expectation in this harness to lock it in.")
    return 1 if counts["FAIL"] or counts["XPASS"] else 0


if __name__ == "__main__":
    sys.exit(main())
