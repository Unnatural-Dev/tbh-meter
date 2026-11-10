"""Resolve and calibrate one attachment to the game (read-only)."""
import json
import os
import time
from dataclasses import dataclass

from config.offsets import (List, Array, Class, MonsterSpawnManager, LogManager,
                            StageInfoData, ItemInfoData, HeroInfoData, EStageDifficulty,
                            EStageType, name_map)
from shared.memory import regions
from shared.utils import resource_path, diag, _emit_status, _write_atomic
from il2cpp.resolver import resolve, resolve_via_rva, instances_of, SINGLETONS
from il2cpp import typeinfo
from metrics.gold import (resolve_combat_gold_klass, resolve_combat_gold_klass_by_index,
                          gold_index_of_klass, gold_index_by_structure,
                          combat_gold_klass_ok, combat_gold_live)
from game import build, save, drops, waves

DIFF_NAMES = name_map(EStageDifficulty)

TARGETS = ["MonsterSpawnManager", "LogManager", "StageClearLog", "StageFailedLog",
           "GetBoxLog", "HeroDieLog", "ResurrectionLog",
           "CommonSaveData", "CurrencySaveData", "HeroSaveData", "StageInfoData",
           "PlayerSaveData", "ItemInfoData", "HeroInfoData", "StageManager"]

# instances_of caps per class (§10). The fast-path sweep raises it for the CATALOG classes
# (ItemInfoData ~7.8k instances live on 1.3.2) and trims the save/manager lists back to the old bound.
CATALOG_INSTANCE_CAP = 20000
BACKREF_INSTANCE_CAP = 4000


@dataclass(frozen=True)
class _Resolved:
    """Named resolution data before the carrier picks are prepared."""

    sc_class: int | None
    sf_class: int | None
    msm: int | None
    lm: int | None
    csd_list: list[int]
    psd_list: list[int]
    stage_info: dict[int, tuple[int, int, int, int]]
    item_cat: dict[int, tuple[int, int, int]]
    hero_cat: dict[int, int | None]
    sm_list: list[int]
    gold_klass: int | None
    gb_class: int | None
    die_class: int | None
    res_class: int | None
    catalog_instances: dict[str, list[int]]

    @property
    def complete(self):
        return bool(self.msm and self.lm and self.sc_class and self.sf_class)


@dataclass(frozen=True)
class AttachedState(_Resolved):
    """Everything prepared once per attachment; no per-run counters or baselines.

    Frozen field bindings, not a deep freeze of the catalog/list contents.
    Re-attach replaces this state wholesale.
    """

    tbase: int | None
    idx_ut: int | None
    csd: int | None
    sm: int | None
    stage_waves: dict[int, tuple[int, int]]


def write_drop_catalog(reader, output_dir, stage_info, game_version, catalog_instances):
    """Read the live drop catalog (per-stage box keys + rates, per-box cooldowns; game.drops) from the
    instances the latest resolve collected and write <output_dir>/drop_catalog.json for the app.
    never-raises; returns the stage count written (0 = nothing written: any earlier drop_catalog.json
    stays on disk, so the app must gate on its gameVersion before preferring it over the bundled data)."""
    try:
        inst = catalog_instances
        if not inst.get("StageInfoData") or not inst.get("ItemInfoData"):
            diag("[drops] catalog instances missing — drop_catalog.json NOT written")
            return 0
        cds = drops.read_box_cooldowns(reader, inst["ItemInfoData"])
        sd = drops.read_stage_drops(reader, inst["StageInfoData"])
        doc = drops.build_catalog(sd, cds, stage_info, game_version)
        n = len(doc["stages"])
        if n and _write_atomic(os.path.join(output_dir, "drop_catalog.json"),
                               json.dumps(doc, separators=(",", ":"))):
            diag(f"[drops] drop_catalog.json written: stages={n} boxes={len(cds)}")
            return n
        diag(f"[drops] drop_catalog.json NOT written (stages={n})")
        return 0
    except Exception as e:
        diag(f"[drops] catalog read failed: {type(e).__name__}: {e}")
        return 0



CACHE_FMT = 9   # bump when the cache shape changes. 9 = stage_info includes ACTBOSS stages (x-10) — old calibs lack those keys and the fast path reuses them forever, so force ONE re-scan. 8 = CALIB-ONLY: a calib{fp:...} block keyed by build fingerprint — relative anchor_rva (ASLR-stable) + indices{name:idx} + idx_ut + build-stable catalogs. The LEGACY absolute-address cache (sc_class/msm/lm/... + load_cache/save_cache/_managers_ok) WAS REMOVED: calib is build-keyed and the fast path revalidates by round-trip + instance size every launch (it stores no absolute address, so there's nothing to revalidate by address). History: 7 = +die_class/res_class; 6 = +gb_class; 5 = +gold_klass; 4 = +sm_list for live party.


def _seed_path():
    """Path to the BUNDLED calibration SEED (read-only in the bundle), via resource_path — matches
    `--add-data "config/calib_seed.json;config"` in the frozen build, resolves to reader/config/ in
    source. The seed is OPTIONAL: when absent → resource_path points at a nonexistent file, and
    _read_calib returns None fine (caller falls back to the scan)."""
    return resource_path("config/calib_seed.json")


