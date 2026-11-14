---
type: invariant
description: "Run lifecycle: the reader INFERS start/end from memory (LogManager's LOG_LIST grows; closes on StageClearLog/StageFailedLog matched by KLASS-POINTER) — boundary detection is ITS job — and emits EVERY run to raw/<id>.json. The skip (<30s exc. stage 10) and partial (success w/ capture <95% OR damage<=0) predicates became the accounting SPEC applied by the CONVERTER (app); the reader no longer drops (skip ≠ vanish). SUCCESS only delays the WRITE by PENDING_CLOSE_GRACE (pending-close) to absorb the boss box the game logs ~0.6s AFTER the clear — otherwise the chest landed in the NEXT run."
symptoms:
  - "run does not close"
  - "run does not appear"
  - "run skipped"
  - "x-10"
  - "stage 10 boss"
  - "partial capture"
  - "partial dropped"
  - "run does not count"
  - "short run"
  - "chest in wrong run"
  - "blue chest in abandoned run"
  - "boss box wrong run"
  - "chest credited to next run"
  - "drop after clear"
  - "kills above stage total after a fail"
  - "overcount after a failed run"
code_anchors:
  - meter_windows.py::new_run
  - meter_windows.py::close_run
  - meter_windows.py::_should_skip_run
  - meter_windows.py::_is_partial
  - meter_windows.py::PARTIAL_CAPTURE_MIN
  - meter_windows.py::PENDING_CLOSE_GRACE
  - meter_windows.py::TRAILING_BOX_TIERS
  - meter_windows.py::_new_pending
  - meter_windows.py::flush_pending
  - meter_windows.py::_box_belongs_to_pending
  - meter_windows.py::_absorb_drop
  - meter_windows.py::_drop_counts
  - meter_windows.py::LogScanCursor
  - metrics/run_boundary.py::RunBoundary
  - metrics/run_boundary.py::RunBoundary.close
  - metrics/run_boundary.py::RunBoundary.observe_stage
  - metrics/run_boundary.py::RunBoundary.observe_dead_count
  - metrics/run_boundary.py::RunBoundary.should_abandon
  - metrics/run_boundary.py::ADOPT_GRACE
  - metrics/run_boundary.py::RELOAD_TOLERANCE
  - metrics/dps.py::DpsTracker
  - metrics/anomaly_dump.py::AnomalyDump
  - metrics/anomaly_dump.py::trigger_reason
  - config/offsets.py::LogManager.LOG_LIST
asserts:
  - meter_windows.PARTIAL_CAPTURE_MIN == 0.95
  - meter_windows.PENDING_CLOSE_GRACE == 3.0
  - metrics.run_boundary.ADOPT_GRACE == 3.0
  - metrics.run_boundary.RELOAD_TOLERANCE == 2
