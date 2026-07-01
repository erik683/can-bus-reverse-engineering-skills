# Collection/dump of ideas for future improvements or fixes by Claude/ChatGPT 

## Recommendation up front

Begin with a ground-truth regression harness built from mustang-test-logs/, then use it to fix the big-endian gap in bitsearch — that combination serves "better decoding" directly and makes every later refinement measurable instead of anecdotal.

1. Turn the Mustang logs into an end-to-end regression fixture (do this first). You now have real raw+HPT log pairs sitting untracked, with known answers recorded in memory and in the docs (RPM on 0x201 big-endian bytes 0–1 scale 0.25, speed/pedal on 0x201, ECT/MAP on 0x420, Δ≈+5.9 s, and known negatives like the six torque PIDs). common.py and bitsearch.py have synthetic selftests, but correlate.py — which just gained 100+ lines including the signed-lag-rerun bugfix — has none, and neither do filter_regime.py, align_reference.py, or scanlog_reference.py. The signed-margin bug fixed in b3bafa6 is exactly the class of regression a fixture asserting "torque candidate margin is positive inside overrun" would have caught before it shipped. A script that runs align → correlate → bitsearch → build_dbc → verify on (possibly subset) logs and asserts the known geometries, scales, and the known-negative outcomes is the single highest-leverage refinement because it de-risks all the others. (Decide whether the logs belong in the repo or in .gitignore with a downloadable fixture — they're currently untracked either way.)

2. Big-endian parity in bitsearch (the top decoding fix). At bitsearch.py:272-275, little-endian gets a full start-bit × length search over active bits, but big-endian gets only byte-aligned widths 1–4. SKILL.md codifies the consequence twice as a human workaround: "bitsearch's Intel-only start-bit search under-reads a big-endian wide field as just its high active bits — trust correlate's --order big geometry instead." That means the tool you call "the field of record" isn't, on Motorola-ordered buses — which is most of Ford/GM broadcast traffic, i.e. your own test vehicle. Extending the BE enumeration to arbitrary start bits, making the parsimony rule and resolution-refinement/cascade endianness-fair, and then deleting the doc workaround is the clearest "better decoding" win. The existing bitsearch.py --selftest plus the fixture from step 1 (0x201 RPM is a real BE 16-bit field) give you the safety net.

3. Promote proxy detection from opt-in to default (false positives). The new --covariate margin and proxy_suspect flag only fire when the operator already suspects a proxy and manually builds sidecars + a regime mask. Two cheap generalizations: the high-Spearman/low-R² proxy_suspect tell needs no covariates at all, so compute and print it on every correlate run; and verify.py — the gate — knows nothing about covariates, so a proxy that survives correlate sails through. Relatedly, SKILL.md calls cross-capture validation "the strongest confirmation" but no script supports it: verify takes one trace. Adding a second-trace mode (verify the finished DBC against a different drive and gate on both) operationalizes your strongest false-positive killer instead of leaving it as advice.

4. Automate the documented manual escapes (false negatives, and decoding quality). Several SKILL.md notes are really script TODOs: "if the lag pins at the ±max-lag boundary, widen to 6–8" (correlate/bitsearch can detect a boundary-pinned optimum and auto-widen or at least say so explicitly); "a slow monotonic ramp tilts the fit — confirm byte-by-byte and force the scale" (build_dbc could detect a monotonic reference and report split-window scale stability instead of relying on the operator noticing); and the default --max-len 24 silently excluding 32-bit fields. These are the main places a real signal currently gets missed or mis-scaled unless a human remembers the caveat.

"The ordering matches your priorities: items 1–2 improve decoding correctness directly, item 3 attacks false positives, item 4 mops up the false-negative escapes. I'd resist refining filter_regime.py/the divergence workflow further right now — it's one day old and needs mileage on more vehicles before you know which knobs matter."

## Curated DBC Overlay Support

### Summary

Add a curated overlay layer to `combine-dbc`: per-signal folders generate the base DBC, optional `curated.dbc` adds human metadata and hand-curated additions, and `<app>.dbc` remains a generated artifact.
Defaults chosen: auto-load `<app-dir>/curated.dbc`, allow overlay-only messages, warn on conflicts while keeping generated definitions, and fail safe if an existing hand-edited output would otherwise be overwritten.

### Key Changes
Update the combine script CLI:
Add `--overlay PATH`, `--no-overlay`, and `--allow-curated-overwrite`.
Resolve relative overlay paths against the app dir.
Treat missing explicit overlays as errors; treat absent default `curated.dbc` as normal.
Apply overlay after generating the base DBC:
Union `BU_` nodes.
Apply message names, senders, comments, cycle time, and max DLC.
Apply signal receivers, comments, and `VAL_` choices to matching signals.
Treat `Vector__XXX` as placeholder, so it does not replace real tx/rx metadata.
Add overlay-only messages and non-overlapping overlay-only signals.
If matching signal geometry/scale/unit differs, warn and keep generated math.
Add a data-loss guard:
If no overlay is active and output exists, compare it with the fresh generated base.
If it contains curated-looking metadata, extra messages/signals, or cannot be parsed, exit nonzero unless `--allow-curated-overwrite` is passed.
Update docs in the skill and README:
Show `curated.dbc` as optional input.
State that `<app>.dbc` is generated and should not be hand-edited.
Document precedence, warnings, fail-safe behavior, and usage examples.
Test Plan
Add stdlib `unittest` tests with temp app dirs and tiny DBC fixtures.
Cover generated-only output unchanged.
Cover auto `curated.dbc` and explicit `--overlay`.
Cover nodes, tx/rx, comments, `VAL_`, overlay-only messages, and non-overlapping overlay-only signals.
Cover conflict warnings with generated definitions preserved.
Cover legacy guard failing safe, and bypass only with `--allow-curated-overwrite`.
Assumptions
Per-signal DBCs remain authoritative for decoded signal math.
`curated.dbc` is authoritative for human metadata.
Existing Ford hand-curated metadata should be moved into `decoding-output/ford-mustang-2006/curated.dbc` before re-running; curated scale/math changes belong in the per-signal DBCs.
v1 will not raw-text-preserve arbitrary custom DBC sections, except where `cantools` preserves them on copied overlay-only messages.