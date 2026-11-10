---
type: invariant
description: "meter_windows.py is a thin ORCHESTRATOR: no inline memory reads outside the scaffolding (log-event loop, manager validation, re-attach); a new metric/capture lives in metrics/ or game/, offsets only in config.offsets. agent_windows.py is the SIBLING debug entry-point, self-contained."
symptoms:
  - "where to put a new metric"
  - "where to add a new capture"
  - "where to add a new metric"
  - "inline read in the orchestrator"
  - "inline memory read in orchestrator"
  - "reader.ri32 in meter_windows"
  - "stray rptr/ri64/read_string in meter_windows"
  - "magic number in meter_windows"
  - "which is the production entry-point"
  - "meter_windows vs agent_windows"
code_anchors:
  - meter_windows.py
  - meter_windows.py::run
  - meter_windows.py::_pick_list_singleton
  - metrics/run_boundary.py::RunBoundary
  - agent_windows.py
  - agent_windows.py::op_dump
guarded_by:
  - tests/test_run_lifecycle_predicates.py::TestIsPartial::test_zero_damage_success_is_always_partial
---

# Orchestrator purity (meter_windows.py is thin)

The reader has **two entry-points at the root**, and they have OPPOSITE roles — don't confuse them:

- **`meter_windows.py` — PRODUCTION.** This is what CI freezes into `tbh-reader.exe` and the Electron
  app spawns as a sidecar. Writes `raw/<id>.json` (RAW record per run → the app's converter turns it
  into `logs/<id>.json`) + `live.json` (RAW snapshot of the current run, overwritten ~1x/s → the app
  cooks the overlay) + `meter.log`. Emits **RAW for both streams**; all derivation (dps/label/format)
  belongs to the app ([[invariants/metric-fallback-chains]] + the live-meter pipeline). **It's a THIN
  orchestrator** — zero inline memory reads; every read goes through the metric/game modules.
- **`agent_windows.py` — DEBUG/inspection, self-contained.** You run it ONCE by hand, with the game
  open, and it sits listening to `output/agent_cmd.json` → runs an `op_*` → replies in
  `output/agent_resp.json`. It's a memory inspector (hunt an offset, decode ObscuredX, find the
  wallet). It ships in NO build. Touching it changes nothing about the product.

**The rule (production only, `meter_windows.py`):** the orchestrator builds ONE `shared.memory.Reader`
and **delegates** everything to the isolated logic — `shared.memory` (attach/regions/scan), `il2cpp`
(resolve classes), `game.*` (domain: save/build/models), `metrics.*` (gold/xp/dps/events). Offsets
come **only** from `config.offsets` (the "bible"). Therefore: **a NEW metric or capture lives in
`metrics/` or `game/`, NEVER inline in `run()`.** And **no literal offset** (`0x…`) in
`meter_windows.py` — it imports the symbol from `config.offsets`.

## What "scaffolding" is (the legitimate exception)

Inline memory reads (`reader.rptr`/`ri32`/`ri64`/`read_string`) IN `meter_windows.py` are only
acceptable in the **run-lifecycle scaffolding**, which is the orchestrator's responsibility and
no one else's:

- **Log-event detection loop** (in `run()`): scans the new `LogManager.LOG_LIST` each tick and
  classifies every entry by its K-class (`StageClearLog`/`StageFailedLog`/`GetBoxLog`/`HeroDieLog`/
  `ResurrectionLog`) to fire `close_run`/drops/deaths/revives. This IS lifecycle orchestration —
  see [[invariants/run-lifecycle]] (and log-event detection).
- **Structural manager validation** (`_pick_list_singleton`/`_valid_list_size`): pick the live
  `LogManager`/`MonsterSpawnManager` instance out of the scan's false positives — see
  [[invariants/instance-selection]].
- **Re-attach / stage-reload detection**: read `LOG_LIST`/`DeadMonsterUnit` to learn that the game
  closed (reads failing) or the stage reloaded. Only the READ is scaffolding: what a dropped count
  MEANS (the reload rule, the stage-switch rule, adoption, the per-run kill/damage ledger) is decided
  by `metrics/run_boundary.py::RunBoundary`, which the loop feeds — see [[invariants/run-lifecycle]].

Note that even this scaffolding **does NOT invent an offset**: it uses `List.SIZE`/`List.ITEMS`/`Array.DATA`
etc. from `config.offsets` ([[invariants/offsets-single-source]]).

## Why this is an invariant (not just style)

Symptom of the anti-pattern: you need a new value (e.g. a new aggregate, a new per-hero stat) and
**put the read inline in `run()`** — either by hardcoding a literal `@0x`, or by mixing the parse
into the loop. Result: the logic becomes **untestable** (trapped in a `while True` that touches the
game's memory, which doesn't run on Mac), the offset **drifts out of sync** with `config.offsets`,
and the orchestrator bloats to the point where nobody understands the lifecycle anymore.

Proof the boundary works: **the pure decisions the orchestrator legitimately owns are extracted as
pure functions and tested in isolation** — `_should_skip_run`, `_is_partial`, `_pick_list_singleton`,
`_read_catalogs` (touching no process at all, they run on Mac via `MockReader`), plus `RunBoundary`
(run-end rules + ledger, fed by the loop). That's exactly how
the `_is_partial` drift (the note said `== 0`, the code is `total_damage <= 0`) got **covered by a
test** instead of becoming a silent bug. If a "metric" of yours can't be tested this way, it's in the
wrong place — move it to `metrics/` or `game/`.

**Where to put the new thing:**
- new derived metric (damage/dps/xp/gold/progress) → `metrics/` (it receives the `reader` and reads there).
- new domain data (save/party/build/monsters/stage-key) → `game/`.
- new class/instance to resolve → `il2cpp/resolver` (and the orchestrator's `TARGETS`/fast-path).
- new offset → **only** `config.offsets`; `meter_windows.py` imports the symbol.
- `meter_windows.py` then **calls** the new function and stitches the result into the `rec`/overlay.

## Related
- [[invariants/offsets-single-source]] — the orchestrator imports an offset, never hardcodes `0x…`.
- [[invariants/instance-selection]] — manager validation is the legitimate scaffolding.
- [[invariants/run-lifecycle]] — the log-event loop is lifecycle orchestration.
See also: [[invariants/log-event-detection]] (how the loop classifies each entry by K-class) · [[invariants/metric-fallback-chains]] (the new metric comes in with its fallback chain) · [[invariants/memory-safety]] (the never-raises reader the scaffolding assumes) · [[guides/map-new-value]]/[[guides/add-runs-field]] (the end-to-end recipe for a "new value") · [[invariants/obscured-data-offlimits]] (agent_windows op_obs is the only place that decodes ObscuredX)