guarded_by:
  - tests/test_run_lifecycle_predicates.py::TestShouldSkipRun::test_stage_x10_under_30s_is_kept
  - tests/test_run_lifecycle_predicates.py::TestIsPartial::test_zero_damage_success_is_always_partial
  - tests/test_run_lifecycle_predicates.py::TestBoxBelongsToPending::test_boss_box_with_pending_goes_to_pending
  - tests/test_meter_windows.py::TestNewPending::test_deadline_is_now_plus_grace
  - tests/test_meter_windows.py::TestFlushPendingRec::test_flushed_json_contains_absorbed_boxes
  - tests/test_raw_record.py::test_absorbed_boss_box_lands_inside_drops_envelope_without_shape_change
  - tests/test_log_scan.py::TestCapRotation::test_rotation_with_size_pinned_detects_new_tail_entry
  - tests/test_log_scan.py::TestClearThenBoxOrderingAcrossRotation::test_clear_then_box_separate_rotated_ticks
  - tests/test_run_boundary.py::TestRecordedFailBoundaries::test_kills_and_damage_on_each_side_of_the_fail
  - tests/test_run_boundary_rules.py::TestReloadRule::test_tolerance_of_two
  - tests/test_run_boundary_rules.py::TestStageAdoption::test_does_not_readopt_at_or_after_grace
  - tests/test_run_boundary_rules.py::TestShouldAbandon::test_abandoned_close_uses_the_old_stage_key
  - tests/test_run_boundary_rules.py::TestFailedRunLeftovers::test_leftovers_leaving_before_the_first_spawn_are_not_kills
  - tests/test_run_wiring.py::test_only_a_failed_close_keeps_its_listed_monsters_out_of_the_next_run
  - tests/test_run_wiring.py::test_reattach_stamps_start_after_resolve_and_before_new_run_reads
  - tests/test_run_wiring.py::test_reattach_does_not_leak_interrupted_run_into_next_run
  - tests/test_run_wiring.py::test_reattach_resolution_read_error_retries_and_resumes_real_output
  - tests/test_run_wiring.py::test_reattach_carrier_read_error_after_resolution_escapes_run
  - tests/test_run_wiring.py::test_startup_resolution_read_error_escapes_run
  - tests/test_run_wiring.py::test_abandoned_record_carries_ledger_mobs_damage_duration_and_old_stage
  - tests/test_run_wiring.py::test_success_close_emits_ledger_and_live_line_shows_pre_close_values
  - tests/test_run_wiring.py::test_terminal_reload_then_missing_stable_or_increasing_count_keeps_next_ledger
  - tests/test_run_wiring.py::test_terminal_reload_retains_baseline_for_fresh_post_grace_drop
  - tests/test_run_wiring.py::test_repeated_terminal_logs_after_reload_close_separate_ledgers_once
  - tests/test_run_wiring.py::test_reattach_after_terminal_reload_discards_old_counter_and_keeps_new_close
  - tests/test_run_wiring.py::test_anomaly_dump_flag_writes_the_ticks_of_an_overcounted_success
  - tests/test_run_wiring.py::test_without_the_flag_nothing_is_written_and_records_are_unchanged
  - tests/test_anomaly_dump.py::test_unknown_values_never_trigger
  - tests/test_anomaly_dump.py::test_metadata_errors_still_count_published_archives_toward_the_cap
---

# Run lifecycle

A **run** is one stage attempt. The reader gets no "started/ended" event: it
**infers the lifecycle from memory**, watching the game's log list every tick.

## Boundary: new LogManager.LOG_LIST entries (tracked by OBJECT POINTER, rotation-aware)

Each tick the loop reads `LogManager.LOG_LIST` (the "bible" `offsets.py` marks this offset as the
*run boundary*) and asks `LogScanCursor` for the **NEW entries** since the previous tick; it then
looks at each one's **klass-pointer** (the entry's first qword = pointer to its class). These new
entries carry the terminal events — there is no separate "start" signal: the next run simply begins
when the previous one closes.

**Identity is the entry's OBJECT POINTER, never its index.** The list is **capped at 2000** (see
[[invariants/log-event-detection]] / `metrics/events.py`); once full the game evicts from the **HEAD**
to stay at the cap, so absolute indices shift DOWN on every append. The original absolute-index cursor
(`[last_size, size)`, fired only while `size` grew) desynced PERMANENTLY at saturation: with `size`
pinned at the cap it never fired again, so every `StageClearLog` was missed — runs closed only via the
`abandoned` path (`clear_time=0`) and one open run accrued several stages (live `mobs` far above the
stage total). A reader/game **restart did NOT fix it** (the game process keeps the saturated list; an
index cursor just re-seeds to the cap). `LogScanCursor` instead tracks the last-processed entry by its
heap pointer: each tick it scans the tail backward, collecting unseen pointers and stopping at the first
already-seen one (bounded per tick by the scan cap), so head-eviction can't desync it and a tail append
is detected even when `size` never changes. `seed()` establishes the baseline at attach/re-attach
WITHOUT replaying the pre-existing backlog. The cursor yields **oldest→newest**, which is what the
pending-close below relies on (the `StageClearLog` is processed BEFORE its trailing boss `GetBoxLog`).

## End: StageClearLog (success) / StageFailedLog (fail) by klass-pointer

The close is decided by comparing the new entry's klass-pointer against `sc_class` /
`sf_class` (resolved once for `StageClearLog` and `StageFailedLog`):

- klass == `sc_class` → `close_run("success", ...)` — reads `CLEAR_TIME` from the log.
- klass == `sf_class` → `close_run("fail", ...)` — reads current/total wave from the log.

