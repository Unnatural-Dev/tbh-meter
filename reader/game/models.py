"""models.py — LIVE WORLD reads (monsters) for DPS + the runtime stageKey.

Free functions taking (reader, ...), mirroring the monolith (same validated logic).
DPS = Σ of monster HP drops (the game doesn't store damage); HP is a RAW float @0x40/0x50."""

from config.offsets import List, MonsterSpawnManager, Unit, UnitHealthController, Monster

MONSTER_LIST_CAP = 600   # sanity cap on a monster List<Unit> size (a larger read is garbage)


def _monster_list_fields():
    """MONSTER_LIST, plus SUMMONED_LIST only when it is a list of its own. 1.3.0 restructured
    MonsterSpawnManager and SUMMONED_LIST now aliases DEAD_MONSTER_LIST (see config/offsets.py): if
    that slot ever fills, iterating it as summons would feed dead units' stale HP into DPS."""
    s = MonsterSpawnManager.SUMMONED_LIST
    if s in (MonsterSpawnManager.MONSTER_LIST, MonsterSpawnManager.DEAD_MONSTER_LIST):
        return (MonsterSpawnManager.MONSTER_LIST,)
    return (MonsterSpawnManager.MONSTER_LIST, s)


def live_monster_snapshot(reader, msm):
    """One tick's monster snapshot for DpsTracker: list of (unit_addr, hp_cur, hp_max), or None
    when the snapshot is INVALID (a list header/array could not be read). A failed read must not
    look like an empty stage, or every live monster is counted dead and counted again when it
    reappears. A unit whose HealthController pointer or HP can't be READ is kept with hp=None so
    the tracker carries it forward; a NULL HealthController (valid read of 0) is omitted, as before,
    so it counts as gone.

    HOT LOOP (10Hz, every mob on the stage): the pointer array comes out in ONE read
    (read_array_ptrs) and hp_cur+hp_max in a SINGLE read_struct (0x40..HP_MAX), leaving 1 syscall
    per Unit to dereference its HealthController (addresses are scattered, so no batching)."""
    out = []
    for field in _monster_list_fields():
        lst = reader.rptr(msm + field)
        if lst is None:
            return None                     # bad read (not a null list)
        if not lst:
            continue                        # null list = no monsters in this slot
        size = reader.ri32(lst + List.SIZE)
        items = reader.rptr(lst + List.ITEMS)
        if size is None or items is None or size < 0 or size > MONSTER_LIST_CAP:
            return None
        if size == 0:
            continue
        ptrs = reader.read_array_ptrs(items, size)
        if len(ptrs) != size:
            return None
        # HP_CURRENT@0x40 and HP_MAX (1.3.0: 0x50, auto-calibrated per build by field_calib) read
        # in ONE syscall. The window spans HP_CURRENT..HP_MAX inclusive so it follows a shifted
        # HP_MAX automatically (field_calib may move HP_MAX on a future recompile).
        cur_off = UnitHealthController.HP_CURRENT
        max_off = UnitHealthController.HP_MAX
        span = max_off - cur_off
        fmt = "<%df" % (span // 4 + 1)
        for u in ptrs:
            if not u:
                continue
            hc = reader.rptr(u + Unit.HEALTH_CONTROLLER)
            if hc == 0:
                continue                    # null HC (valid read): no live health -> gone
            vals = reader.read_struct(hc + cur_off, fmt) if hc else None
            if vals is None:
                out.append((u, None, None))
            else:
                out.append((u, vals[0], vals[span // 4]))
    return out


def live_monsters(reader, msm):
    """Iterates (unit_addr, hp_cur, hp_max) over the live + summoned monsters with readable HP.
    Readable-HP view of live_monster_snapshot for callers such as validate_live. If any list
    read fails, discard the whole snapshot, including readable units from another list, so
    validation uses the same complete-snapshot requirement as runtime DPS. Within a valid
    snapshot, skip units whose HP could not be read."""
    for u, cur, hp_max in live_monster_snapshot(reader, msm) or ():
        if cur is not None:
            yield u, cur, hp_max


def live_stage_key(reader, msm):
    """LIVE stageKey = Monster.STAGE_KEY (every mob has the same one; flips instantly on a stage
    change — the save's currentStageKey is stale). Mode of the first few reads."""
    keys = []
    for u in reader.list_iter(reader.rptr(msm + MonsterSpawnManager.MONSTER_LIST), cap=MONSTER_LIST_CAP):
        k = reader.ri32(u + Monster.STAGE_KEY)
        if k is not None and 0 < k < 10_000_000:
            keys.append(k)
        if len(keys) >= 10:
            break
    return max(set(keys), key=keys.count) if keys else None
