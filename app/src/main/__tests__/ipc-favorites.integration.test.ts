import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as fs from "node:fs";
import { EventEmitter } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Exercise the registered IPC handler with real favorites, history, and pruning.
// Only Electron's IPC/window boundary and its external updater are substituted.
const userData = fs.mkdtempSync(join(tmpdir(), "tbh-favorite-ipc-ud-"));
const handlers = new Map<string, (...args: unknown[]) => unknown>();
const windows = Array.from({ length: 2 }, () => ({
  isDestroyed: () => false,
  webContents: { send: vi.fn() },
}));
vi.mock("electron", () => ({
  app: { isPackaged: false, getVersion: () => "0.0.0", getPath: () => userData },
  ipcMain: {
    handle: (name: string, fn: (...args: unknown[]) => unknown) => handlers.set(name, fn),
    on: vi.fn(),
  },
  BrowserWindow: { getAllWindows: () => windows },
  dialog: {}, shell: {}, Notification: { isSupported: () => false },
  net: { fetch: () => { throw new Error("unexpected network access"); } },
}));
vi.mock("electron-updater", () => ({ default: { autoUpdater: new EventEmitter() } }));
vi.mock("node:fs", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:fs")>();
  return { ...real, statSync: vi.fn(real.statSync), readdirSync: vi.fn(real.readdirSync) };
});

import { registerIpcHandlers } from "../ipc.js";
import { getRunsSource } from "../sources/runs-source.js";
import { getLiveSource } from "../sources/live-source.js";
import { readerEvents } from "../reader-process.js";
import { updateSettings, flushPendingSettings } from "../settings.js";
import { invalidateFavoritesCache, toggleFavorite } from "../favorites-store.js";
import { ingestPending } from "../converter/ingest.js";
import { RAW_V2_EXAMPLE } from "../../shared/__fixtures__/raw-v2.js";
import type { RunIndexEntry } from "../../shared/ipc-types.js";

let dir: string;
function invoke(channel: string, ...args: unknown[]) {
  const handler = handlers.get(channel);
  if (!handler) throw new Error(`unregistered IPC: ${channel}`);
  return handler({}, ...args);
}
function index(): RunIndexEntry[] {
  return invoke("meter:list-runs") as RunIndexEntry[];
}
function seed(ts: number) {
  fs.mkdirSync(join(dir, "raw"), { recursive: true });
  fs.writeFileSync(join(dir, "raw", `${ts}.json`), JSON.stringify({ ...RAW_V2_EXAMPLE, id: String(ts), ts }));
}

beforeEach(() => {
  dir = fs.mkdtempSync(join(tmpdir(), "tbh-favorite-ipc-"));
  updateSettings({ outputDir: dir, maxRuns: null });
  invalidateFavoritesCache();
  getRunsSource().setDir(dir);
  registerIpcHandlers({
    applyLiveSettings() {}, applyListSettings() {}, refreshTrayMenu() {}, openListWindow() {},
    setLiveHeight() {}, startWindowDrag() {}, moveWindowDrag() {}, endWindowDrag() {},
    resetLiveWindow() {}, async debugInfo() { return ""; },
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  getRunsSource().stop(); getRunsSource().removeAllListeners();
  getLiveSource().removeAllListeners(); readerEvents.removeAllListeners();
  flushPendingSettings();
  fs.rmSync(dir, { recursive: true, force: true });
  handlers.clear();
});
afterAll(() => { fs.rmSync(userData, { recursive: true, force: true }); });

describe("favorite IPC", () => {
  it("updates every window and persists the star without scanning history", () => {
    seed(1000); seed(2000);
    ingestPending(dir); getRunsSource().reloadNow();
    const original = structuredClone(getRunsSource().all());
    const stat = vi.mocked(fs.statSync);
    const listing = vi.mocked(fs.readdirSync);
    for (const favorite of [true, false]) {
      stat.mockClear(); listing.mockClear();
      for (const window of windows) window.webContents.send.mockClear();
      expect(invoke("meter:toggle-favorite", "1000")).toBe(favorite);
      expect(index().find(r => r.id === "1000")?.favorite).toBe(favorite);
      expect(index().find(r => r.id === "2000")?.favorite).toBe(false);
      expect(getRunsSource().all()).toEqual(original);
      invalidateFavoritesCache();
      expect(index().find(r => r.id === "1000")?.favorite).toBe(favorite);
      for (const window of windows) {
        expect(window.webContents.send.mock.calls.filter(([channel]) => channel === "meter:runs-changed")).toHaveLength(1);
      }
      expect(stat).not.toHaveBeenCalled();
      expect(listing).not.toHaveBeenCalled();
    }
  });

  it("prunes a newly unstarred run over the cap and never resurrects it", () => {
    for (let ts = 1000; ts <= 11000; ts += 1000) seed(ts);
    ingestPending(dir); getRunsSource().reloadNow();
    toggleFavorite("1000");
    updateSettings({ maxRuns: 10 });
    expect(index()).toHaveLength(11);
    expect(index().find(r => r.id === "1000")?.favorite).toBe(true);
    for (const window of windows) window.webContents.send.mockClear();

    expect(invoke("meter:toggle-favorite", "1000")).toBe(false);
    expect(index().map(r => r.id)).toEqual([
      "11000", "10000", "9000", "8000", "7000", "6000", "5000", "4000", "3000", "2000",
    ]);
    expect(fs.existsSync(join(dir, "raw", "1000.json"))).toBe(false);
    expect(fs.existsSync(join(dir, "logs", "1000_1000.json"))).toBe(false);
    for (const window of windows) {
      expect(window.webContents.send).toHaveBeenCalledWith("meter:runs-changed", undefined);
    }
    ingestPending(dir); getRunsSource().reloadNow();
    expect(index()).toHaveLength(10);
    expect(index().some(r => r.id === "1000")).toBe(false);
  });
});
