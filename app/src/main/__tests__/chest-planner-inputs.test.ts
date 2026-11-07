import { mkdtemp, mkdir, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadChestPlannerInputs } from "../chest-planner-inputs.js";
import type { RunIndexEntry } from "../../shared/ipc-types.js";

vi.mock("node:fs/promises", { spy: true });
vi.mock("node:timers/promises", () => ({ setTimeout: vi.fn().mockResolvedValue(undefined) }));

let dir: string;
beforeEach(async () => {
  vi.clearAllMocks();
  dir = await mkdtemp(join(tmpdir(), "tbh-chest-auto-"));
  await mkdir(join(dir, "raw"));
});
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });
const run = (id: string, ts = 1): RunIndexEntry => ({
  id, ts, schemaVersion: 2, stage: "1-1", stageNo: 1, mode: "Normal", status: "success", quality: "counted", clearTime: 58,
} as RunIndexEntry);
const raw = (id: string, fields: object) => writeFile(join(dir, "raw", `${id}.json`),
  JSON.stringify({ id, raw_schema_version: 2, ...fields }));

describe("chest planner account snapshot loading", () => {
  it("retries the newest raw once after a transient lock and returns its bonuses", async () => {
    await raw("200", { pets: { ok: true, value: [] }, runes: { ok: true, value: [{ key: 102, level: 2 }] } });
    vi.mocked(readFile).mockRejectedValueOnce(Object.assign(new Error("locked"), { code: "EACCES" }));

    const result = await loadChestPlannerInputs(dir, [run("100", 100), run("200", 200)]);

    expect(delay).toHaveBeenCalledExactlyOnceWith(150);
    expect(vi.mocked(readFile).mock.calls).toEqual([
      [join(dir, "raw", "200.json"), "utf8"],
      [join(dir, "raw", "200.json"), "utf8"],
    ]);
    expect(result.runeBonuses?.generalBossPercent).toBe(40);
    expect(result.runeSnapshotTs).toBe(200);
    expect(result.petSnapshotTs).toBe(200);
  });

  it("falls back to the previous snapshot when the newest raw stays locked", async () => {
    await raw("100", { pets: { ok: true, value: [] }, runes: { ok: true, value: [] } });
    const busy = Object.assign(new Error("locked"), { code: "EBUSY" });
    vi.mocked(readFile).mockRejectedValueOnce(busy).mockRejectedValueOnce(busy);

    const result = await loadChestPlannerInputs(dir, [run("200", 200), run("100", 100)]);

    expect(delay).toHaveBeenCalledExactlyOnceWith(150);
    expect(readFile).toHaveBeenCalledTimes(3);
    expect(result.runeSnapshotTs).toBe(100);
    expect(result.petSnapshotTs).toBe(100);
  });

  it("loads pets independently of rune failures and keeps each latest readable source", async () => {
    await raw("100", { pets: { ok: true, value: [{ key: 1003, unlocked: true }] }, runes: { ok: true, value: [] } });
    await raw("200", { pets: { ok: false }, runes: { ok: true, value: [{ key: 102, level: 2 }] } });
    await raw("300", { pets: { ok: true, value: [{ key: 1001, unlocked: true }] }, runes: { ok: false } });
    await raw("400", { pets: { ok: true, value: [{ key: 999999, unlocked: true }] } });
    const result = await loadChestPlannerInputs(dir, [run("100", 100), run("400", 400), run("200", 200), run("300", 300)]);
    expect(result.petBonuses?.generalNormalPercent).toBe(10);
    expect(result.petBonuses?.generalBossPercent).toBe(0);
    expect(result.petSnapshotTs).toBe(300);
    expect(result.petsCaptured).toBe(true);
    expect(result.runeSnapshotTs).toBe(200);
    expect(result.runeBonuses?.generalBossPercent).toBe(40);
    expect(delay).not.toHaveBeenCalled();
  });

  it("distinguishes an empty pet collection from a missing snapshot", async () => {
    await writeFile(join(dir, "raw", "100.json"), JSON.stringify({ id: "100", raw_schema_version: 2, pets: { ok: true, value: [] } }));
    const result = await loadChestPlannerInputs(dir, [run("100", 100)]);
    expect(result.petsCaptured).toBe(true);
    expect(result.petBonuses?.generalBossPercent).toBe(0);
    expect(result.runeBonuses).toBeNull();
    expect((await loadChestPlannerInputs(dir, [])).petBonuses).toBeNull();
  });
  it("uses the latest readable rune snapshot and reports its timestamp, with pets explicitly missing", async () => {
    await writeFile(join(dir, "raw", "100.json"), JSON.stringify({ id: "100", raw_schema_version: 2, runes: { ok: true, value: [{ key: 102, level: 2 }] } }));
    await writeFile(join(dir, "raw", "200.json"), JSON.stringify({ id: "200", raw_schema_version: 2, runes: { ok: false, error: "read failed" } }));
    const result = await loadChestPlannerInputs(dir, [run("200", 200), run("100", 100)]);
    expect(result.runeBonuses?.generalBossPercent).toBe(40);
    expect(result.runeSnapshotTs).toBe(100);
    expect(result.petsCaptured).toBe(false);
    expect(result.clearTimes).toEqual({ 1101: 58 });
  });

  it("tolerates missing, malformed, or mismatched raw files without fabricating zero bonuses", async () => {
    await writeFile(join(dir, "raw", "100.json"), "{");
    await writeFile(join(dir, "raw", "200.json"), JSON.stringify({ id: "different", raw_schema_version: 2, runes: { ok: true, value: [] } }));
    const result = await loadChestPlannerInputs(dir, [run("300"), run("200"), run("100"), run("../escape")]);
    expect(result.runeBonuses).toBeNull();
    expect(result.runeSnapshotTs).toBeNull();
    expect(delay).not.toHaveBeenCalled();
  });

  it("retains a successfully captured empty rune tree as a known zero", async () => {
    await writeFile(join(dir, "raw", "100.json"), JSON.stringify({ id: "100", raw_schema_version: 2, runes: { ok: true, value: [] } }));
    expect((await loadChestPlannerInputs(dir, [run("100")])).runeBonuses?.generalBossPercent).toBe(0);
  });
});