def _stage_info_ok(stage_info):
    """Sanity check on the stage_info catalog: non-empty and EVERY row in shape (act, stage_no, horde,
    diff) — 4 ints, with plausible act/stage_no (1..200, mirrors the row gate in
    _read_catalogs; horde has NO range-check — boss x-10 legitimately has horde=0) and diff
    within EStageDifficulty (the DIFF_NAMES keys, exactly what close_run/overlay
    resolve via DIFF_NAMES.get). A row outside this = suspect catalog (scan misread or
    poisoned cache): serving/persisting it would mean PERMANENT mode "?".
    It's the gate on BOTH sides: on LOAD (_read_calib rejects → load_calib falls back to seed → scan,
    auto-healing a poisoned resolve_cache.json without the user deleting anything) and on PERSIST
    (save_calib doesn't write a bad catalog). Unlike anchor/indices, the catalogs have NO
    live round-trip revalidation in the fast path — their defense is this VALUE gate + the
    COMPLETENESS-vs-seed gate (_covers_seed_keys)."""
    if not stage_info:
        return False
    # bool is a subclass of int in Python: a `true` in a hand-edited cache would pass as
    # diff=1 — exclude it explicitly (real JSON from save_calib only produces ints).
    return all(isinstance(row, tuple) and len(row) == 4
               and all(isinstance(x, int) and not isinstance(x, bool) for x in row)
               and 1 <= row[0] <= 200 and 1 <= row[1] <= 200
               and row[3] in DIFF_NAMES
               for row in stage_info.values())


def _covers_seed_keys(seed_entry, stage_info, item_cat, hero_cat):
    """COMPLETENESS-vs-seed gate: do the candidate catalogs cover EVERY key the bundled
    seed has for the SAME fp? Catalogs are build CONSTANTS — for the same fingerprint,
    the shipped seed (validated live at capture) is ground truth for WHICH keys exist; a
    local catalog missing some seed key has a HOLE (a row dropped by misread in the
    _read_catalogs row gate) and is provably worse. With no seed for the fp (None) there's no
    reference → no constraint (True). PRESENCE of a key ONLY, NEVER a value comparison: an
    extra local key always passes, and the local value wins when present (protects against a
    hypothetically stale seed under the same fp)."""
    if seed_entry is None:
        return True
    return (set(seed_entry["stage_info"]) <= set(stage_info)
            and set(seed_entry["item_cat"]) <= set(item_cat)
            and set(seed_entry["hero_cat"]) <= set(hero_cat))


def load_calib(path, fp):
    """Reads the BUILD-STABLE calibration block `calib[fp]`. Tries the USER cache (`path`,
    ~/tbh-meter/resolve_cache.json) FIRST; if it doesn't cover `fp`, falls back to the bundled SEED
    (config/calib_seed.json). Returns {anchor_rva, indices{name:idx}, idx_ut, stage_info,
    item_cat, hero_cat} or None (old fmt / fp absent in both / corrupt JSON → scan).

    SEED FALLBACK (seed-calib strategy): lets the FIRST launch on a SHIPPED build skip the ~70s
    scan (turns into ~ms of load). The seed is just ONE MORE calib[fp] hypothesis — ZERO new trust: the
    fast path (_resolve_fast) revalidates live every launch (name round-trip + instance size
    + gold round-trip) and degrades to the guaranteed scan on ANY mismatch; an old seed / one from
    another build is simply a MISS by fp (falls back to the scan), never poisons. The user cache
    has priority — a locally LEARNED calib (save_calib) overrides the seed on the next launch.
    Proven in tbh-meter-dev/seed_calib_probe.py (20/20, seed-path 7.8s vs scan 73s, ~9x; negatives
    corrupt anchor/idx → _resolve_fast None).

    Amendment R3 (completeness-vs-seed): the user cache's priority is CONDITIONED on
    covering the seed's keys for the SAME fp (_covers_seed_keys). A cache whose catalogs
    lost keys the seed has is a catalog with a HOLE (rows dropped by misread in the
    _read_catalogs row gate): it passes the value gates, but serving it would shadow the
    good seed FOREVER (nothing re-triggers a scan) → same symptom as the poisoned cache (mode
    "?" on the hole's stage), again only curable by deleting the cache by hand. In that case serve the
    SEED (with an observability log). Cost: the seed is parsed ONCE per load, even on a
    cache-hit (~ms, acceptable). Semantics preserved otherwise: good cache → cache; cache
    None → seed; both None → None."""
    seed = _read_calib(_seed_path(), fp)
    entry = _read_calib(path, fp)
    if entry is not None:
        if _covers_seed_keys(seed, entry["stage_info"], entry["item_cat"], entry["hero_cat"]):
            return entry
        # Name EACH holed catalog (not just the total): triage is remote, via
        # meter.log — "stage_info=2" vs "item_cat=40" point to very different misreads.
        holes = {c: len(set(seed[c]) - set(entry[c]))
                 for c in ("stage_info", "item_cat", "hero_cat")}
        detail = " ".join(f"{c}={n}" for c, n in holes.items() if n)
        print(f"[calib] user cache for fp {fp} missing seed keys: {detail} — serving seed")
        return seed
    return seed


