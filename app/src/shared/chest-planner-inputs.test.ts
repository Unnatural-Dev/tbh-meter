import { describe, expect, it } from "vitest";
import { bonusesFromPets, bonusesFromRunes, recordedChestTimes, EMPTY_CHEST_AUTO } from "./chest-planner-inputs.js";
import { buildChestPlan, calculateChestRates, DEFAULT_CHEST_PLANNER, plannerStages, recordedChestBonuses } from "./chest-planner.js";
import type { RunIndexEntry } from "./ipc-types.js";
import snapshot from "./data/chest-planner.json";

const run = (overrides: Partial<RunIndexEntry> = {}): RunIndexEntry => ({
  id: "1", ts: 1, stage: "1-1", stageNo: 1, mode: "Normal", status: "success", quality: "counted", clearTime: 58,
  ...overrides,
} as RunIndexEntry);

describe("automatic chest planner inputs", () => {
  it("reconciles all unlocked pets and fully purchased runes with the maxed account", () => {
    const petBonuses = bonusesFromPets(Object.keys(snapshot.petBonuses).map((key) => ({ key: Number(key), unlocked: true })));
    const runeBonuses = bonusesFromRunes(Object.entries(snapshot.runeBonuses).map(([key, levels]) => ({ key: Number(key), level: levels.length })));
    expect(petBonuses).toEqual({ generalBossPercent: 25, generalNormalPercent: 45, plagueBossPercent: 0, plagueNormalPercent: 0 });
    expect(recordedChestBonuses({ ...EMPTY_CHEST_AUTO, petBonuses, runeBonuses })).toEqual(snapshot.maxedBonuses);
  });
  it("adds only unlocked pets and rejects incomplete, duplicate, or unknown ownership", () => {
    expect(bonusesFromPets([{ key: 1001, unlocked: true }, { key: 1003, unlocked: false }])).toEqual({
      ...DEFAULT_CHEST_PLANNER.bonuses, generalNormalPercent: 10,
    });
    expect(bonusesFromPets([{ key: 1003, unlocked: true }])?.generalBossPercent).toBe(10);
    expect(bonusesFromPets([])).toEqual(DEFAULT_CHEST_PLANNER.bonuses);
    for (const value of [undefined, {}, [null], [{ key: 1001 }], [{ key: 1001, unlocked: 1 }],
      [{ key: 999999, unlocked: false }], [{ key: 1001, unlocked: true }, { key: 1001, unlocked: true }]]) {
      expect(bonusesFromPets(value)).toBeNull();
    }
  });

  it("combines independent recorded rune and pet bonuses without mutating overrides", () => {
    const auto = { ...EMPTY_CHEST_AUTO, runeBonuses: bonusesFromRunes([{ key: 102, level: 2 }]),
      petBonuses: bonusesFromPets([{ key: 1003, unlocked: true }]), clearTimes: { 1101: 58 } };
    expect(recordedChestBonuses(auto).generalBossPercent).toBe(50);
    expect(recordedChestBonuses({ ...auto, runeBonuses: null }).generalBossPercent).toBe(10);
    const stage = plannerStages.find((s) => s.key === 1101)!;
    expect(buildChestPlan(DEFAULT_CHEST_PLANNER, auto)[0].rows[0].rates).toEqual(
      calculateChestRates(stage, 58, recordedChestBonuses(auto)));
    const manual = { ...DEFAULT_CHEST_PLANNER, bonusMode: "custom" as const,
      bonuses: { ...DEFAULT_CHEST_PLANNER.bonuses, generalBossPercent: 123 } };
    expect(buildChestPlan(manual, auto)[0].rows[0].rates).toEqual(calculateChestRates(stage, 58, manual.bonuses));
    expect(manual.bonuses.generalBossPercent).toBe(123);
  });
  it("takes the median of the ten newest complete successful runs per stage", () => {
    const runs = Array.from({ length: 11 }, (_, i) => run({ ts: i, clearTime: 50 + i }));
    const bad = [run({ quality: "partial", clearTime: 1 }), run({ quality: "degraded" }),
      run({ status: "fail" }), run({ quality: "skipped" }), run({ clearTime: NaN }),
      run({ clearTime: 0 }), run({ stage: "99-99" }), run({ mode: "?" })];
    const input = recordedChestTimes([...bad, ...runs]);
    expect(input.clearTimes).toEqual({ 1101: 55.5 });
    expect(input.sampleCounts).toEqual({ 1101: 10 });
  });

  it("resolves Plaguelands and legacy complete successes without inventing stages", () => {
    const input = recordedChestTimes([run({ stage: "21-1", mode: "Nightmare", quality: undefined }),
      run({ stage: "1-2", stageNo: 2, clearTime: 60 })]);
    expect(input.clearTimes).toEqual({ 201201: 58, 1102: 60 });
  });

  it("keeps fractional seconds on either side of the cooldown cliff", () => {
    const stage = plannerStages.find((s) => s.key === 1101)!;
    const faster = calculateChestRates(stage, 57.54, DEFAULT_CHEST_PLANNER.bonuses);
    const boundary = calculateChestRates(stage, 57.55, DEFAULT_CHEST_PLANNER.bonuses);
    expect(faster.bossRuns).toBe(3);
    expect(boundary.bossRuns).toBe(2);
  });

  it("sums purchased rune levels once per key and distinguishes missing from zero", () => {
    expect(bonusesFromRunes(undefined)).toBeNull();
    expect(bonusesFromRunes([{ key: 101, level: -1 }])).toBeNull();
    expect(bonusesFromRunes([{ key: 101, level: 100 }])).toBeNull();
    expect(bonusesFromRunes([])).toEqual(DEFAULT_CHEST_PLANNER.bonuses);
    expect(bonusesFromRunes([{ key: 101, level: 2 }, { key: 101, level: 1 }, { key: 102, level: 1 }, { key: 10, level: 1 }])).toEqual({
      generalBossPercent: 20, generalNormalPercent: 20, plagueBossPercent: 0, plagueNormalPercent: 0,
    });
  });

  it("keeps manual times and bonus overrides independent of new automatic readings", () => {
    const auto = { ...EMPTY_CHEST_AUTO, clearTimes: { 1101: 57.54, 1102: 49 }, sampleCounts: { 1101: 2, 1102: 3 },
      runeBonuses: { ...DEFAULT_CHEST_PLANNER.bonuses, generalBossPercent: 200 } };
    const manual = { ...DEFAULT_CHEST_PLANNER, clearTimes: { 1101: 58 }, bonusMode: "custom" as const,
      bonuses: { ...DEFAULT_CHEST_PLANNER.bonuses, generalNormalPercent: 15 } };
    const original = structuredClone(manual);
    const rows = buildChestPlan(manual, auto)[0].rows;
    expect(rows.find((row) => row.stage.key === 1101)?.clearSeconds).toBe(58);
    expect(rows.find((row) => row.stage.key === 1102)?.clearSeconds).toBe(49);
    expect(manual).toEqual(original);
    const hidden = buildChestPlan({ ...manual, excludedStages: [1101] }, auto)[0].rows;
    expect(hidden.map((row) => row.stage.key)).toEqual([1102]);
    const automatic = buildChestPlan(DEFAULT_CHEST_PLANNER, auto)[0].rows;
    expect(automatic.find((row) => row.stage.key === 1101)?.clearSeconds).toBe(57.54);
  });
});
