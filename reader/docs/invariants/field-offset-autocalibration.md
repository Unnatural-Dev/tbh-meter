---
type: invariant
description: "The four FRAGILE field offsets (Monster.STAGE_KEY, UnitHealthController.HP_MAX, StageManager.HERO_LIST, Unit.CACHE) are AUTO-CALIBRATED live at attach by semantic signature, because the fields are obfuscated (random names per build) and shift on almost every recompile. The static values in config/offsets.py are the FALLBACK, kept when a signature can't be found live. This is why those offsets no longer need a hand-patch every game update."
symptoms:
  - "stageKey unread post-update"
  - "stage unresolved after a new version"
  - "party live off / StageManager unresolved after update"
  - "dps 0 / hp_max 0 after update"
  - "offset shifted on recompile"
  - "hand-patch offsets every version"
  - "1.3.0 / 1.2.8 / 1.00.27 recalibration"
code_anchors:
  - game/field_calib.py::calibrate
  - game/field_calib.py::_wait_for_monsters
  - game/field_calib.py::discover_stage_key
  - game/field_calib.py::discover_hp_max
  - game/field_calib.py::discover_hero_list_and_cache
  - meter_windows.py::_autocalibrate_fields
  - config/offsets.py::Monster.STAGE_KEY
  - config/offsets.py::Unit.CACHE
  - config/offsets.py::FieldCalibWindows
guarded_by:
  - tests/test_field_calib.py::test_discover_stage_key_by_catalog
  - tests/test_field_calib.py::test_discover_hp_max_ge_current
  - tests/test_field_calib.py::test_discover_hero_list_and_cache
  - tests/test_field_calib.py::test_calibrate_keeps_static_on_no_signal
  - tests/test_field_calib.py::test_stage_key_static_wins_over_coincidental_match
  - tests/test_field_calib.py::test_stage_key_tie_between_non_static_slots_keeps_static
  - tests/test_field_calib.py::test_stage_key_rejects_slot_with_varying_catalog_values
  - tests/test_field_calib.py::test_stage_key_scan_never_reads_obscured_cache
  - tests/test_field_calib.py::test_hp_max_rejects_copy_of_current
  - tests/test_field_calib.py::test_hp_max_static_wins_over_earlier_qualifying_slot
  - tests/test_field_calib.py::test_hp_max_full_health_static_wins_over_larger_float
  - tests/test_field_calib.py::test_hero_list_ignores_junk_catalog_keys
  - tests/test_field_calib.py::test_hero_list_static_pair_wins
  - tests/test_field_calib.py::test_hero_list_solo_tie_is_ambiguous
  - tests/test_field_calib_wave_gap.py::test_calibrate_retries_through_wave_gap
  - tests/test_field_calib_wave_gap.py::test_calibrate_keeps_static_when_list_stays_empty
  - tests/test_field_calib_wave_gap.py::test_calibrate_does_not_retry_without_msm
---

# Field-offset auto-calibration (the four fragile offsets)

## The problem

IL2CPP **class resolution** is already name-based and scan-based, so it survives a game update on
its own (gold/save/catalogs keep resolving on an unknown build). But four **field offsets** are read
at fixed positions and shift on almost every recompile, because the fields are **obfuscated** —
their IL2CPP names are randomized per build (`ud.tl`-style), so they can't be found by name in the
metadata. This is the documented blind spot. Historically each update needed a human to live-probe
them and hand-patch `config/offsets.py` (1.00.14, 1.00.27, 1.2.8, 1.3.0…), each time the same ritual.

The four:

| Field | What it drives | Symptom when wrong |
|---|---|---|
| `Monster.STAGE_KEY` | live stage key → act/stage/difficulty/total_mobs | "stageKey unread", stage "?" |
| `UnitHealthController.HP_MAX` | DPS (Σ HP drops) | dps 0 |
| `StageManager.HERO_LIST` | the live deployed party | "party live off (StageManager unresolved)" |
| `Unit.CACHE` | HeroRuntime → party identity + live level/xp | empty party, no live xp |

