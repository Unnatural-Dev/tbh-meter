"""Auto-calibration of the FRAGILE field offsets (game/field_calib.py).

WHY: il2cpp class RESOLUTION is already name-based + scan-based, so it survives a game update on its
own. But four FIELD offsets are read at hardcoded positions and shift on almost every recompile,
because the fields are OBFUSCATED (random names per build) so they can't be found by name in the
metadata — the documented blind spot. Historically each update required a human to live-probe them
and hand-patch config/offsets.py (1.00.14, 1.00.27, 1.2.8, 1.3.0...).

This module DISCOVERS those four offsets at attach time by SEMANTIC SIGNATURE — what the field
CONTAINS, not where it sits — then applies them in-memory over the static offsets, which remain the
fallback. It piggybacks on structures the resolver already produced (msm, sm_list, hero_cat,
stage_info), so it adds no scan. If a signature can't be found live (e.g. not in combat), the static
offset is kept and nothing is overwritten — so this NEVER makes a working build worse.

The four signatures:
  Monster.STAGE_KEY          — the int32 on a live monster whose value is a key in stage_info, ONE key
                               dominating the hits (every mob of a stage carries the same one).
  UnitHealthController.HP_MAX— the float in the HealthController that is >= HP_CURRENT(0x40) and > 0;
                               a replacement must be strictly above current on at least one monster.
  StageManager.HERO_LIST +   — the Il2CppArray of 1..12 ptrs on a StageManager whose elements, at some
  Unit.CACHE                   cache offset, resolve INFO->HeroKey to a plausible key in hero_cat
                               (discovered together).

STATIC FIRST: when the static offset still satisfies its signature it is kept, even if another slot in
the window also qualifies. Stage-key and hero-list/cache replacements require a UNIQUE best match;
ties keep the static. HP-max replacements require a value above current and prefer the smallest
qualifying offset. Scan windows live in config.offsets.FieldCalibWindows.

Discovery is read-only; it writes nothing to the game and nothing to disk.
"""
from collections import Counter

from config.offsets import (Array, Unit, Monster, UnitHealthController, StageManager,
                            MonsterSpawnManager, HeroRuntime, HeroInfoData,
                            FieldCalibWindows as Win)

# A real heroKey is <class><nn> (101, 201, ... 601). The HeroInfoData scan also yields junk keys
# (1, 2, 4, 8, 14, 17, 65535, 1376256, ...) that would widen the hero-list false-positive surface.
_HERO_KEY_MIN, _HERO_KEY_MAX = 100, 10_000
_MIN_PTR = 0x10000          # below this a "pointer" is a small int, not a heap address
_MAX_HERO_SLOTS = 12
_MAX_HP = 1e9
_HP_TOLERANCE = 1.01        # cur may sit a hair above max on a float round-trip
_SAMPLE_MONSTERS = 8


def _ri32(reader, a):
    try: return reader.ri32(a)
    except Exception: return None


def _rptr(reader, a):
    try: return reader.rptr(a)
    except Exception: return None


def _rf32(reader, a):
    try:
        v = reader.read_struct(a, "<f")
    except Exception:
        return None
    return v[0] if v else None


def _sample_monsters(reader, msm):
    ml = _rptr(reader, msm + MonsterSpawnManager.MONSTER_LIST)
    return (reader.list_ptrs(ml, cap=600) if ml else [])[:_SAMPLE_MONSTERS]


def _majority(hits, sampled):
    return sampled > 0 and hits * 2 >= sampled


def _pick(qualified, static):
    """`qualified` = {offset: score}. Static wins if it qualifies; otherwise the unique best score
    wins; a tie between non-static offsets is ambiguous -> None (the static fallback stays)."""
    if not qualified:
        return None
    if static in qualified:
        return static
    best = max(qualified.values())
    top = [o for o, c in qualified.items() if c == best]
    return top[0] if len(top) == 1 else None


def discover_stage_key(reader, msm, stage_info):
    """The int32 on a live monster whose value is a key in the stage catalog. ONE key must sit at the
    offset on a majority of the sampled monsters AND outnumber every other catalog value there combined
    (every mob of a stage carries the same key; a coincidental catalog-valued field varies by mob)."""
    if not msm or not stage_info:
        return None
    mons = _sample_monsters(reader, msm)
    if not mons:
        return None
    keys = set(stage_info.keys())
    seen = {}  # offset -> [catalog values hit]
    for u in mons:
        for off in Win.STAGE_KEY:
            v = _ri32(reader, u + off)
            if v is not None and v in keys:
                seen.setdefault(off, []).append(v)
    qualified = {}
    for o, vs in seen.items():
        top = Counter(vs).most_common(1)[0][1]
        if _majority(top, len(mons)) and top * 2 > len(vs):
            qualified[o] = top
    return _pick(qualified, Monster.STAGE_KEY)


