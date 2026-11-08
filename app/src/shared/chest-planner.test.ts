import { describe, expect, it } from "vitest";
import {
  buildChestPlan, calculateChestRates, CHEST_TRANSITION_SECONDS,
  DEFAULT_CHEST_PLANNER, MAX_CHEST_BONUSES, parseClearTime,
  PLAGUE_CHEST_CAP_PER_HOUR, plannerBoxes, plannerStages, sanitizePlannerSettings,
  type ChestPlannerSettings, type PlannerStage,
} from "./chest-planner.js";

const noBonus = DEFAULT_CHEST_PLANNER.bonuses;
const stage = plannerStages.find((s) => s.key === 1101)!;
const bossOnly = (overrides: Partial<PlannerStage> = {}): PlannerStage => ({
  ...stage, monsterBoxKey: null, bossRate: 1000, ...overrides,
});
const settings = (clearTimes: Record<number, number>): ChestPlannerSettings => ({
  ...DEFAULT_CHEST_PLANNER, clearTimes,
});

describe("chest planner snapshot", () => {
  it("bundles cooldown-positive timed-stage candidates including Plaguelands", () => {
    expect(plannerStages).toHaveLength(168);
    expect(plannerStages.filter((s) => s.act >= 21)).toHaveLength(60);
    for (const s of plannerStages) {
      expect(plannerBoxes.get(s.boxKey)?.cooldownSeconds).toBeGreaterThan(0);
      if (s.monsterBoxKey != null) expect(plannerBoxes.get(s.monsterBoxKey)?.cooldownSeconds).toBeGreaterThan(0);
    }
    expect(new Set(plannerStages.map((s) => s.key)).size).toBe(plannerStages.length);
  });

  it("uses the website rune and pet maximums, not a nominal chance multiplier", () => {
    expect(MAX_CHEST_BONUSES).toEqual({
      generalBossPercent: 885, generalNormalPercent: 491,
      plagueBossPercent: 550, plagueNormalPercent: 250,
    });
    expect(CHEST_TRANSITION_SECONDS).toBe(2.45);
  });
});

describe("planner input parsing", () => {
  it.each([["58", 58], [" 58 ", 58], ["1:05", 65], ["01:05", 65], ["60:00", 3600], ["57.54", 57.54], ["1:05.5", 65.5]])(
    "parses %s as %i seconds", (input, expected) => {
      expect(parseClearTime(input)).toBe(expected);
    },
  );

  it.each(["", " ", "0", "0:00", "-1", "0.5", "1.2345", "1:60", "1:5", "60:01", "3601", "1e2", "45s", "Infinity"])(
    "rejects invalid clear time %s", (input) => expect(parseClearTime(input)).toBeNull(),
  );

  it("drops unknown stages and nonnumeric, zero, and nonfinite clear times", () => {
    expect(sanitizePlannerSettings({ clearTimes: {
      1101: 58, 1102: "45", 1103: 0, 1104: 45.5,
      1105: Infinity, 1106: 3601, 999999: 60,
    } }).clearTimes).toEqual({ 1101: 58, 1104: 45.5 });
  });

  it("sanitizes malformed persistence without leaking shared defaults", () => {
    expect(sanitizePlannerSettings(null)).toEqual(DEFAULT_CHEST_PLANNER);
    expect(sanitizePlannerSettings([])).toEqual(DEFAULT_CHEST_PLANNER);
    const parsed = sanitizePlannerSettings({ bonusMode: "unknown", bonuses: {
      generalBossPercent: -10, generalNormalPercent: 10001,
      plagueBossPercent: NaN, plagueNormalPercent: "50",
    } });
    expect(parsed).toEqual({ clearTimes: {}, bonusMode: "auto", bonuses: {
      generalBossPercent: 0, generalNormalPercent: 10000,
      plagueBossPercent: 0, plagueNormalPercent: 0,
    } });
    parsed.bonuses.generalBossPercent = 1;
    expect(DEFAULT_CHEST_PLANNER.bonuses.generalBossPercent).toBe(0);
  });

  it("does not turn prototype-like clear-time keys into stages", () => {
    const raw = JSON.parse('{"clearTimes":{"__proto__":12,"constructor":20,"1101":58},"bonusMode":"maxed"}');
    expect(sanitizePlannerSettings(raw)).toEqual({
      ...DEFAULT_CHEST_PLANNER, clearTimes: { 1101: 58 }, bonusMode: "maxed",
    });
  });
});

