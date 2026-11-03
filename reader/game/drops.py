"""drops.py — the game's DROP CATALOG (per stage: which boxes drop, at what rate, and each box's
cooldown), read live from the build's own StageInfoData/ItemInfoData tables.

Why live (not the app's datamined snapshot): the snapshot is pinned to the build it was mined from
(1.00.14 at the time of writing) — 1.3.x re-keyed boxes (e.g. 3-8 Torment 920801 -> 920901) and
added the Plaguelands stages, while the live tables are by definition the running build's truth.

Never-raises, no I/O beyond the reader: free functions over (reader, instance lists). Emits plain
dicts; the orchestrator (meter_windows.write_drop_catalog) writes them to <output>/drop_catalog.json
for the app (the reader stays presentation-dumb; the app derives the boxes/hr model from this + the
measured clear times).
"""

from config.offsets import StageInfoData, ItemInfoData

# Box item key families (EItemType BOX): 91xxxx = monster (gray), 92xxxx = stage boss (blue),
# 93xxxx = act boss. Only these keys carry a DropCooldown at ItemInfoData.DROP_COOLDOWN.
BOX_KEY_MIN = 910000
BOX_KEY_MAX = 940000
RATE_SCALE = 1_000_000          # StageInfoData drop rates are parts-per-million
MAX_COOLDOWN_SEC = 86_400       # a DropCooldown above 24h is a misread, never served
CATALOG_FMT = 1                 # bump when drop_catalog.json's SHAPE changes


def is_box_key(k):
    return isinstance(k, int) and BOX_KEY_MIN <= k < BOX_KEY_MAX


def _plausible_rate(r):
    return isinstance(r, int) and 0 <= r <= RATE_SCALE


def read_box_cooldowns(reader, item_infos):
    """{boxKey: cooldown_seconds} for every box item in the live ItemInfoData list. A cooldown
    outside [0, MAX_COOLDOWN_SEC] is a misread and is dropped (never served)."""
    out = {}
    for a in item_infos or []:
        try:
            k = reader.ri32(a + ItemInfoData.ITEM_KEY)
            if not is_box_key(k) or k in out:
                continue
            cd = reader.ri32(a + ItemInfoData.DROP_COOLDOWN)
            if cd is not None and 0 <= cd <= MAX_COOLDOWN_SEC:
                out[k] = cd
        except Exception:
            continue
    return out


def read_stage_drops(reader, stage_infos):
    """{stageKey: {"monster": (boxKey, ppm) | None, "boss": (boxKey, ppm) | None}} from the live
    StageInfoData list. A slot whose key isn't a box or whose rate is out of range reads as None."""
    out = {}
    for a in stage_infos or []:
        try:
            sk = reader.ri32(a + StageInfoData.STAGE_KEY)
            if sk is None or not (0 < sk < 10_000_000) or sk in out:
                continue
            mk = reader.ri32(a + StageInfoData.MONSTER_DROP_KEY)
            mr = reader.ri32(a + StageInfoData.MONSTER_DROP_RATE)
            bk = reader.ri32(a + StageInfoData.BOSS_DROP_KEY)
            br = reader.ri32(a + StageInfoData.BOSS_DROP_RATE)
            out[sk] = {
                "monster": (mk, mr) if is_box_key(mk) and _plausible_rate(mr) else None,
                "boss": (bk, br) if is_box_key(bk) and _plausible_rate(br) else None,
            }
        except Exception:
            continue
    return out


def build_catalog(stage_drops, box_cooldowns, stage_info, game_version):
    """Assemble the JSON document the app reads. Only stages present in the validated stage_info
    catalog (act/stageNo/difficulty already gated) are emitted, so a garbage StageInfoData instance
    can never surface a phantom stage. `stage_info` rows are (act, stage_no, horde, diff)."""
    stages = {}
    for sk, d in stage_drops.items():
        si = stage_info.get(sk)
        if not si:
            continue
        row = {"act": si[0], "stageNo": si[1], "difficulty": si[3]}
        for slot in ("monster", "boss"):
            v = d.get(slot)
            row[slot] = ({"boxKey": v[0], "rate": v[1] / RATE_SCALE,
                          "cooldownSec": box_cooldowns.get(v[0])} if v else None)
        stages[str(sk)] = row
    return {"fmt": CATALOG_FMT, "gameVersion": game_version,
            "boxCooldowns": {str(k): v for k, v in sorted(box_cooldowns.items())},
            "stages": stages}
