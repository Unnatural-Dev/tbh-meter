"""waves.py — the stage's REAL mob total, after the account's wave-count reduction.

The catalog row (`stage_info`) stores horde = WaveAmount × WaveMonsterAmount, which is the total for an
account with NO Rune of Brevity. Each Brevity node (EAccountStatus.WaveCountReduction) removes ONE
whole wave, so an account that bought them kills `(WaveAmount − reduction) × WaveMonsterAmount` + the boss.
Ignoring it made the overlay's mob bar never fill (e.g. 3-8 Nightmare: 301 kills shown as 301/346).

Evidence (game 1.3.2, the maintainer's account, all three nodes at level 1 → reduction 3), from the
modal kill count of `counted` successes: 3-8 Nightmare 301 = (23−3)×15+1, 3-6 Hell 409 = (27−3)×17+1,
3-8 Torment 589 = (31−3)×21+1, 1-1 Normal 8 = (10−3)×1+1; act-boss x-10 (no horde) unchanged at 1.

Pure functions (no memory access except `read_stage_waves`, which only reads through the reader and
never raises). The orchestrator calls these; the formula lives here (orchestration-purity).
"""

from config.offsets import StageInfoData

# Rune of Brevity nodes (the three WaveCountReduction runes in the account rune tree; max level 1 each,
# "Stage Wave Count -1" per node). Keys are build-stable rune ids, the same ids PlayerSaveData.RUNES
# carries (raw records already store them under `runes`).
WAVE_REDUCTION_RUNE_KEYS = (1171, 1242, 1301)
WAVE_REDUCTION_RUNES = frozenset(WAVE_REDUCTION_RUNE_KEYS)
WAVES_PER_LEVEL = 1
MAX_WAVES = 200          # mirrors the waves_ok row gate in meter_windows._read_catalogs


def read_stage_waves(reader, stage_infos):
    """{stageKey: (wave_amount, wave_monster_amount)} for horde stages in the live StageInfoData list.
    Rows outside 1..MAX_WAVES (act-boss x-10, garbage instances) are skipped. never-raises."""
    out = {}
    for a in stage_infos or []:
        try:
            sk = reader.ri32(a + StageInfoData.STAGE_KEY)
            if sk is None or not (0 < sk < 10_000_000) or sk in out:
                continue
            wa = reader.ri32(a + StageInfoData.WAVE_AMOUNT)
            wm = reader.ri32(a + StageInfoData.WAVE_MOB_AMOUNT)
            if wa and wm and 1 <= wa <= MAX_WAVES and 1 <= wm <= MAX_WAVES:
                out[sk] = (wa, wm)
        except Exception:
            continue
    return out


def wave_reduction(runes):
    """Total waves removed by the account's Brevity runes. `runes` = [{key, level}] (build.read_runes);
    None (didn't read) → 0, i.e. the unreduced total — the historical behavior, never a guess."""
    total = 0
    for r in runes or []:
        try:
            if r.get("key") in WAVE_REDUCTION_RUNES and (r.get("level") or 0) > 0:
                total += r["level"] * WAVES_PER_LEVEL
        except Exception:
            continue
    return total


def total_mobs(stage_row, waves, reduction):
    """Mobs in a full clear (horde + the boss). `stage_row` = the stage_info tuple (act, stage_no, horde,
    diff) or None; `waves` = (wave_amount, wave_monster_amount) or None. With no wave data (or no
    reduction) it falls back to the catalog's horde + 1 — exactly the previous total. A reduction can
    never take the horde below 0 (at least the boss remains)."""
    if not stage_row:
        return None
    if not waves or reduction <= 0:
        return stage_row[2] + 1
    wa, wm = waves
    return max(0, wa - reduction) * wm + 1