describe("cooldown and drop chance model", () => {
  it("rounds each cooldown to complete runs including the 2.45s transition", () => {
    const result = calculateChestRates(bossOnly(), 58, noBonus);
    expect(result.bossRuns).toBe(2);
    expect(result.bossPerHour).toBeCloseTo(3600 / (2 * 60.45));
    expect(result.monsterPerHour).toBe(0);
    expect(result.totalPerHour).toBe(result.bossPerHour);
  });

  it("reproduces the cliff where 58s beats 49s, and crossing 57s to 58s drops a full run", () => {
    const faster = calculateChestRates(bossOnly(), 49, noBonus);
    const slower = calculateChestRates(bossOnly(), 58, noBonus);
    expect(faster.bossRuns).toBe(3);
    expect(calculateChestRates(bossOnly(), 57, noBonus).bossRuns).toBe(3);
    expect(slower.bossRuns).toBe(2);
    expect(slower.totalPerHour).toBeGreaterThan(faster.totalPerHour);
  });

  it("adds the geometric wait for failed per-run rolls", () => {
    const result = calculateChestRates(bossOnly({ bossRate: 100 }), 58, noBonus);
    expect(result.bossPerHour).toBeCloseTo(3600 / ((2 + 9) * 60.45));
    expect(calculateChestRates(bossOnly({ bossRate: 0 }), 58, noBonus).totalPerHour).toBe(0);
    expect(calculateChestRates(bossOnly({ bossRate: NaN }), 58, noBonus).totalPerHour).toBe(0);
  });

  it("combines monster rolls over the stage and uses the monster cooldown independently", () => {
    const twoRolls = { ...stage, boxKey: 920051, monsterRate: 100, monsters: 2 };
    const result = calculateChestRates(twoRolls, 58, noBonus);
    const p = 1 - 0.9 ** 2;
    expect(result.bossRuns).toBe(3); // three-minute boss box
    expect(result.monsterRuns).toBe(2); // two-minute monster box
    expect(result.monsterPerHour).toBeCloseTo(3600 / ((2 + (1 - p) / p) * 60.45));
    expect(calculateChestRates({ ...twoRolls, monsters: 0 }, 58, noBonus).monsterPerHour).toBe(0);
    expect(calculateChestRates({ ...twoRolls, monsters: Infinity }, 58, noBonus).monsterPerHour).toBe(0);
    expect(calculateChestRates({ ...twoRolls, monsterRate: NaN }, 58, noBonus).monsterPerHour).toBe(0);
  });

  it("caps odds at 100% and does not apply plague-only bonuses outside Plaguelands", () => {
    const baseline = calculateChestRates(bossOnly({ bossRate: 100 }), 58, noBonus);
    const plagueOnly = calculateChestRates(bossOnly({ bossRate: 100 }), 58, {
      ...noBonus, plagueBossPercent: 1000,
    });
    expect(plagueOnly).toEqual(baseline);
    const capped = calculateChestRates(bossOnly({ bossRate: 100 }), 58, {
      ...noBonus, generalBossPercent: 10000,
    });
    expect(capped).toEqual(calculateChestRates(bossOnly(), 58, noBonus));
  });

  it("does not invent a cooldown for an unknown box", () => {
    expect(calculateChestRates(bossOnly({ boxKey: 999999 }), 58, noBonus).totalPerHour).toBe(0);
    expect(calculateChestRates({ ...stage, monsterBoxKey: 999999 }, 58, noBonus).monsterPerHour).toBe(0);
  });

  it.each([0, -1, Infinity, NaN, 3601])("returns zero rates for invalid clear seconds %s", (seconds) => {
    expect(calculateChestRates(stage, seconds, noBonus).totalPerHour).toBe(0);
  });

  it("flags rare per-clear rolls", () => {
    expect(calculateChestRates(bossOnly({ bossRate: 40 }), 58, noBonus).rateLimited).toBe(true);
    expect(calculateChestRates({ ...stage, monsterRate: 1 }, 58, noBonus).rateLimited).toBe(true);
    expect(calculateChestRates(bossOnly(), 58, noBonus).rateLimited).toBe(false);
  });

  it("models Plaguelands per-clear rolls, applies both bonus families, and scales the combined cap", () => {
    const plague = plannerStages.find((s) => s.key === 201201)!;
    const result = calculateChestRates(plague, 500, noBonus);
    expect(result.bossPerHour).toBeCloseTo(3600 / 502.45 * 0.04);
    expect(result.monsterPerHour).toBeCloseTo(3600 / 502.45 * (1 - (1 - 0.0014) ** 255));
    const maxed = calculateChestRates(plague, 1, MAX_CHEST_BONUSES);
    expect(maxed.totalPerHour).toBeCloseTo(PLAGUE_CHEST_CAP_PER_HOUR);
    expect(maxed.rateLimited).toBe(true);
    const rawBoss = 0.04 * (1 + (885 + 550) / 100);
    const rawMonster = 1 - (1 - 0.0014 * (1 + (491 + 250) / 100)) ** 255;
    expect(maxed.bossPerHour / maxed.monsterPerHour).toBeCloseTo(rawBoss / rawMonster);
  });
});

describe("per-tier planning", () => {
  it("only compares entered timings, grouped by the actual box identity", () => {
    expect(buildChestPlan(settings({}))).toEqual([]);
    const groups = buildChestPlan(settings({ 1101: 49, 1102: 58, 1104: 40, 999999: 10 }));
    expect(groups).toHaveLength(2);
    expect(groups.map((g) => g.boxKey)).toEqual([920051, 920011]);
    const commonTier = groups.find((g) => g.boxKey === 920011)!;
    expect(commonTier.rows.map((row) => row.stage.key)).toEqual([1102, 1101]);
  });

  it("switches from real bonus inputs to maxed without replacing the saved inputs", () => {
    const custom = settings({ 4301: 58 });
    const original = structuredClone(custom);
    const maxed = buildChestPlan({ ...custom, bonusMode: "maxed" })[0].rows[0];
    expect(maxed.rates.totalPerHour).toBeGreaterThan(buildChestPlan(custom)[0].rows[0].rates.totalPerHour);
    expect(custom).toEqual(original);
  });

  it("suggests an achievable faster band peak and reports the exact combined gain", () => {
    const row = buildChestPlan(settings({ 1101: 49 }))[0].rows[0];
    expect(row.advice).not.toBeNull();
    const advice = row.advice!;
    expect(advice.targetSeconds).toBe(38);
    expect(advice.secondsToSave).toBe(11);
    expect(advice.gainPerHour).toBeCloseTo(
      calculateChestRates(row.stage, advice.targetSeconds, noBonus).totalPerHour - row.rates.totalPerHour,
    );
  });

  it("does not suggest cooldown peaks for plague rolls or an existing peak", () => {
    expect(buildChestPlan(settings({ 201201: 58 }))[0].rows[0].advice).toBeNull();
    expect(buildChestPlan(settings({ 1101: 58 }))[0].rows[0].advice).toBeNull();
  });
});
