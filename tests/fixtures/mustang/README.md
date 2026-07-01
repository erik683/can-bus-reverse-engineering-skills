# Mustang regression fixtures

Time-window subsets of two independent 2006 Ford Mustang GT HS-CAN (500 kbit/s)
drives, each recorded twice in parallel: the raw bus as a GVRET/SavvyCAN CSV
export, and an HP Tuners scan-tool log of the same drive on its own clock.
They are the ground-truth data for `tests/regression_mustang.py`.

| fixture | source log (not in repo) | window (CAN clock) | Δ (scan→CAN) |
|---|---|---|---|
| `drive1_gvret.csv.gz` | `2006_Ford_Mustang_GT_HSCAN.csv` | 145–255 s | +5.93 s |
| `drive1_hpt.csv.gz` | `2006_Ford_Mustang_GT_HSCAN_HPT.csv` | (window − Δ) ± 5 s | |
| `drive2_gvret.csv.gz` | `2006_Ford_Mustang_GT_HSCAN_GVRET_2.csv` | 575–666 s | +3.15 s |
| `drive2_hpt.csv.gz` | `2006_Ford_Mustang_GT_HSCAN_HPT_2.csv` | (window − Δ) ± 5 s | |

The windows were picked (see `make_fixtures.py`) to contain strong RPM/speed
dynamics, idle rest (exercises the zero-anchor re-fit), and engine-overrun
segments (pedal < 2 %, RPM > 1800 — the divergence regime the torque
negative-control tier needs; drive2 has ~29 s of it).

## Ground truth encoded in the harness

Broadcast (must decode):

- `0x201` bytes 0–1, big-endian, ×0.25 → **engine RPM**
- `0x201` bytes 4–5, big-endian, ×0.01 km/h − 100 (≈0.006214 mph/bit, offset
  ≈ −62.1 mph vs the HPT mph reference) → **vehicle speed**
- `0x201` byte 6, ×0.5 → **accelerator pedal %**
- `0x215` four 16-bit big-endian wheel speeds (same encoding as vehicle
  speed) — appear as the genuine tie-group behind `0x201` in correlate

Not broadcast (must stay negative):

- **ETC Torque Request** (and the other HPT torque channels): ECM-internal,
  read via Ford `0xA0/0xA1` enhanced diagnostics. Inside the overrun
  divergence regime nothing on the bus locks on it (r² stays ≪ 0.9) and the
  airflow/speed proxies get `proxy_suspect`-flagged.

Cross-capture: the drive1-built RPM DBC must re-decode drive2 at
Spearman ≥ 0.99, slope ≈ 1.

## Regenerating

Needs the full logs locally (gitignored, ~100 MB):

    .venv/bin/python tests/fixtures/mustang/make_fixtures.py

If you change the windows, re-calibrate the harness expectations — the
divergence tier's retained-% band and the overrun statistics are
window-dependent.