(The same entries also carry `GetBoxLog`/`HeroDieLog`/`ResurrectionLog`, matched by the
same klass-pointer pattern.) There is also a third
outcome — `close_run("abandoned", ...)` — when the stage reloads (DeadMonsterUnit drops) or the
player switches stage without clearing/failing, once past the initial grace window (that rule lives in
`metrics/run_boundary.py`, see the next section).

## Run-end rules + kill/damage ledger: `metrics/run_boundary.py::RunBoundary`

The orchestrator READS memory (monster snapshot, live stage key, `DeadMonsterUnit` size, `LOG_LIST`) and
feeds the observations to ONE `RunBoundary`; the module DECIDES and keeps the per-run ledger. It owns:

- the ledger — a fresh `DpsTracker` per run: `kills`, `damage`, `dps(now)`, `peak_dps`, `alive`; an invalid
  snapshot (`None`) leaves the state untouched. After a FAILED run (`close(now, failed=True)`) the
  monsters still listed go to the next ledger as leftovers: one that leaves before that run's first
  own spawn is not a kill (its remaining HP still counts as damage), and the first spawn makes the
  rest ordinary monsters; a leftover no longer listed alive has left, and its address is a new
  monster's. Clears, abandons and re-attach carry nothing (#28);
- the run `start` and the adopted `stage_key`: the live stage is adopted while none is adopted yet, or
  during `ADOPT_GRACE` after the run starts (auto-replay/advance right after a clear); keys outside the
  catalog are never adopted;
- the **reload** rule (`observe_dead_count`): `DeadMonsterUnit` is cumulative and only a manual restart of
  the stage drops it — a drop of MORE than `RELOAD_TOLERANCE` is a reload; a failed read (`None`) is
  neither a reload nor a new baseline; the baseline survives a close and is cleared only by
  `reset_dead_baseline()` at re-attach;
- the **switch** rule and the abandon decision (`should_abandon`): once past the grace and with a stage
  adopted, `reload OR (live key is another catalogued stage)`. The orchestrator evaluates it only on a tick
  where no log closed the run, and closes with the OLD `stage_key`;
- `close(now, failed=False)` — returns the immutable `ClosedRun` (kills, damage, measured, stage_key)
  that `close_run` puts in the record/summary, and starts the next run's ledger (holding the failed
  run's leftovers when `failed`); `restart(now)` discards the run in progress at re-attach.

### Caller ordering and transient reload observations

`run()` feeds the dead count on **every tick**, including `None`, before log
acquisition and abandonment decisions. Each observation replaces the transient
reload decision; `None` clears that decision without replacing the cumulative
baseline. A success/fail log coincident with a reload closes the current ledger,
and `closed` suppresses the same tick's abandonment check. The next observation
must happen before deciding whether to abandon the new ledger.

`close()` does not promise to clear a pending reload decision for arbitrary callers
that omit this observation step. The real caller regressions cover missing,
stable and increasing next counts, repeated terminals, a fresh post-grace drop,
and reattachment. This documents existing ordering, not a demonstrated overcount
fix. Reattachment discards the interrupted ledger, reseeds the real log cursor,
and resets the previous attachment's cumulative dead baseline.

What does NOT move: closing on `StageClearLog`/`StageFailedLog` stays in the orchestrator's log scan (it
needs the memory reads), and the run's other state (gold/xp/party/drops/deaths) stays in `new_run()`.
A new end-of-run rule goes in the module (pure, testable), not inline in `run()` —
[[invariants/orchestration-purity]].

## Anomaly dump (opt-in diagnostics): `metrics/anomaly_dump.py::AnomalyDump`

`--anomaly-dump` (default OFF, the app never passes it) keeps a bounded ring — `ceil(360 * hz)` ticks: a whole run plus the boundary before it — of what
the loop ALREADY reads each tick: time, the monster snapshot (`[addr, hp, hp_max]`, `[]` between waves, `None` if
invalid), the raw live stage key, the dead count, and the log classes that closed a run on that tick. It adds NO
memory read. On each run close `trigger_reason` judges a SUCCESS: `mobs > total_mobs`, or `duration - clear_time > 5`
(both known; an unknown input never triggers). A hit writes `<output>/anomalies/<run_id>.ticks.jsonl.gz` (the ring,
which keeps rolling) plus `<run_id>.json` (outcome, counts, reason, reader version, hz). Capped per process
(`MAX_DUMPS`): each published archive consumes a slot even if its summary fails to write.
Every I/O error is logged via `diag()` and swallowed. Pure module fed by `run()` — the rule lives
there, not inline ([[invariants/orchestration-purity]]); the flag never changes `raw/<id>.json`.