## The rule

`game/field_calib.py` **discovers these four at attach time by SEMANTIC SIGNATURE** — what the field
*contains*, not where it sits — and applies them in-memory over the static offsets. It runs in
`meter_windows._autocalibrate_fields` after BOTH resolve paths (fast and scan), so the offsets are
correct before any reading. It piggybacks on structures the resolver already produced
(`msm`, `sm_list`, `stage_info`, `hero_cat`) → **no extra scan**.

Signatures:

- **STAGE_KEY** — the int32 on a live monster whose value is a key in `stage_info`, confirmed on ≥ half
  of a few sampled monsters, with ONE key outnumbering every other catalog value at that offset (every
  mob of a stage has the same one; a coincidental catalog-valued field varies by mob).
  The window skips `Monster.CACHE_OBSCURED` (see [[invariants/obscured-data-offlimits]]).
- **HP_MAX** — the float in the HealthController that is `> 0` and `≥ HP_CURRENT(0x40)` on ≥ half the
  sampled monsters. A plausible static offset is kept even when every sampled monster is at full HP.
  A non-static candidate must also be strictly above current on at least one sample (rejects copies
  of current); among those candidates the smallest offset wins.
- **HERO_LIST + CACHE** (discovered together) — a `StageManager` array of 1..12 ptrs whose elements, at
  some cache offset, resolve `HeroRuntime.INFO → HeroInfoData.HERO_KEY` to a PLAUSIBLE `hero_cat` key
  (100..9999; the catalog scan also yields junk keys like 1, 2, 4, 8, 65535); requires every element to
  match AND ≥ 2 distinct hero keys (defeats the "same key repeated" false positive). The static pair
  matching on any StageManager ends the scan.

**Static first.** If the static offset still satisfies its signature it is kept, even when another slot
in the window also qualifies. For stage-key and hero-list/cache discovery, a non-static slot wins only
as the UNIQUE best match; ties keep the static. HP-max discovery uses the smallest qualifying
non-static offset as described above.

## Invariants (do not break these)

1. **Static offsets stay as the FALLBACK.** A signature not found live (e.g. the game is not in combat)
   keeps the static value — auto-calibration NEVER makes a working build worse. `config/offsets.py`
   remains the single source of truth for the baseline and must still be hand-updated when a live
   recalibration is done (it seeds the scan windows and the fallback).
2. **Read-only.** Discovery writes nothing to the game and nothing to disk; it only mutates the offset
   classes in memory for the running process.
3. **Scan windows are generous on purpose.** Each `FieldCalibWindows` range (in `config/offsets.py`,
   next to the offsets it brackets) spans several recompiles' worth of
   drift around the current value. If a future build shifts a field OUTSIDE its window, widen the
   window (and update the static fallback) — that is the only maintenance left.
4. **Any reader of these offsets must read them dynamically**, not bake a fixed position into a struct
   read. `game/models.live_monsters` spans `HP_CURRENT..HP_MAX` computed from the live offsets, so a
   shifted `HP_MAX` is followed automatically.

## Attach during a wave gap

The monster list is validly EMPTY between waves (about 16% of 10 Hz samples on 1.3.2), and a
single attach-time sample silently kept `Monster.STAGE_KEY` and `HP_MAX` static on 9 of 27 logged
resolves ("kept static (signature not found live)"). `calibrate` therefore first waits, bounded
(`MONSTER_RETRY_ATTEMPTS` x `MONSTER_RETRY_INTERVAL`, about 6 s), while the list reads EMPTY. An
unreadable or null list returns at once; if no monster appears the static fallback stays.

## Why this doesn't eliminate the static offsets

The discovery needs a starting window and a safe fallback; the obfuscated fields have no name to
resolve from. So this reduces the per-update work from "investigate + hand-patch 4 offsets + reseed"
to, in the common case, **nothing** — and, when a field leaves its window, a one-line window widen.
It does not make the game self-describing.
