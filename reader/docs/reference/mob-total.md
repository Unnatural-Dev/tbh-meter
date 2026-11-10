---
type: reference
description: "The REAL mob total of a full clear: horde = (WaveAmount − Brevity waves) × WaveMonsterAmount, + 1 for the boss. Each Rune of Brevity node removes one whole wave; ignoring it left the overlay mob bar short of full on every stage (3-8 Nightmare 301/346)."
symptoms:
  - "mob bar never fills"
  - "mobs total wrong"
  - "total_mobs too high"
  - "Rune of Brevity"
  - "wave count reduction"
code_anchors:
  - game/waves.py::total_mobs
  - game/waves.py::wave_reduction
  - game/waves.py::read_stage_waves
  - game/build.py::read_runes
  - game/attach.py::write_drop_catalog
  - config/offsets.py::StageInfoData.WAVE_AMOUNT
  - config/offsets.py::StageInfoData.WAVE_MOB_AMOUNT
asserts:
  - game.waves.WAVES_PER_LEVEL == 1
  - game.waves.WAVE_REDUCTION_RUNE_KEYS == (1171, 1242, 1301)
guarded_by:
  - tests/test_waves.py::test_total_matches_measured_kills_with_three_brevity_nodes
  - tests/test_waves.py::test_no_reduction_is_the_historical_total
  - tests/test_waves.py::test_missing_wave_data_falls_back_to_catalog_horde
  - tests/test_waves.py::test_wave_reduction_counts_only_brevity_nodes_with_levels
related: ["[[reference/drop-catalog]]", "[[invariants/orchestration-purity]]", "[[invariants/memory-safety]]"]
---

# Mob total (Rune of Brevity)

## What it is

`stage_info` stores the catalog horde (`WaveAmount × WaveMonsterAmount`) — correct only for an account
with no Rune of Brevity. The account rune tree has three Brevity nodes (`WAVE_REDUCTION_RUNE_KEYS`), each
"Stage Wave Count −1" at its single level, so a full clear kills
`(WaveAmount − reduction) × WaveMonsterAmount` horde mobs, plus the boss.

Oracle (1.3.2, an account with all three nodes): the modal kill count of `counted` successes equals the
formula on every farmed stage — 3-8 Nightmare 301, 3-6 Hell 409, 3-8 Torment 589, 1-1 Normal 8.

## How it flows

- `read_runes` (the same save read the run record's `runes` uses) feeds `wave_reduction` once per run in
  `new_run` — runes change only between runs.
- The per-stage `(WaveAmount, WaveMonsterAmount)` comes from the StageInfoData instances the resolve
  already collected for the drop catalog (no extra sweep); re-read at re-attach.
- `total_mobs` is used for BOTH the run record's `total_mobs` and the live overlay's — one formula.

## Rules

- Runes unreadable (None) → reduction 0 → the historical `horde + 1`. Never guess a reduction.
- No live wave row for the stage → fall back to `horde + 1` (same as before).
- Act-boss x-10 has no horde (catalog horde 0) → total stays 1 regardless of runes.
