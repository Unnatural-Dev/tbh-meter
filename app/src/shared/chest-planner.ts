import type { ChestPlannerAutoData } from "./chest-planner-inputs.js";
import snapshot from "./data/chest-planner.json";

/** Empirical TBH Index model assumptions, bundled locally; these do not predict readiness. */
export const CHEST_TRANSITION_SECONDS = snapshot.transitionSeconds;
export const PLAGUE_CHEST_CAP_PER_HOUR = snapshot.plagueCombinedCapPerHour;
export const MAX_CLEAR_SECONDS = 3600;
export const MAX_CHEST_BONUS_PERCENT = 10000;

export interface ChestDropBonuses {
  generalBossPercent: number;
  generalNormalPercent: number;
  plagueBossPercent: number;
  plagueNormalPercent: number;
}
export interface ChestPlannerSettings {
  clearTimes: Record<number, number>;
  bonusMode: "auto" | "custom" | "maxed";
  /** Auto-discovered stages hidden by the owner, independent of saved overrides. */
  excludedStages?: number[];
  bonuses: ChestDropBonuses;
}
export const MAX_CHEST_BONUSES: Readonly<ChestDropBonuses> = snapshot.maxedBonuses;
export const DEFAULT_CHEST_PLANNER: ChestPlannerSettings = {
  clearTimes: {},
  bonusMode: "auto",
  bonuses: { generalBossPercent: 0, generalNormalPercent: 0, plagueBossPercent: 0, plagueNormalPercent: 0 },
};
export interface PlannerBox {
  name: string;
  grade: string;
  tier: string;
  cooldownSeconds: number;
}
export interface PlannerStage {
  key: number;
  act: number;
  stageNo: number;
  difficulty: string;
  level: number;
  name: string;
  boxKey: number;
  monsterBoxKey: number | null;
  /** Datamined rates are per thousand, before rune/pet bonuses. */
  bossRate: number;
  monsterRate: number;
  monsters: number;
}
export const plannerBoxes = new Map<number, PlannerBox>(
  Object.entries(snapshot.boxes).map(([key, box]) => [Number(key), box]),
);
export const plannerStages: PlannerStage[] = snapshot.stages.filter(
  (stage) => (plannerBoxes.get(stage.boxKey)?.cooldownSeconds ?? 0) > 0,
);
const stageKeys = new Set(plannerStages.map((stage) => stage.key));

