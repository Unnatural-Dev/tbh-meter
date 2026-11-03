---
type: reference
description: "The live DROP CATALOG: per-stage monster/boss box keys + drop rates (StageInfoData) and per-box DropCooldown seconds (ItemInfoData), read from the RUNNING build's own tables and written to <output>/drop_catalog.json for the app's chest features. Live, not the datamined snapshot, because patches re-key boxes (1.3.x: 3-8 Torment 920801 -> 920901) and add stages (Plaguelands)."
symptoms:
  - "chest cooldown wrong"
  - "box cooldown"
  - "boxes per hour"
  - "drop_catalog.json missing"
  - "stale box key after patch"
  - "Plaguelands boxes unknown"
code_anchors:
  - game/drops.py::read_box_cooldowns
  - game/drops.py::read_stage_drops
  - game/drops.py::build_catalog
  - meter_windows.py::write_drop_catalog
  - meter_windows.py::_write_atomic
  - config/offsets.py::ItemInfoData.DROP_COOLDOWN
  - config/offsets.py::StageInfoData.MONSTER_DROP_KEY
  - config/offsets.py::StageInfoData.BOSS_DROP_KEY
asserts:
  - game.drops.RATE_SCALE == 1000000
  - game.drops.CATALOG_FMT == 1
guarded_by:
  - tests/test_drops.py::test_box_cooldowns_only_boxes_and_plausible
  - tests/test_drops.py::test_stage_drops_rejects_non_box_slots
  - tests/test_drops.py::test_catalog_joins_cooldowns_and_gates_on_stage_info
  - tests/test_drops.py::test_never_raises_on_unreadable_reader
  - tests/test_drops.py::TestWriteDropCatalog::test_writes_gated_catalog
  - tests/test_drops.py::TestWriteDropCatalog::test_missing_instances_writes_nothing
related: ["[[invariants/rva-index-resolution]]", "[[invariants/memory-safety]]", "[[invariants/orchestration-purity]]"]
---

# Drop catalog (box keys, rates, cooldowns)

## What it is

Each stage row (`StageInfoData`) carries its **monster box** and **stage/act-boss box** item keys and
their drop chances in **parts-per-million**. Each box item (`ItemInfoData`, keys `91xxxx` monster /
`92xxxx` stage boss / `93xxxx` act boss) carries the game's **DropCooldown** in seconds. Both tables
are plain (not Obscured) catalog classes the reader already resolves (`TARGETS`).

Live-confirmed on 1.3.2 by dumping the rows read-only and matching the datamined `stages.json`
(1-1 Normal → `910011` @160000 / `920011` @1000000) and the per-box cooldown families
(monster 120/180/240 s; stage boss 120/180/240/300/360 s; act boss 0 = ungated).

## How it flows

- Collected in the SAME backref sweep as PSD/CSD/StageManager on the fast path (single-sweep:
  extra needles cost ~0) and from `pass3` on the scan path. The instance lists ride OUTSIDE the
  frozen 14-tuple (`_LAST_CATALOG_INSTANCES`).
- `write_drop_catalog` runs once per resolve (startup + re-attach), joins rates + cooldowns, keeps
  ONLY stages present in the already-validated `stage_info` (a garbage instance can't surface a
  phantom stage), and writes `drop_catalog.json` atomically (`_write_atomic`, the same writer as
  `raw/<id>.json`). never-raises; on failure (or zero usable stages) NOTHING is written — a file
  from an earlier session stays on disk, so the app must check `gameVersion` before trusting it.

## Rules

- **Cooldown is per BOX, not per stage** — keep the box key as the identity (the app's tracker
  already keys by box).
- **`DROP_COOLDOWN` is only meaningful for box keys**; the same offset holds unrelated data on
  gear — `read_box_cooldowns` gates on the key family.
- The reader emits per-event values only: `rate` is a 0–1 probability (`ppm / RATE_SCALE`,
  lossless), `cooldownSec` / `boxCooldowns` are seconds (`cooldownSec` is null when the box row
  wasn't read). The boxes/hr model lives app-side.