## Pending-close: the boss box arrives AFTER the clear (only the success WRITE is delayed)

Proven live (1.00.11): the game logs the **boss chest** (`GetBoxLog` with `MONSTER_TYPE` in
`TRAILING_BOX_TIERS` — StageBoss/ActBoss) **~0.6s AFTER the `StageClearLog`**, in a SEPARATE
growth of the `LOG_LIST`. Since the close had already reset `R`, the chest landed in the **NEXT**
run — invisible while grinding the same stage, glaring when the next one was abandoned (blue chest
in a 0s "invalid" run, and the real clear with no drop).

The rule: **the close does NOT wait** — reads, metrics, `ts_ms` (the identity) and
`new_run()` happen at the close, as always (delaying the close would leak the next run's first
seconds into the record on auto-replay, worse than the bug). What changes is that a `success`
close **does not write the file right away**: the record stays PENDING for up to
`PENDING_CLOSE_GRACE` and any boss `GetBoxLog` that arrives in the meantime (even in the SAME
batch of entries) is absorbed into it (`_box_belongs_to_pending` routes; `_absorb_drop` mutates
the value INSIDE the drops envelope — `build_raw_record` doesn't copy the list, so that's what
goes out in the JSON). A gray (mob, tier `Monster`) drops DURING the stage → keeps going to the
current run's `R["drops"]`. A boss box **with no** pending (e.g. attached right after a clear) →
current run + WARN in meter.log; a real chest is never dropped. `fail`/`abandoned` write right
away (a boss box only follows a clear).