function record(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
function validClearSeconds(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 1 && value <= MAX_CLEAR_SECONDS;
}

/** Unknown stages and invalid fields are discarded at both load and IPC persist boundaries. */
export function sanitizePlannerSettings(value: unknown): ChestPlannerSettings {
  const raw = record(value);
  const clearTimes: Record<number, number> = {};
  for (const [key, seconds] of Object.entries(record(raw.clearTimes))) {
    const stageKey = Number(key);
    if (stageKeys.has(stageKey) && validClearSeconds(seconds)) clearTimes[stageKey] = seconds;
  }
  const rawBonuses = record(raw.bonuses);
  const bonuses = { ...DEFAULT_CHEST_PLANNER.bonuses };
  for (const key of Object.keys(bonuses) as (keyof ChestDropBonuses)[]) {
    const bonus = rawBonuses[key];
    if (typeof bonus === "number" && Number.isFinite(bonus)) {
      bonuses[key] = Math.min(MAX_CHEST_BONUS_PERCENT, Math.max(0, bonus));
    }
  }
  const bonusMode = raw.bonusMode === "maxed" || raw.bonusMode === "custom" ? raw.bonusMode : "auto";
  const excludedStages = Array.isArray(raw.excludedStages)
    ? [...new Set(raw.excludedStages.filter((key): key is number => typeof key === "number" && stageKeys.has(key)))] : [];
  return { clearTimes, bonusMode, bonuses, ...(excludedStages.length ? { excludedStages } : {}) };
}

/** Seconds or m:ss, optionally to millisecond precision; no partial numeric coercion. */
export function parseClearTime(text: string): number | null {
  const value = text.trim();
  let seconds: number;
  if (/^\d+(?:\.\d{1,3})?$/.test(value)) seconds = Number(value);
  else {
    const parts = /^(\d+):([0-5]\d(?:\.\d{1,3})?)$/.exec(value);
    if (!parts) return null;
    seconds = Number(parts[1]) * 60 + Number(parts[2]);
  }
  return validClearSeconds(seconds) ? seconds : null;
}

export function isPlagueStage(stage: PlannerStage): boolean {
  return stage.act >= 21 && stage.act <= 23;
}
function cadence(clearSeconds: number): number {
  return clearSeconds + CHEST_TRANSITION_SECONDS;
}
function runsPerCooldown(clearSeconds: number, cooldownSeconds: number): number {
  return Math.max(1, Math.ceil(cooldownSeconds / cadence(clearSeconds)));
}
function chance(rate: number, bonusPercent: number): number {
  if (!Number.isFinite(rate) || !Number.isFinite(bonusPercent)) return 0;
  return Math.max(0, Math.min(1, rate / 1000 * (1 + bonusPercent / 100)));
}
function perHour(clearSeconds: number, cooldownSeconds: number, probability: number): number {
  if (cooldownSeconds <= 0 || probability <= 0) return 0;
  // After the cooldown rounds up to whole runs, failed rolls add a geometric wait.
  const runs = runsPerCooldown(clearSeconds, cooldownSeconds) + (1 - probability) / probability;
  return 3600 / (runs * cadence(clearSeconds));
}
export interface ChestRates {
  bossPerHour: number;
  monsterPerHour: number;
  totalPerHour: number;
  rateLimited: boolean;
  bossRuns: number;
  monsterRuns: number;
}
const ZERO_RATES: ChestRates = {
  bossPerHour: 0, monsterPerHour: 0, totalPerHour: 0,
  rateLimited: false, bossRuns: 0, monsterRuns: 0,
};

/** Expected throughput, not a guaranteed number of drops or an elapsed-time countdown. */
export function calculateChestRates(
  stage: PlannerStage,
  clearSeconds: number,
  bonuses: ChestDropBonuses,
): ChestRates {
  const bossCooldown = plannerBoxes.get(stage.boxKey)?.cooldownSeconds ?? 0;
  if (!validClearSeconds(clearSeconds) || bossCooldown <= 0) return { ...ZERO_RATES };
  const monsterCooldown = stage.monsterBoxKey == null ? 0
    : plannerBoxes.get(stage.monsterBoxKey)?.cooldownSeconds ?? 0;
  const plague = isPlagueStage(stage);
  const bossProbability = chance(stage.bossRate,
    bonuses.generalBossPercent + (plague ? bonuses.plagueBossPercent : 0));
  const monsterPerKill = chance(stage.monsterRate,
    bonuses.generalNormalPercent + (plague ? bonuses.plagueNormalPercent : 0));
  const monsterProbability = monsterCooldown > 0 && Number.isFinite(stage.monsters) && stage.monsters > 0
    ? 1 - (1 - monsterPerKill) ** stage.monsters : 0;
  const bossRuns = runsPerCooldown(clearSeconds, bossCooldown);
  const monsterRuns = monsterCooldown > 0 ? runsPerCooldown(clearSeconds, monsterCooldown) : 0;
  let bossPerHour: number;
  let monsterPerHour: number;
  if (plague) {
    const clearsPerHour = 3600 / cadence(clearSeconds);
    bossPerHour = clearsPerHour * bossProbability;
    monsterPerHour = clearsPerHour * monsterProbability;
    // The website caps the combined Plaguelands model using its measured throughput.
    const scale = Math.min(1, PLAGUE_CHEST_CAP_PER_HOUR / (bossPerHour + monsterPerHour || 1));
    bossPerHour *= scale;
    monsterPerHour *= scale;
  } else {
    bossPerHour = perHour(clearSeconds, bossCooldown, bossProbability);
    monsterPerHour = perHour(clearSeconds, monsterCooldown, monsterProbability);
  }
  return {
    bossPerHour, monsterPerHour, totalPerHour: bossPerHour + monsterPerHour,
    // Flag rare-roll stages like the website; other figures are still model estimates.
    rateLimited: plague || stage.bossRate < 80
      || (monsterCooldown > 0 && stage.monsterRate > 0 && stage.monsterRate < 8),
    bossRuns, monsterRuns,
  };
}

export interface ChestTimingAdvice {
  targetSeconds: number;
  secondsToSave: number;
  gainPerHour: number;
}
export interface ChestPlanRow {
  stage: PlannerStage;
  clearSeconds: number;
  rates: ChestRates;
  advice: ChestTimingAdvice | null;
}
export interface ChestPlanGroup {
  boxKey: number;
  rows: ChestPlanRow[];
}
function timingAdvice(stage: PlannerStage, clearSeconds: number, bonuses: ChestDropBonuses): ChestTimingAdvice | null {
  // The plague model is roll-limited, so cooldown-boundary advice would be misleading.
  if (isPlagueStage(stage)) return null;
  const current = calculateChestRates(stage, clearSeconds, bonuses).totalPerHour;
  const candidates = [stage.boxKey, stage.monsterBoxKey].flatMap((key) => {
    const cd = key == null ? 0 : plannerBoxes.get(key)?.cooldownSeconds ?? 0;
    if (cd <= 0) return [];
    const runs = runsPerCooldown(clearSeconds, cd);
    const target = Math.ceil(cd / runs - CHEST_TRANSITION_SECONDS);
    return target > 0 && target < clearSeconds ? [target] : [];
  });
  let best: ChestTimingAdvice | null = null;
  for (const target of candidates) {
    const gain = calculateChestRates(stage, target, bonuses).totalPerHour - current;
    if (gain > 0.000001 && (best == null || gain > best.gainPerHour)) {
      best = { targetSeconds: target, secondsToSave: clearSeconds - target, gainPerHour: gain };
    }
  }
  return best;
}

/** Only supplied timings participate. Compare totals inside a box tier, never item value. */
export function buildChestPlan(settings: ChestPlannerSettings, auto?: ChestPlannerAutoData): ChestPlanGroup[] {
  const clean = sanitizePlannerSettings(settings);
  const bonuses = clean.bonusMode === "maxed" ? MAX_CHEST_BONUSES
    : clean.bonusMode === "auto" ? recordedChestBonuses(auto) : clean.bonuses;
  const times = { ...auto?.clearTimes, ...clean.clearTimes };
  const excluded = new Set(clean.excludedStages);
  const groups = new Map<number, ChestPlanRow[]>();
  for (const stage of plannerStages) {
    const clearSeconds = times[stage.key];
    if (excluded.has(stage.key) || !validClearSeconds(clearSeconds)) continue;
    const rows = groups.get(stage.boxKey) ?? [];
    rows.push({ stage, clearSeconds, rates: calculateChestRates(stage, clearSeconds, bonuses),
      advice: timingAdvice(stage, clearSeconds, bonuses) });
    groups.set(stage.boxKey, rows);
  }
  return [...groups].map(([boxKey, rows]) => ({ boxKey,
    rows: rows.sort((a, b) => b.rates.totalPerHour - a.rates.totalPerHour || a.stage.key - b.stage.key),
  })).sort((a, b) => b.boxKey - a.boxKey);
}

/** Missing sources contribute no known bonus; the view separately reports their availability. */
export function recordedChestBonuses(auto?: ChestPlannerAutoData): ChestDropBonuses {
  const bonuses = { ...DEFAULT_CHEST_PLANNER.bonuses };
  for (const field of Object.keys(bonuses) as (keyof ChestDropBonuses)[]) {
    bonuses[field] = (auto?.runeBonuses?.[field] ?? 0) + (auto?.petBonuses?.[field] ?? 0);
  }
  return bonuses;
}
