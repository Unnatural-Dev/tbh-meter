import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, watch, statSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

vi.mock("node:fs", async (original) => ({
  ...await original<typeof import("node:fs")>(),
  watch: vi.fn(() => ({ close: vi.fn() })),
  readFileSync: vi.fn((...args: Parameters<typeof import("node:fs").readFileSync>) => actualRead(...args)),
  statSync: vi.fn((...args: Parameters<typeof import("node:fs").statSync>) => actualStat(...args)),
}));
const { statSync: actualStat, readFileSync: actualRead } = await vi.importActual<typeof import("node:fs")>("node:fs");
import { Ingestor, ingestPending, rawDir, logsDir } from "../converter/ingest.js";
import { RunsSource } from "../sources/runs-source.js";
import { RAW_V2_EXAMPLE } from "../../shared/__fixtures__/raw-v2.js";
import { historyJsonName } from "../history-watch.js";

let dir: string;
let ingest: Ingestor;
let source: RunsSource;
beforeEach(() => {
  vi.useFakeTimers(); vi.mocked(watch).mockClear();
  dir = mkdtempSync(join(tmpdir(), "tbh-watch-"));
  mkdirSync(rawDir(dir)); mkdirSync(logsDir(dir));
  ingest = new Ingestor(); source = new RunsSource();
});
afterEach(() => {
  ingest.stop(); source.stop(); vi.useRealTimers();
  rmSync(dir, { recursive: true, force: true });
});
function raw(id: string): string {
  const name = `${id}.json`;
  writeFileSync(join(rawDir(dir), name), JSON.stringify({ ...RAW_V2_EXAMPLE, id }));
  return name;
}
function notify(index: number, name: string | null): void {
  const callback = vi.mocked(watch).mock.calls[index][1] as (event: string, filename: string | null) => void;
  callback("rename", name);
}
describe("history watchers", () => {
  it("processes named files without restatting old history on the next poll", () => {
    raw("one"); ingest.setDir(dir); ingest.start(); source.setDir(dir); source.start();
    const name = raw("two");
    vi.mocked(statSync).mockClear();
    notify(0, name); vi.advanceTimersByTime(150);
    const newLog = readdirSync(logsDir(dir)).find((n) => n.includes("two"))!;
    notify(1, newLog); vi.advanceTimersByTime(150);
    vi.advanceTimersByTime(1_000);
    expect(source.all()).toHaveLength(2);
    expect(vi.mocked(statSync).mock.calls.some(([p]) => String(p).includes("one"))).toBe(false);
  });
  it("uses a full pass for unnamed events and cancels queued events on directory changes", () => {
    ingest.setDir(dir); ingest.start(); raw("one"); notify(0, null);
    vi.advanceTimersByTime(150);
    expect(readdirSync(logsDir(dir))).toHaveLength(1);
    raw("two"); notify(0, "two.json");
    ingest.setDir(null); vi.advanceTimersByTime(1_000);
    expect(readdirSync(logsDir(dir))).toHaveLength(1);
  });
  it("retries transient raw and log read failures without any file metadata change", () => {
    raw("one");
    vi.mocked(readFileSync).mockImplementationOnce(() => { throw new Error("temporary read failure"); });
    expect(ingestPending(dir).raws).toBe(0);
    expect(ingestPending(dir).raws).toBe(1);
    source.setDir(dir);
    vi.mocked(readFileSync).mockImplementationOnce(() => { throw new Error("temporary read failure"); });
    source.reloadNow();
    expect(source.all()).toHaveLength(0);
    source.reloadNow();
    expect(source.all()).toHaveLength(1);
  });
  it("treats unsafe names conservatively and ignores temporary files", () => {
    expect(historyJsonName(null)).toBeNull();
    expect(historyJsonName("../escape.json")).toBeNull();
    expect(historyJsonName("file.json.tmp")).toBeUndefined();
    expect(historyJsonName("file.tmp.json")).toBeUndefined();
    expect(historyJsonName(Buffer.from("run.json"))).toBe("run.json");
  });
  it("ignores a complete temporary log during full ingestion and reload", () => {
    raw("one");
    expect(ingestPending(dir).raws).toBe(1);
    const log = readdirSync(logsDir(dir))[0];
    const content = readFileSync(join(logsDir(dir), log), "utf-8");
    writeFileSync(join(logsDir(dir), log.replace(/\.json$/, ".tmp.json")), content);
    rmSync(join(logsDir(dir), log));

    source.setDir(dir); source.reloadNow();
    expect(source.all()).toHaveLength(0);
    expect(ingestPending(dir).raws).toBe(1);
    source.reloadNow();
    expect(source.all()).toHaveLength(1);
  });
});