The pending state is born in `_new_pending` (rec + path + deadline `now + PENDING_CLOSE_GRACE` +
a fresh absorbed list — the constructor is shared with the tests so the shape doesn't drift).
`flush_pending` writes the pending (same atomic write) and runs at **every** window exit point:
deadline expired, checked AFTER the tick's `LOG_LIST` scan (a boss box that surfaces on the SAME
tick as the expiry is still absorbed — effective window `PENDING_CLOSE_GRACE` + ≤1 tick); the
**top of `close_run`** (any status — record order preserved, never two pendings); the
game-closed/re-attach path (the pending is a COMPLETE run — the game closing right after the
clear can't make it vanish); and the `run()` finally. ACCEPTED trade-off: a hard kill (AV
SIGKILL) inside the window loses that record. **Live**: live.json's chest count sums the current
run + ABSORBED (`_drop_counts`) — the late boss box RAISES the count with the `stage_key` still
live on the cleared stage (the rising edge the app's cooldown-tracker/drop-notifier detect);
post-flush it drops (baseline in the app, no event). The pending record's full drops do NOT enter
(its grays would hang on the overlay).

## SKIP — `_should_skip_run(measured, clear_time, stage)` (now the converter's SPEC)

A short run **does NOT COUNT** — but **the reader NO LONGER drops it**: it
emits EVERY run to `raw/<id>.json` (skip ≠ vanish; otherwise the user thinks the meter broke and
the app can't mark it as "ignored"). The accounting is applied by the **converter** (app), over
the record's raw fields. `_should_skip_run` stays here as the **canonical drift-tested spec** (the
converter ports it to TS) and is **no longer called** on the emission path. The real rule is:

```
max(measured, clear_time or 0) < 30  AND  stage != 10
```

The `stage != 10` exception keeps the **x-10** (boss-only fight, which can last seconds).
**Careful: `stage` here is the stage NUMBER (`StageNo`), NOT the `EStageType.ACTBOSS`.** These
are DIFFERENT signals: `EStageType` is a stage *type* (value 1) read from another offset; the
predicate compares the number `10` (the `si[1]` derived from the catalog). Don't swap one for the
other — using the type instead of the number here would make normal x-10 runs get dropped.

**Floor in the converter = 15s** (TS constant, non-tunable). The `< 30` here is the reader's
historical value, which the port revisits at **15s**; what the port can NOT lose is the **x-10
exception** (`stage != 10`) — that's the real invariant, not the floor number.

## PARTIAL — `_is_partial(status, clear_time, measured, total_damage)`

**PARTIAL** capture = the meter joined a run **already in progress** (undercounted
damage/gold/xp). It **no longer goes in the record** (`partial` left the raw): the **converter**
derives it from the emitted raw fields (`run_outcome`, `clear_time`, `duration`, `total_damage`) —
same formula — and seals it into `status`. The reader still computes `partial` only to annotate
the summary/console. The real rule (which the converter ports) is:

```
status == "success"  AND  (
    (clear_time >= 30 AND measured < clear_time * PARTIAL_CAPTURE_MIN)   # joined mid-run (<95%)
    OR total_damage <= 0                                                 # success with no damage = lost capture
)
```

`PARTIAL_CAPTURE_MIN` is **0.95** — a clear counts only if the meter captured **≥95%** of it. The
reader (`meter_windows.PARTIAL_CAPTURE_MIN`) and the converter
(`app/src/main/converter/helpers.ts::PARTIAL_CAPTURE_MIN`) **MUST hold the same number**; the
converter is the persisted spec, the reader only annotates `meter.log` / the `[run-close]` diag.

Two points the skill drifted on that the TRUTH (the code + `tests/test_run_lifecycle_predicates.py`)
contradicts:

- the second clause is **`total_damage <= 0`**, NOT `== 0`. Any non-positive damage in a
  success is a lost capture (the game doesn't clear a stage with no damage). This covers the gap
  of x-10s with `clear_time < 30` that skipped the 1st clause and recorded all-zero runs (#163).
- the `clear_time >= 30` gate on the 1st clause is deliberate: x-10 runs (boss, seconds) must
  not be mislabeled as partial — that's why only `<= 0` catches them.

## Attachment is not a run

Startup and re-attach obtain their classes, catalogs, carrier picks and gold handles from
`game/attach.py::prepare`. A successful re-attach replaces that state wholesale before
`rb.restart`, `new_run`, cursor seeding, and the dead-count baseline reset. Preparation does
not own those transitions; the PSD per-run pick remains in `new_run()`.
Re-attach calls preparation with `startup=False`: a resolution read error returns
incomplete and the loop retries until valid preparation resumes run output. Startup
resolution errors and errors after resolution (such as carrier reads) still propagate;
retry must not widen into swallowing preparation failures.

## new_run() initializes ALL the per-run state

`new_run()` is the SOLE source of a run's state and returns the zeroed dict (the kill/damage ledger,
start time and stage adoption are NOT in it — they are `RunBoundary`'s, restarted by `close()`/`restart()`
alongside it): the gold baselines (`gold_start`/`gold_live_start`/`gold_save_start`),
`heroes_start`, the live party (`party_live_start`) + the live xp accumulator (`xp_acc`, seeded
with the party at t=0 — see [[invariants/metric-fallback-chains]]), `build`, `drops`,
`party_seen`, `deaths`/`revives`/`killers`. **Golden rule: every
field that ACCUMULATES during the run (gold/xp delta, deaths, drops) has to be born here** —
otherwise the value leaks from the previous run. When adding a new run field, initialize it in
`new_run` AND emit it in `build_raw_record` (see [[invariants/schema-versioning]] and [[guides/add-runs-field]]).

## Related
- [[invariants/instance-selection]] — the run end depends on the LIVE LogManager's LOG_LIST; if the pick grabbed the dead list, `size` never grows and NO run closes.
- [[invariants/schema-versioning]] — new run field: bump `SCHEMA_VERSION` + init in `new_run` + serialize in `close_run`.
- [[invariants/log-event-detection]] — the klass-pointer matching of the `LOG_LIST`'s new entries (what triggers the close).
- [[invariants/orchestration-purity]] — why the boundary rules + ledger are a pure module (`metrics/run_boundary.py`), not inline in `run()`.
- [[reference/run-data-map]] — the shape of the record `close_run` emits, field by field.
- [[guides/add-runs-field]] — the end-to-end recipe for adding a field to the run.
