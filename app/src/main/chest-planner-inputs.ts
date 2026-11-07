import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { RunIndexEntry } from "../shared/ipc-types.js";
import { bonusesFromPets, bonusesFromRunes, recordedChestTimes, EMPTY_CHEST_AUTO, type ChestPlannerAutoData } from "../shared/chest-planner-inputs.js";

async function readRawSnapshot(path: string, isNewest: boolean): Promise<string> {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    const code = (error as NodeJS.ErrnoException | null)?.code;
    if (!isNewest || (code !== "EACCES" && code !== "EBUSY" && code !== "EPERM")) throw error;
    // A just-ingested raw can briefly be locked. Give the same refresh one more chance.
    await delay(150);
    return readFile(path, "utf8");
  }
}

/** Reads only account snapshots for existing runs; no paths or raw files are exposed over IPC.
 * Latest-first, bounded to 20 files, asynchronous so large histories don't freeze the main thread. */
export async function loadChestPlannerInputs(dir: string, index: ReadonlyArray<RunIndexEntry>): Promise<ChestPlannerAutoData> {
  const data: ChestPlannerAutoData = { ...EMPTY_CHEST_AUTO, ...recordedChestTimes(index) };
  const candidates = [...index].sort((a, b) => b.ts - a.ts)
    .filter((run) => /^\d+$/.test(run.id) && run.schemaVersion === 2).slice(0, 20);
  for (const run of candidates) {
    try {
      const raw = JSON.parse(await readRawSnapshot(join(dir, "raw", `${run.id}.json`), run === candidates[0]));
      if (raw?.id !== run.id || raw.raw_schema_version !== 2) continue;
      if (data.runeBonuses == null && raw.runes?.ok === true) {
        data.runeBonuses = bonusesFromRunes(raw.runes.value);
        if (data.runeBonuses != null) data.runeSnapshotTs = run.ts;
      }
      if (data.petBonuses == null && raw.pets?.ok === true) {
        data.petBonuses = bonusesFromPets(raw.pets.value);
        if (data.petBonuses != null) {
          data.petSnapshotTs = run.ts;
          data.petsCaptured = true;
        }
      }
      if (data.runeBonuses != null && data.petBonuses != null) break;
    } catch {
      // A pruned/missing/partially written raw must not make the planner unavailable.
    }
  }
  return data;
}