def _read_calib(path, fp):
    """Reads the `calib[fp]` block of ONE file (fmt==CACHE_FMT). None if old fmt / fp absent /
    corrupt JSON or unexpected shape (e.g. non-dict top-level) / nonexistent file.
    TOTAL by construction — NEVER raises: every shape access lives inside try. Matters
    because save_calib's completeness-vs-seed gate calls this OUTSIDE save_calib's try,
    and _calibrate promises "NEVER breaks the flow" (a malformed file can't crash the
    post-scan).

    Does NOT validate absolute addresses: the calib block has none — `anchor_rva` is RELATIVE to
    ga_base (re-read live every launch) and the indices are build CONSTANTS. Anchor/indices
    are "raw data, VALIDATED BY THE CALLER": the resolver revalidates 1 name via round-trip + instance
    size every start; a bad anchor/index degrades to the scan, NEVER poisons. Catalogs
    reconstructed with int(k)/tuple(v) as in the legacy load_cache.

    EXCEPTION — stage_info is validated HERE (_stage_info_ok): the catalog has no live round-trip in the
    fast path, so a poisoned cache (e.g. rows with diff -1 written before the diff gate
    in _read_catalogs) would be served forever → mode "?" on every run. Rejecting the block here
    makes load_calib fall back to the bundled seed (and that, if it too fails/misses, to the scan, which
    re-calibrates and OVERWRITES the poisoned calib[fp]) — auto-healing, no deleting the cache by hand."""
    try:
        c = json.load(open(path, encoding="utf-8"))
        if c.get("fmt") != CACHE_FMT:
            return None
        entry = c.get("calib", {}).get(fp)
    except Exception:
        return None
    if not entry:
        return None
    try:
        indices = {k: int(v) for k, v in entry.get("indices", {}).items()}
        si = {int(k): tuple(v) for k, v in entry.get("stage_info", {}).items()}
        if not _stage_info_ok(si):
            return None
        item_cat = {int(k): tuple(v) for k, v in entry.get("item_cat", {}).items()}
        hero_cat = {int(k): (v if v is not None else None)
                    for k, v in entry.get("hero_cat", {}).items()}
        return {"anchor_rva": entry.get("anchor_rva"), "indices": indices,
                "idx_ut": entry.get("idx_ut"), "stage_info": si,
                "item_cat": item_cat, "hero_cat": hero_cat}
    except Exception:
        return None