def discover_hp_max(reader, msm):
    """The float in the HealthController that is >0 and >= HP_CURRENT(0x40) on a majority of the
    sampled monsters. Keep a plausible static even when all samples are at full HP. A replacement must
    be strictly above HP_CURRENT on at least one sample (rejects a copy of current); otherwise prefer
    the smallest such offset (the max sits right after current)."""
    if not msm:
        return None
    tally, above = {}, set()
    sampled = 0
    for u in _sample_monsters(reader, msm):
        hc = _rptr(reader, u + Unit.HEALTH_CONTROLLER)
        if not hc:
            continue
        cur = _rf32(reader, hc + UnitHealthController.HP_CURRENT)
        if cur is None or not (cur > 0):
            continue
        sampled += 1
        for off in Win.HP_MAX:
            if off == UnitHealthController.HP_CURRENT:
                continue
            v = _rf32(reader, hc + off)
            if v is not None and v > 0 and v == v and cur <= v * _HP_TOLERANCE and v < _MAX_HP:
                tally[off] = tally.get(off, 0) + 1
                if v > cur:
                    above.add(off)
    # Full-health samples provide no evidence against a plausible static max. Require damage only
    # when selecting a replacement, so an unrelated larger float cannot displace the working static.
    if _majority(tally.get(UnitHealthController.HP_MAX, 0), sampled):
        return UnitHealthController.HP_MAX
    good = sorted(o for o, c in tally.items() if o in above and _majority(c, sampled))
    if not good:
        return None
    return good[0]


def _plausible_hero_keys(hero_cat):
    return {k for k in hero_cat if _HERO_KEY_MIN <= k < _HERO_KEY_MAX}


def _hero_match(reader, els, cache_off, hero_keys):
    """Number of elements matched if EVERY element resolves a plausible heroKey at `cache_off` with
    >= 2 DISTINCT keys when there are >= 2 elements (defeats the "same key repeated" false positive,
    e.g. [2,2,2]); else 0."""
    seen = set()
    for e in els:
        uf = _rptr(reader, e + cache_off)
        if not uf or uf < _MIN_PTR:
            return 0
        info = _rptr(reader, uf + HeroRuntime.INFO)
        if not info or info < _MIN_PTR:
            return 0
        hk = _ri32(reader, info + HeroInfoData.HERO_KEY)
        if hk not in hero_keys:
            return 0
        seen.add(hk)
    return len(els) if len(seen) >= min(2, len(els)) else 0


def discover_hero_list_and_cache(reader, sm_list, hero_cat):
    """Discover StageManager.HERO_LIST and Unit.CACHE TOGETHER: an array of 1..12 ptrs whose elements,
    at some cache offset, resolve HeroRuntime.INFO -> HeroInfoData.HERO_KEY to a plausible hero_cat key.
    The static pair wins as soon as it matches on any StageManager (stops the scan); otherwise the
    pair with the unique largest match wins, and a tie between pairs is ambiguous (a solo party
    matching at two pairs is exactly the weak case). Returns (hero_list_off, cache_off) or (None, None)."""
    if not sm_list or not hero_cat:
        return None, None
    hero_keys = _plausible_hero_keys(hero_cat)
    if not hero_keys:
        return None, None
    static = (StageManager.HERO_LIST, Unit.CACHE)
    best = {}  # (hero_list_off, cache_off) -> largest matched count on any StageManager
    for sm in sm_list:
        for hl_off in Win.HERO_LIST:
            hl = _rptr(reader, sm + hl_off)
            if not hl or hl < _MIN_PTR:
                continue
            n = _ri32(reader, hl + Array.MAX_LENGTH)
            if n is None or not (0 < n <= _MAX_HERO_SLOTS):
                continue
            els = [p for p in reader.read_array_ptrs(hl, n) if p and p > _MIN_PTR]
            if not els:
                continue
            for cache_off in Win.CACHE:
                matched = _hero_match(reader, els, cache_off, hero_keys)
                if not matched:
                    continue
                pair = (hl_off, cache_off)
                if pair == static:
                    return static
                best[pair] = max(best.get(pair, 0), matched)
    return _pick(best, static) or (None, None)


def calibrate(reader, msm, sm_list, stage_info, hero_cat, log=lambda *a: None):
    """Run all discoveries and APPLY the found offsets in-memory over the static offset classes.
    Only overwrites a field when its signature was found; otherwise the static fallback stays.
    Returns a dict of {field_name: (old, new, 'found'|'fallback')} for logging/diagnostics."""
    report = {}

    sk = discover_stage_key(reader, msm, stage_info)
    report["Monster.STAGE_KEY"] = (Monster.STAGE_KEY, sk, "found" if sk else "fallback")
    if sk:
        Monster.STAGE_KEY = sk

    hpm = discover_hp_max(reader, msm)
    report["UnitHealthController.HP_MAX"] = (UnitHealthController.HP_MAX, hpm, "found" if hpm else "fallback")
    if hpm:
        UnitHealthController.HP_MAX = hpm

    hl, cache = discover_hero_list_and_cache(reader, sm_list, hero_cat)
    report["StageManager.HERO_LIST"] = (StageManager.HERO_LIST, hl, "found" if hl else "fallback")
    report["Unit.CACHE"] = (Unit.CACHE, cache, "found" if cache else "fallback")
    if hl:
        StageManager.HERO_LIST = hl
    if cache:
        Unit.CACHE = cache

    for name, (old, new, status) in report.items():
        if status == "found" and new != old:
            log(f"[field-calib] {name}: 0x{old:X} -> 0x{new:X} (auto-discovered)")
        elif status == "fallback":
            log(f"[field-calib] {name}: kept static 0x{old:X} (signature not found live)")
    return report
