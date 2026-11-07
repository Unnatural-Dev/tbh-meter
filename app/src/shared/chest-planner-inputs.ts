import snapshot from "./data/chest-planner.json";
import { DEFAULT_CHEST_PLANNER, MAX_CLEAR_SECONDS, plannerStages, type ChestDropBonuses } from "./chest-planner.js";
import type { RunIndexEntry } from "./ipc-types.js";

export interface ChestPlannerAutoData {
  clearTimes: Record<number, number>;
  sampleCounts: Record<number, number>;
  runeBonuses: ChestDropBonuses | null;
  /** Timestamp of the source run in epoch MILLISECONDS, like RunIndexEntry.ts. */
  runeSnapshotTs: number | null;
  petBonuses: ChestDropBonuses | null;
  petSnapshotTs: number | null;
  petsCaptured: boolean;
}

export const EMPTY_CHEST_AUTO: ChestPlannerAutoData = {
  clearTimes: {}, sampleCounts: {}, runeBonuses: null, runeSnapshotTs: null,
  petBonuses: null, petSnapshotTs: null, petsCaptured: false,
};

const stageByLocation = new Map(plannerStages.map((stage) => [
  `${stage.difficulty}|${stage.act}|${stage.stageNo}`, stage.key,
]));

/** Median of the ten newest complete successful runs per stage, matching the farm planner's
 * measured-first policy. Keep fractional seconds: rounding across a cooldown cliff changes ranks. */
export function recordedChestTimes(index: ReadonlyArray<RunIndexEntry>): Pick<ChestPlannerAutoData, "clearTimes" | "sampleCounts"> {
  const samples = new Map<number, number[]>();
  for (const run of [...index].sort((a, b) => b.ts - a.ts)) {
    if (run.status !== "success" || (run.quality != null && run.quality !== "counted")) continue;
    if (!Number.isFinite(run.clearTime) || run.clearTime <= 0 || run.clearTime > MAX_CLEAR_SECONDS) continue;
    const location = /^(\d+)-(\d+)$/.exec(run.stage);
    if (!location) continue;
    const stage = stageByLocation.get(`${run.mode.toUpperCase()}|${Number(location[1])}|${run.stageNo ?? Number(location[2])}`);
    if (stage == null) continue;
    const times = samples.get(stage) ?? [];
    if (times.length < 10) times.push(run.clearTime);
    samples.set(stage, times);
  }
  const clearTimes: Record<number, number> = {};
  const sampleCounts: Record<number, number> = {};
  for (const [key, values] of samples) {
    values.sort((a, b) => a - b);
    const mid = Math.floor(values.length / 2);
    clearTimes[key] = values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2;
    sampleCounts[key] = values.length;
  }
  return { clearTimes, sampleCounts };
}

/** Rune ranks grant the sum of their purchased levels; raw values are already rank IDs.
 * Null is unreadable/unsupported. A successfully read empty tree legitimately contributes zero. */
export function bonusesFromRunes(value: unknown): ChestDropBonuses | null {
  if (!Array.isArray(value)) return null;
  const levels = new Map<number, number>();
  for (const row of value) {
    if (!row || !Number.isInteger(row.key) || !Number.isInteger(row.level) || row.level < 0) return null;
    levels.set(row.key, Math.max(levels.get(row.key) ?? 0, row.level));
  }
  const bonuses = { ...DEFAULT_CHEST_PLANNER.bonuses };
  const rules = snapshot.runeBonuses as Record<string, { field: keyof ChestDropBonuses; percent: number }[]>;
  for (const [key, level] of levels) {
    const effects = rules[key];
    if (!effects) continue; // Other rune nodes do not grant chest drop chance.
    if (level > effects.length) return null; // The game's tree has outgrown this snapshot.
    for (const effect of effects.slice(0, level)) bonuses[effect.field] += effect.percent;
  }
  return bonuses;
}

/** Every unlocked pet grants its account bonus, regardless of the cosmetic pet selected. */
export function bonusesFromPets(value: unknown): ChestDropBonuses | null {
  if (!Array.isArray(value)) return null;
  const rules = snapshot.petBonuses as Record<string, { field: keyof ChestDropBonuses; percent: number }[]>;
  const seen = new Set<number>();
  const bonuses = { ...DEFAULT_CHEST_PLANNER.bonuses };
  for (const row of value) {
    if (!row || !Number.isInteger(row.key) || typeof row.unlocked !== "boolean" || seen.has(row.key)) return null;
    seen.add(row.key);
    // A new pet may change chest odds; an outdated catalog cannot prove a zero contribution.
    const effects = rules[row.key];
    if (!effects) return null;
    if (row.unlocked) for (const effect of effects) bonuses[effect.field] += effect.percent;
  }
  return bonuses;
}