def save_calib(path, fp, anchor_rva, indices, idx_ut, stage_info, item_cat, hero_cat):
    """Persists ONE build's (`fp`) calibration into the cache's calib block. MERGE: re-reads the
    existing JSON, sets calib[fp]={...}, preserves the other fps and the legacy address block.

    COMPLETENESS PERSIST-GATE (amendment R1): only persists if the catalogs are SOUND
    (len>0 on all three), mirroring run()'s `if msm and lm and sc_class and sf_class:` guard.
    Otherwise a scan run OUTSIDE a stage would write an empty catalog → the fast-path would serve a
    degraded catalog FOREVER for that fp, with no re-resolve trigger. DON'T persist an incomplete calib.
    Amendment R2: stage_info goes through _stage_info_ok (every row with a valid EStageDifficulty
    diff) — mirrors _read_calib's load-gate; a misread never becomes a persisted calibration.
    Amendment R3: completeness-vs-seed (_covers_seed_keys) — when the bundled seed covers the fp,
    a scan whose catalogs DON'T have every seed key (rows dropped by misread in the _read_catalogs
    row gate) does NOT persist: the holed catalog would shadow the good seed
    forever for that fp (mirrors load_calib's load-gate; the seed keeps serving on subsequent
    launches). With no seed covering the fp, persists exactly as before.

    ATOMIC WRITE: json.dump into path+".tmp", flush()+os.fsync(), os.replace(tmp, path). Same
    volume (local `~/tbh-meter`) → os.replace is OS-atomic → a kill mid-write NEVER leaves the
    cache truncated/poisoned. (Fixes the legacy save_cache's non-atomic write hygiene.)"""
    if not (_stage_info_ok(stage_info) and len(item_cat) > 0 and len(hero_cat) > 0):
        return
    if not _covers_seed_keys(_read_calib(_seed_path(), fp), stage_info, item_cat, hero_cat):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            doc = json.load(open(path, encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("fmt") != CACHE_FMT:
                doc = {"fmt": CACHE_FMT}
        except Exception:
            doc = {"fmt": CACHE_FMT}
        calib = doc.get("calib")
        if not isinstance(calib, dict):
            calib = {}
        calib[fp] = {
            "anchor_rva": anchor_rva,
            "indices": {k: int(v) for k, v in indices.items()},
            "idx_ut": idx_ut,
            "stage_info": {str(k): list(v) for k, v in stage_info.items()},
            "item_cat": {str(k): list(v) for k, v in item_cat.items()},
            "hero_cat": {str(k): v for k, v in hero_cat.items()},
        }
        doc["calib"] = calib
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        pass


def _valid_list_size(reader, inst, list_off, cap):
    """size of the List<T> at inst+list_off IF it's a STRUCTURALLY valid List, else None.
    The pointer scan finds class-K in DOZENS of slots that are NOT the real object (vtables,
    copies, metadata): the true singleton is the only one whose list_off is a real List<T> —
    readable items, capacity >= size, and entries that are objects (readable class). Before, it only
    checked 0<=size<cap: a junk slot with the qword at +SIZE landing in range (e.g. 0 from zeroed
    memory) passed, the "list" never grew, and NO run closed (a non-deterministic per-launch bug)."""
    ll = reader.rptr(inst + list_off)
    if not ll or ll < 0x10000:
        return None
    size = reader.ri32(ll + List.SIZE)
    items = reader.rptr(ll + List.ITEMS)
    if size is None or not (0 <= size < cap) or not items or items < 0x10000:
        return None
    maxlen = reader.ri32(items + Array.MAX_LENGTH)
    if maxlen is None or not (size <= maxlen < 1_000_000):
        return None
    for idx in ({0, size - 1} if size else ()):       # are entries objects with a readable class?
        e = reader.rptr(items + Array.DATA + idx * 8)
        if not e or not reader.read_cstr(reader.rptr((reader.rptr(e) or 0) + Class.NAME)):
            return None
    return size


def _pick_list_singleton(reader, cands, list_off, cap):
    """The REAL singleton among the scan's false-positives: the candidate with the structurally
    valid List of LARGEST size (the live log/monster-list has entries; junk doesn't). Falls back to the
    old pick (1st in range) so a good resolve NEVER regresses to None in a degenerate state."""
    best, best_sz = None, -1
    for a in cands:
        s = _valid_list_size(reader, a, list_off, cap)
        if s is not None and s > best_sz:
            best, best_sz = a, s
    if best is not None:
        return best
    return next((a for a in cands
                 if (lambda s: s is not None and 0 <= s < cap)(
                     reader.ri32((reader.rptr(a + list_off) or 0) + List.SIZE))), None)


def _read_catalogs(reader, inst):
    """Derives the build-stable catalogs (stage_info/item_cat/hero_cat) from the resolved
    *Data instances. Used by the scan (slow path); the fast path reuses calib's."""
    stage_info = {}
    for a in inst.get("StageInfoData", []):
        sk = reader.ri32(a + StageInfoData.STAGE_KEY)
        st = reader.ri32(a + StageInfoData.STAGE_TYPE)
        wa = reader.ri32(a + StageInfoData.WAVE_AMOUNT)
        wm = reader.ri32(a + StageInfoData.WAVE_MOB_AMOUNT)
        act = reader.ri32(a + StageInfoData.ACT)
        sno = reader.ri32(a + StageInfoData.STAGE_NO)
        diff = reader.ri32(a + StageInfoData.DIFFICULTY)
        # DIFF, ACT and STAGE_NO are required and plausible on EVERY row (horde AND boss):
        # diff within EStageDifficulty (the DIFF_NAMES keys — exactly what
        # close_run/overlay resolve), act/sno in 1..200. A horde row with an
        # unreadable/out-of-enum diff was cataloged with -1 → mode "?" on EVERY run of that stage,
        # FOREVER (the catalog persists in calib). A suspect row does NOT enter (§6: degrade,
        # never serve/persist bad data) — but dropping it opens a HOLE with the SAME blast
        # radius as x-10 below (mode "?" at close/overlay and blind stage adoption/switch
        # in the loop), and the hole would persist in calib[fp] for the build's lifetime, not "just
        # this run". What cures the recurrence is the completeness-vs-seed gate
        # (_covers_seed_keys, in save_calib AND load_calib): with a seed covering the fp, a
        # holed catalog never persists nor shadows the sound seed.
        diff_ok = diff in DIFF_NAMES
        actsno_ok = (act is not None and sno is not None
                     and 1 <= act <= 200 and 1 <= sno <= 200)
        waves_ok = bool(wa and wm and 1 <= wa <= 200 and 1 <= wm <= 200) and diff_ok and actsno_ok
        # x-10 (ACTBOSS) has no horde waves (wa/wm out of range) -> the row fell into the
        # filter above and the stage stayed OUT of the catalog: mode "?" at close/overlay
        # and blind stage adoption/switch in the loop. Validate by STAGE_TYPE + plausible
        # fields; horde = 0 (the consumers' "+1 = the boss" already covers the total).
        boss_ok = st == EStageType.ACTBOSS and diff_ok and actsno_ok
        if sk is not None and (waves_ok or boss_ok):
            stage_info[sk] = (act, sno, wa * wm if waves_ok else 0, diff)
    item_cat = {}
    for a in inst.get("ItemInfoData", []):
        ik = reader.ri32(a + ItemInfoData.ITEM_KEY)
        if ik is not None and 0 < ik < 10_000_000 and ik not in item_cat:
            item_cat[ik] = (reader.ri32(a + ItemInfoData.GRADE), reader.ri32(a + ItemInfoData.PARTS),
                            reader.ri32(a + ItemInfoData.LEVEL))
    hero_cat = {}
    for a in inst.get("HeroInfoData", []):
        hk = reader.ri32(a + HeroInfoData.HERO_KEY)
        if hk is not None and 0 < hk < 10_000_000 and hk not in hero_cat:
            hero_cat[hk] = reader.ri32(a + HeroInfoData.CLASS_TYPE)
    return stage_info, item_cat, hero_cat


def _resolve_fast(reader, ga_base, calib):
    """FAST PATH (name-free, ~ms): resolves the same named state as the scan from the build-stable
    `calib` block (no scan at all). All memory reading/validation lives in
    typeinfo/resolver/gold — here we only orchestrate: re-read the live table_base via anchor_rva (the
    ga_base changes per ASLR every launch, the anchor_rva is build-stable), resolve classes/singletons
    by index + round-trip + size (resolver), the gold by index (gold) and assemble the state. The
    catalogs come from calib (build-stable). Returns the resolved state or None on ANY sanity-fail
    (§6: degrades to the scan, NEVER serves bad data).

    PSD/CSD/StageManager are NOT useful singletons here: PlayerSaveData/CommonSaveData aren't
    singletons (bbwf→None) and `pick_live_sm` needs the PARTY-CARRYING instance (the StageManager's
    bbwf gives SOME instance, not necessarily the live one). All THREE come from ONE targeted
    backref (resolver.instances_of) over the READABLE regions — K is already obtained by index, all
    that's left is finding who points to it. Single-sweep (#110): 3 needles ≈ 1, ~8s on the measured machine. This
    leaves psd_list/csd_list/sm_list POPULATED before `ready` → the startup's new_run() (which reads PSD
    for build/heroes/baselines) has a live PSD. `pick_live_sm` operates as it does today over the backref's list."""
    tbase = typeinfo.table_base(reader, ga_base, calib["anchor_rva"])
    if not tbase:
        return None
    rv = resolve_via_rva(reader, tbase, calib["indices"], TARGETS, SINGLETONS)
    gold_klass = resolve_combat_gold_klass_by_index(reader, tbase, calib["idx_ut"])
    if rv is None or not gold_klass:
        return None
    classes, instances = rv
    sc_class = next(iter(classes["StageClearLog"]))
    sf_class = next(iter(classes["StageFailedLog"]))
    gb_class = next(iter(classes["GetBoxLog"]))
    die_class = next(iter(classes["HeroDieLog"]))
    res_class = next(iter(classes["ResurrectionLog"]))
    # Managers MSM/LM: the resolver already validated the instance by size (§ resolver._manager_inst_ok).
    msm = next(iter(instances["MonsterSpawnManager"]), None)
    lm = next(iter(instances["LogManager"]), None)
    # PSD/CSD/StageManager via targeted backref (ONE scan, ~8s). PSD/CSD non-singletons; the
    # StageManager comes from here (not bbwf) so pick_live_sm picks the instance with party,
    # exactly as in the slow path. An empty PSD (e.g. not logged in) does NOT fail the fast path — it's the
    # same degradation the scan would have (pick_live_psd→None → empty build for that run only, §6).
    _tb = time.time()
    # ONE sweep also collects StageInfoData/ItemInfoData instances for the DROP CATALOG (box keys,
    # rates, per-box cooldowns — game.drops). Single-sweep: extra needles cost ~0 (measured live on
    # 1.3.2: 3 classes 7.69s vs 5 classes 7.66s). Catalog lists travel with this
    # attachment, never through a module-global stash.
    _backref = ("PlayerSaveData", "CommonSaveData", "StageManager")
    _catalog = tuple(n for n in ("StageInfoData", "ItemInfoData") if classes.get(n))
    insts = instances_of(reader, regions(reader),
                         {name: next(iter(classes[name])) for name in _backref + _catalog},
                         cap=CATALOG_INSTANCE_CAP)
    catalog_instances = {n: insts.get(n, []) for n in _catalog}
    # The raised cap is for the catalogs only (ItemInfoData has ~7.8k instances on 1.3.2; the old
    # 4000 default silently dropped half the boxes). PSD/CSD/SM keep their historical §10 bound.
    for n in _backref:
        insts[n] = insts.get(n, [])[:BACKREF_INSTANCE_CAP]
    print(f"[calib] fast path PSD/CSD/StageManager backref in {time.time() - _tb:.1f}s "
          f"(PSD={len(insts['PlayerSaveData'])} CSD={len(insts['CommonSaveData'])} "
          f"SM={len(insts['StageManager'])})")
    psd_list = insts["PlayerSaveData"]
    csd_list = insts["CommonSaveData"]
    sm_list = insts["StageManager"]
    return _Resolved(sc_class=sc_class, sf_class=sf_class, msm=msm, lm=lm,
                     csd_list=csd_list, psd_list=psd_list, stage_info=calib["stage_info"],
                     item_cat=calib["item_cat"], hero_cat=calib["hero_cat"], sm_list=sm_list,
                     gold_klass=gold_klass, gb_class=gb_class, die_class=die_class,
                     res_class=res_class, catalog_instances=catalog_instances)


def _resolve_scan(reader):
    """SLOW PATH (scan ~190s + gold value-scan ~90s): resolves from scratch via il2cpp.resolver
    and derives all managers/catalogs. GUARANTEED fallback (§6) — always works, any
    build. Returns (resolved state, classes) — `classes` feeds the calibration in _resolve_all."""
    regs = regions(reader)
    classes, inst = resolve(reader, regs, TARGETS)
    sc_class = next(iter(classes["StageClearLog"]), None)
    sf_class = next(iter(classes["StageFailedLog"]), None)
    gb_class = next(iter(classes.get("GetBoxLog", [])), None)
    die_class = next(iter(classes.get("HeroDieLog", [])), None)
    res_class = next(iter(classes.get("ResurrectionLog", [])), None)
    # Pick the managers by STRUCTURAL VALIDATION (not 1st-in-range): the scan returns dozens of
    # false-positives; a junk slot with size=0 at a lower address shadowed the real LogManager
    # → the list never grew → NO run closed. Canonical caps: MSM=2000, LM=100000 (LOG_LIST
    # grows the whole session). See _pick_list_singleton/_valid_list_size.
    msm = _pick_list_singleton(reader, inst["MonsterSpawnManager"], MonsterSpawnManager.MONSTER_LIST, 2000)
    lm = _pick_list_singleton(reader, inst["LogManager"], LogManager.LOG_LIST, 100000)
    # infra-log: structural pick of the managers (instance-selection). A badly-picked LM (dead list)
    # = NO run closes (historical "runs don't close" bug). Logging cands/picked makes this visible.
    diag(f"[manager-pick] MSM cands={len(inst['MonsterSpawnManager'])} picked={hex(msm) if msm else None}; "
         f"LM cands={len(inst['LogManager'])} picked={hex(lm) if lm else None}")
    csd_list = list(inst.get("CommonSaveData", []))
    psd_list = list(inst.get("PlayerSaveData", []))
    sm_list = list(inst.get("StageManager", []))
    catalog_instances = {n: list(inst.get(n, [])) for n in ("StageInfoData", "ItemInfoData")}
    stage_info, item_cat, hero_cat = _read_catalogs(reader, inst)
    # Live gold: resolves the AggregateManager klass by STRUCTURE (name-free; see metrics.gold).
    # All gold logic lives in gold.py — here (and in the rest of the meter) we ONLY call.
    _tg = time.time()
    gold_klass = resolve_combat_gold_klass(reader, psd_list)
    print(f"[resolve] gold singleton (writable value-scan) in {time.time() - _tg:.1f}s")
    resolved = _Resolved(sc_class=sc_class, sf_class=sf_class, msm=msm, lm=lm,
                         csd_list=csd_list, psd_list=psd_list, stage_info=stage_info,
                         item_cat=item_cat, hero_cat=hero_cat, sm_list=sm_list,
                         gold_klass=gold_klass, gb_class=gb_class, die_class=die_class,
                         res_class=res_class, catalog_instances=catalog_instances)
    return resolved, classes


def _calibrate(reader, pid, fp, cache_path, classes, stage_info, item_cat, hero_cat, gold_klass):
    """CALIBRATES after a full scan: discovers the anchor_rva + indices + idx_ut and persists them in
    calib[fp] (deliverable 02). All discovery logic lives in typeinfo/gold — here we only
    orchestrate + persist. NEVER breaks the flow (failing = just no speedup next time).

    The idx_ut comes from a cheap table WALK: by value==gold_klass (gold_index_of_klass) when the
    scan already bootstrapped `gold_klass`, OR — when the value-scan does NOT converge (gold_klass None: the
    save lagged the live value, seen in 1.00.11) — by STRUCTURE (gold_index_by_structure: the index whose
    table[idx] passes combat_gold_klass_ok, the SAME gate as the fast path). Both name-free and without re-running
    the ~40s value-scan. (History: find_gold_index(reader, tbase, []) with an empty psd made the
    value-scan fail → idx_ut None → calib NEVER saved → the fast path never activated. Bug.)

    CARRIED OBLIGATION (validation 01): if discovery fails (anchor OR idx_ut None), EMIT
    a clear log-event — a build that "never speeds up" must be OBSERVABLE (stdout goes to
    meter.log + the app's relay). Without fp/ga_base there's no way to calibrate (bows out quietly)."""
    if not fp:
        return
    ga_base, ga_size = typeinfo.ga_module(pid)
    if not ga_base or not ga_size:
        print("[calib] FAILED to read GameAssembly.dll module — build will keep scanning")
        return
    regs = regions(reader)
    known_K = {name: next(iter(classes[name])) for name in classes if classes[name]}
    disc = typeinfo.discover_anchor(reader, ga_base, ga_size, known_K, regs)
    if disc is None:
        print("[calib] FAILED to discover anchor — build will keep scanning")
        return
    anchor_rva, tbase2, indices = disc
    # idx_ut: reuse the scan's klass as a shortcut (gold_index_of_klass) when it exists; otherwise — value-scan
    # didn't converge (gold_klass None) — derive by STRUCTURE (gold_index_by_structure), name-free and
    # save-independent. Both walk the same table; both avoid re-running the value-scan.
    idx_ut = gold_index_of_klass(reader, tbase2, gold_klass) if gold_klass else None
    if idx_ut is None:
        idx_ut = gold_index_by_structure(reader, tbase2)
    if idx_ut is None:
        print("[calib] FAILED to locate gold idx in table — build will keep scanning")
        return
    save_calib(cache_path, fp, anchor_rva, indices, idx_ut, stage_info, item_cat, hero_cat)
    print(f"[calib] anchor_rva={hex(anchor_rva)} idx_ut={idx_ut} indices={len(indices)} — "
          "fast path armed for this build")


def _resolve_all(reader, pid, fp, cache_path, ga_base):
    """Orchestrates resolution GATED by build fingerprint (§1: thin — all RVA logic lives
    in typeinfo/resolver/gold). Fallback chain (§6): calib[fp] (build-stable) → scan+calibrate.
    Reusable at startup AND re-attach. Returns named resolution data, including catalog instances.

    `pid` is needed for scan calibration; `ga_base` was read by prepare; `fp` is the
    build fingerprint (computed in run() via _detect_game_version, the calib key);
    `cache_path` is the resolve_cache.json (respects --output)."""
    # FAST PATH: known build → resolve by index/bbwf (~ms), no scan.
    if fp and ga_base:
        calib = load_calib(cache_path, fp)
        if calib:
            resolved = _resolve_fast(reader, ga_base, calib)
            if resolved is not None:
                print(f"[calib] fast path (fp {fp}) — resolved via RVA, no scan")
                diag(f"[resolve] path=FAST fp={fp} (calib hit, RVA, no scan)")
                _autocalibrate_fields(reader, resolved, fp)
                return resolved
            print("[calib] fast path sanity-fail (RVA/idx/size) — falling back to scan")
            diag(f"[resolve] fast-path SANITY-FAIL (RVA/idx/size) → cold scan fp={fp}")
        else:
            diag(f"[resolve] calib MISS (seed+cache don't cover fp) → cold scan fp={fp}")
    else:
        diag(f"[resolve] no fp/ga_base (fp={fp} ga_base={hex(ga_base) if ga_base else None}) → cold scan")
    # SLOW PATH: guaranteed scan + calibrate at the end (persist-gate in save_calib).
    _emit_status("scanning")   # app: splash shows "first time on this version, mapping (~1 min)"
    print("resolving classes/instances (~1-2min)...")
    resolved, classes = _resolve_scan(reader)
    if resolved.complete:
        _calibrate(reader, pid, fp, cache_path, classes, resolved.stage_info,
                   resolved.item_cat, resolved.hero_cat, resolved.gold_klass)
        diag(f"[resolve] path=SCAN fp={fp} → calibrated (persisted)")
    else:
        diag(f"[resolve] path=SCAN fp={fp} → NOT calibrated (incomplete: "
             f"msm={bool(resolved.msm)} lm={bool(resolved.lm)} "
             f"sc={bool(resolved.sc_class)} sf={bool(resolved.sf_class)})")
    _autocalibrate_fields(reader, resolved, fp)
    return resolved


def _autocalibrate_fields(reader, resolved, fp):
    """Auto-discover the four FRAGILE field offsets (Monster.STAGE_KEY, UnitHealthController.HP_MAX,
    StageManager.HERO_LIST, Unit.CACHE) by semantic signature and apply them in-memory over the static
    offsets, which stay as the fallback. Runs after BOTH resolve paths so the offsets are correct
    before any reading. Read-only; nothing is written to the game or disk. A signature not found live
    (e.g. not in combat) simply keeps the static offset — never makes a working build worse."""
    try:
        from game import field_calib
        field_calib.calibrate(reader, resolved.msm, resolved.sm_list,
                              resolved.stage_info, resolved.hero_cat,
                              log=lambda m: (print(m), diag(m)))
    except Exception as e:
        diag(f"[field-calib] skipped (error: {e}) — static offsets kept")


def prepare(reader, pid, fp, cache_path, output_dir, game_version, *, startup):
    """Resolve one attachment and pick its carriers, or return None if incomplete.

    Owns attach-only reads/diagnostics and catalog export. The caller owns the
    ready/searching markers, retry loop, and every per-run state transition.
    """
    t0 = time.time()
    ga_base, _ = typeinfo.ga_module(pid)
    if startup:
        resolved = _resolve_all(reader, pid, fp, cache_path, ga_base)
    else:
        # Re-attach has always retried when RESOLUTION raised (and only then): preserve that.
        try:
            resolved = _resolve_all(reader, pid, fp, cache_path, ga_base)
        except Exception:
            return None
    calib = load_calib(cache_path, fp) if fp else None
    if calib and ga_base:
        tbase = typeinfo.table_base(reader, ga_base, calib["anchor_rva"])
        idx_ut = calib["idx_ut"]
    else:
        tbase = idx_ut = None
    stage_info, item_cat, hero_cat = resolved.stage_info, resolved.item_cat, resolved.hero_cat
    csd_list, psd_list, sm_list = resolved.csd_list, resolved.psd_list, resolved.sm_list
    gold_klass = resolved.gold_klass
    print(f"[ok] resolved in {time.time()-t0:.0f}s. stages={len(stage_info)} "
          f"items-catalog={len(item_cat)} heroes-catalog={len(hero_cat)} "
          f"PSD={len(psd_list)} CSD={len(csd_list)}.\n")
    diag(f"[resolve] fp={fp} SM={len(sm_list)} PSD={len(psd_list)} CSD={len(csd_list)} "
         f"gold={'ok' if gold_klass else 'None'} stages={len(stage_info)} "
         f"items={len(item_cat)} heroes={len(hero_cat)}")
    if not resolved.complete:
        diag(f"[resolve] INCOMPLETE → {'abort' if startup else 'retry'} "
             f"(msm={bool(resolved.msm)} lm={bool(resolved.lm)} "
             f"sc={bool(resolved.sc_class)} sf={bool(resolved.sf_class)})")
        return None
    csd = save.pick_live_csd(reader, csd_list, stage_info)
    sm = save.pick_live_sm(reader, sm_list, hero_cat)
    # Startup and re-attach run the SAME carrier/save/gold checks (a re-attach after a game relaunch
    # used to keep a stale gold klass until a run self-healed); only the [party-pick] label differs.
    print(f"[live party] StageManager {'ok' if sm else 'NOT found (no live party/xp)'}"
          f" — {len(build.read_live_party(reader, sm, hero_cat))} heroes deployed.")
    # infra-log: the pick decision in detail — candidates, REAL carriers vs ghosts (heroKey ok but not a
    # catalog hero), which one was picked + a sample of ghosts. THIS was missing in the 1.00.13 debug:
    # "carriers=0 picked=0x.. party_read=0" would have pointed at the cause on the spot.
    _smd = build.describe_sm_candidates(reader, sm_list, sm, hero_cat)
    label = "startup" if startup else "re-attach"
    diag(f"[party-pick] {label} candidates={_smd['total']} hk-accept={_smd['hk_accept']} "
         f"carriers={_smd['carriers']} picked={hex(_smd['picked']) if _smd['picked'] else None} "
         f"party_read={len(build.read_live_party(reader, sm, hero_cat))}")
    for _ga, _gh in _smd["ghosts"]:
        diag(f"[party-pick]   ghost {hex(_ga)} heroes(hk,lvl,exp)={_gh}")
    # infra-log: the SAVE pick — PSD (the build's gold/heroes source) and CSD (current stage). PSD None was
    # the 1.00.12 bug (save offsets shifted → read_gold=0 → pick_live_psd None → run with no
    # heroes/gold). Logging psd/gold/heroes here makes that failure mode visible.
    _psd = save.pick_live_psd(reader, psd_list)
    diag(f"[save-pick] psd_cands={len(psd_list)} psd={hex(_psd) if _psd else None} "
         f"gold={save.read_gold(reader, _psd) if _psd else None} "
         f"heroes={len(save.read_heroes(reader, _psd)) if _psd else 0}; "
         f"csd_cands={len(csd_list)} csd={hex(csd) if csd else None}")
    # Live gold: reads GoldEarn[SubKey1] from the LIVE AggregateManager, resolved by STRUCTURE (name-free,
    # immune to the obfuscated name changing between builds; see metrics.gold). Reuses the cached klass if still
    # valid (cheap); otherwise resolves. All the logic lives in gold.py — here we just call.
    if not (gold_klass and combat_gold_klass_ok(reader, gold_klass)):
        # §6 fallback: primary index (~ms, calibrated build) → value-scan fallback (~90s, no calib
        # or index failed). resolve_combat_gold_klass_by_index already has the anti-poison gate (round-trip),
        # so None = bad index/no calib → falls back to the value-scan, NEVER serves a wrong klass.
        gold_klass = None
        if tbase and idx_ut is not None:
            gold_klass = resolve_combat_gold_klass_by_index(reader, tbase, idx_ut)
        if not gold_klass:
            gold_klass = resolve_combat_gold_klass(reader, psd_list)
    if gold_klass:
        print(f"[live gold] AggregateManager klass {hex(gold_klass)} — GoldEarn[SubKey1] live "
              f"(exact per-run, lag-free, excludes selling; immune to name drift)")
    else:
        print("[live gold] NOT resolved — combat gold from save (stale, fallback)")
    # infra-log: resolved gold + live value (combat). klass=None or an absurd live (0 / 1.97T) =
    # the gold source broke — historical gold bugs (value-scan catching frozen=0 or junk).
    diag(f"[gold] klass={hex(gold_klass) if gold_klass else None} "
         f"live={combat_gold_live(reader, gold_klass) if gold_klass else None}")
    # Live drop catalog (box keys/rates/cooldowns) for the app's chest features — once per resolve.
    write_drop_catalog(reader, output_dir, stage_info, game_version, resolved.catalog_instances)
    # Per-stage (WaveAmount, WaveMonsterAmount) for the Brevity-corrected mob total (game.waves).
    stage_waves = waves.read_stage_waves(reader, resolved.catalog_instances.get("StageInfoData"))
    return AttachedState(**{**vars(resolved), "gold_klass": gold_klass},
                         tbase=tbase, idx_ut=idx_ut, csd=csd, sm=sm, stage_waves=stage_waves)
