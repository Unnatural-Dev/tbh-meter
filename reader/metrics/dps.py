"""DPS measured from monsters' HP DROP — the meter's headline number.

Idea: each tick, read every live monster's HP. How much HP fell since the
previous tick IS the damage dealt. Sum that over a sliding window -> DPS.

Cases handled:
  - new monster (not seen before): record it, count no damage.
  - HP went up (heal, or address reused from the pool by a new monster):
    ignore, don't count as damage.
  - monster gone from the list, or HP <= 0 (died): count its remaining HP as the
    killing blow and ONE kill for that identity (total_kills).
  - invalid snapshot (monsters is None: the list could not be read): NOT "everyone died".
    The tick is skipped and the previous state kept, so monsters that come back on the
    next good read are not counted dead twice.
  - unreadable / non-finite HP for one monster (None, NaN, inf): carry its previous HP
    forward; a bad read is not a death.
  - leftovers of a FAILED run (given by RunBoundary): one that leaves before this run's first
    own spawn is not a kill, though its remaining HP still counts as damage; the first spawn
    makes the rest ordinary monsters.
"""

import math

from shared.utils import RollingWindow, now


class DpsTracker:
    def __init__(self, window_seconds: float = 5.0, leftovers=()):
        self._window = RollingWindow(window_seconds)
        self._last_hp: dict[int, float] = {}   # monster addr -> last HP seen
        self.total_damage: float = 0.0         # run accumulator
        self.peak_dps: float = 0.0
        self.alive: int = 0                     # mobs alive on the last VALID tick
        self.total_kills: int = 0               # run accumulator: monster identities that died
        self._leftovers: set[int] = set(leftovers)   # a failed run's monsters still listed

    def update(self, monsters, timestamp: float | None = None) -> None:
        """`monsters` = iterable of (addr, current_hp, hp_max) tuples for the live mobs
        (see game.models.live_monster_snapshot), or None for an invalid snapshot (skipped,
        state kept). Only (addr, hp) matter here; hp_max is ignored."""
        ts = now() if timestamp is None else timestamp

        if monsters is not None:
            current: dict[int, float] = {}
            damage = 0.0
            kills = 0
            spawned = False

            for addr, hp, *_ in monsters:
                prev = self._last_hp.get(addr)
                if hp is None or not math.isfinite(hp):
                    # bad HP read: a known monster stays alive at its last HP; an unknown one waits
                    if prev is not None:
                        current[addr] = prev
                    continue
                if hp <= 0:
                    continue            # dead this tick -> counted by the "gone" pass below
                current[addr] = hp
                if prev is None and addr not in self._leftovers:
                    spawned = True          # a monster of this run appeared
                if prev is not None and hp < prev:
                    damage += (prev - hp)   # took damage

            # monsters gone since the previous tick = died -> killing blow + one kill each
            for addr, prev_hp in self._last_hp.items():
                if addr not in current and prev_hp > 0:
                    damage += prev_hp
                    if addr in self._leftovers:
                        self._leftovers.discard(addr)   # the failed run's monster left: no kill
                    else:
                        kills += 1
            if spawned:
                self._leftovers.clear()     # this run has begun: what is still listed is its own

            self._last_hp = current
            self.alive = len(current)
            self.total_kills += kills
            if damage > 0:
                self._window.add(damage, ts)
                self.total_damage += damage

        dps = self.dps(ts)
        if dps > self.peak_dps:
            self.peak_dps = dps

    @property
    def listed(self) -> frozenset[int]:
        """Monsters alive on the last valid tick."""
        return frozenset(self._last_hp)

    def dps(self, timestamp: float | None = None) -> float:
        """Smoothed DPS (damage in the window / window size)."""
        return self._window.rate_per_second(timestamp)

    def reset(self) -> None:
        """Zero out (e.g. on stage change / splitting per run). The meter prefers to instantiate
        a fresh DpsTracker per run, but this covers in-place reuse."""
        self._window.reset()
        self._last_hp.clear()
        self.total_damage = 0.0
        self.peak_dps = 0.0
        self.alive = 0
        self.total_kills = 0
