import { vi } from "vitest";
import { EventEmitter } from "node:events";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import type { AppSettings } from "../../shared/ipc-types.js";

export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

// Electron/process/filesystem-watcher edges only. Settings, IPC registration, drag,
// single-instance gating and the reader supervisor remain production modules.
export async function entryHarness(options: {
  primary?: boolean;
  dev?: boolean;
  packaged?: boolean;
  settings?: Partial<AppSettings>;
  bootPending?: boolean;
  ingestPending?: boolean;
  readyPending?: boolean;
} = {}) {
  vi.resetModules();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const tempRoot = process.env.TMPDIR ?? join(process.cwd(), "node_modules", ".cache");
  mkdirSync(tempRoot, { recursive: true });
  const root = mkdtempSync(join(tempRoot, "index-entry-"));
  const settingsFile = join(root, "settings.json");
  writeFileSync(settingsFile, JSON.stringify(options.settings ?? {}));
  const resources = join(root, "resources");
  mkdirSync(join(resources, "reader"), { recursive: true });
  writeFileSync(join(resources, "reader", "tbh-reader.exe"), "fixture, never executed");
  const platform = Object.getOwnPropertyDescriptor(process, "platform")!;
  const resourcesPath = Object.getOwnPropertyDescriptor(process, "resourcesPath");
  Object.defineProperty(process, "platform", { value: "win32", configurable: true });
  Object.defineProperty(process, "resourcesPath", { value: resources, configurable: true });
  const ready = deferred<void>();
  const boot = deferred<"proceed" | "updated">();
  const ingest = deferred<void>();
  const stops: string[] = [];
  const stopSnapshots: Partial<AppSettings>[] = [];
  const recordStop = (name: string) => {
    stops.push(name);
    stopSnapshots.push(JSON.parse(readFileSync(settingsFile, "utf8")) as Partial<AppSettings>);
  };
  const source = (name: string) => Object.assign(new EventEmitter(), {
    setDir: vi.fn<() => void>(), start: vi.fn<() => void>(), stop: vi.fn(() => recordStop(name)),
    all: () => [], reloadNow: vi.fn<() => void>(),
  });
  const runs = source("runs");
  const live = source("live");
  const ingestor = { setDir: vi.fn<() => void>(), start: vi.fn<() => void>(), stop: vi.fn(() => recordStop("ingestor")) };
  const children: (EventEmitter & { stdout: EventEmitter; stderr: EventEmitter; exitCode: number | null })[] = [];
  const spawn = vi.fn(() => {
    const child = Object.assign(new EventEmitter(), {
      stdout: new EventEmitter(), stderr: new EventEmitter(), exitCode: null as number | null,
    });
    children.push(child);
    return child;
  });
  const reap = vi.fn(() => recordStop("reader"));
  const windows: FakeWindow[] = [];
  class FakeWindow extends EventEmitter {
    bounds: { x: number; y: number; width: number; height: number };
    destroyed = false;
    visible = false;
    transientWidth: number | undefined;
    webContents = { setWindowOpenHandler: vi.fn<() => void>(), setZoomFactor: vi.fn<() => void>(), send: vi.fn<() => void>() };
    constructor(public options: { x?: number; y?: number; width: number; height: number; transparent?: boolean }) {
      super();
      this.bounds = { x: options.x ?? 100, y: options.y ?? 80, width: options.width, height: options.height };
      windows.push(this);
    }
    getBounds() { return { ...this.bounds, width: this.transientWidth ?? this.bounds.width }; }
    setBounds = vi.fn((bounds: typeof this.bounds) => {
      const old = this.bounds;
      this.bounds = { ...bounds };
      if (old.x !== bounds.x || old.y !== bounds.y) this.emit("moved");
      if (old.width !== bounds.width || old.height !== bounds.height) this.emit("resized");
    });
    setPosition = vi.fn((x: number, y: number) => { this.bounds = { ...this.bounds, x, y }; this.emit("moved"); });
    setMinimumSize = vi.fn<() => void>();
    setMaximumSize = vi.fn<() => void>();
    setOpacity = vi.fn<() => void>();
    setAlwaysOnTop = vi.fn<() => void>();
    isAlwaysOnTop = () => false;
    isDestroyed = () => this.destroyed;
    isVisible = () => this.visible;
    isMinimized = () => false;
    show = vi.fn(() => { this.visible = true; });
    hide = vi.fn(() => { this.visible = false; });
    focus = vi.fn<() => void>();
    restore = vi.fn<() => void>();
    loadFile = vi.fn<() => void>();
    loadURL = vi.fn<() => void>();
    close = vi.fn(() => {
      let prevented = false;
      this.emit("close", { preventDefault: () => { prevented = true; } });
      if (!prevented) { this.destroyed = true; this.emit("closed"); }
    });
    static getAllWindows() { return windows.filter((w) => !w.destroyed); }
    static fromWebContents(sender: unknown) { return windows.find((w) => w.webContents === sender); }
  }
  const app = Object.assign(new EventEmitter(), {
    isPackaged: options.packaged ?? true,
    getPath: () => root, getVersion: () => "0.0.0-test", setName: vi.fn<() => void>(),
    setLoginItemSettings: vi.fn<() => void>(), requestSingleInstanceLock: () => options.primary ?? true,
    quit: vi.fn<() => void>(), whenReady: () => ready.promise,
  });
  const handlers = new Map<string, (...args: unknown[]) => unknown>();
  const ipc = Object.assign(new EventEmitter(), {
    handle: (channel: string, handler: (...args: unknown[]) => unknown) => handlers.set(channel, handler),
  });
  const power = new EventEmitter();
  const cursor = { x: 110, y: 90 };
  const screen = {
    getCursorScreenPoint: () => ({ ...cursor }),
    getPrimaryDisplay: () => ({ workArea: { x: -1200, y: 50, width: 1200, height: 900 }, scaleFactor: 2 }),
  };
  const updater = {
    checkAndApplyBootUpdate: () => boot.promise,
    initAutoUpdate: vi.fn<() => void>(), checkForUpdatesThrottled: vi.fn<() => void>(),
    getUpdateStatus: () => ({ state: "idle" }), __devSetUpdateStatus: vi.fn<() => void>(),
    onUpdateStatus: vi.fn<() => void>(), quitAndInstallUpdate: vi.fn<() => void>(), updaterSupported: () => false, checkForUpdates: vi.fn<() => void>(),
  };
  const tray = { createTray: vi.fn<() => void>(), destroyTray: vi.fn(() => recordStop("tray")), refreshTrayMenu: vi.fn<() => void>() };
  vi.doMock("electron", () => ({ app, BrowserWindow: FakeWindow, ipcMain: ipc, powerMonitor: power,
    screen, shell: { openExternal: vi.fn<() => void>(), openPath: vi.fn<() => void>() }, nativeImage: {}, dialog: {} }));
  vi.doMock("@electron-toolkit/utils", () => ({ electronApp: { setAppUserModelId: vi.fn<() => void>() },
    optimizer: { watchWindowShortcuts: vi.fn<() => void>() }, is: { dev: options.dev ?? false } }));
  vi.doMock("../../../resources/icon.png?asset", () => ({ default: "test-icon" }));
  vi.doMock("node:child_process", () => ({ spawn, execFileSync: reap }));
  vi.doMock("../sources/runs-source.js", () => ({ getRunsSource: () => runs, setFavoritePredicate: vi.fn<() => void>() }));
  vi.doMock("../sources/live-source.js", () => ({ getLiveSource: () => live }));
  vi.doMock("../converter/ingest.js", async () => { await ingest.promise; return { getIngestor: () => ingestor }; });
  vi.doMock("../auto-update.js", () => updater);
  vi.doMock("../tray.js", () => tray);
  vi.doMock("../variant.js", () => ({ isRcBuild: () => false }));
  vi.doMock("../crash-recovery.js", () => ({ installCrashRecovery: vi.fn<() => void>() }));
  vi.doMock("../debug-info.js", () => ({ collectDebugInfo: vi.fn<() => void>() }));
  vi.doMock("../logs-archive.js", () => ({ logsDir: () => join(root, "logs"), dataDir: () => root }));
  vi.doMock("../chest-planner-inputs.js", () => ({ loadChestPlannerInputs: vi.fn<() => void>() }));
  vi.doMock("../runs-store.js", () => ({ listRuns: () => [], getRun: vi.fn<() => void>(), clearAllRuns: vi.fn<() => void>(), pruneToMaxRuns: vi.fn<() => void>() }));
  vi.doMock("../favorites-store.js", () => ({ isFavorite: () => false, toggleFavorite: vi.fn<() => void>(), invalidateFavoritesCache: vi.fn<() => void>() }));
  vi.doMock("../cooldown-tracker.js", () => ({ getCooldownState: vi.fn<() => void>(), dismissCooldown: vi.fn<() => void>(), hideCooldownInOverlay: vi.fn<() => void>(), clearAllCooldowns: vi.fn<() => void>() }));
  vi.doMock("../drop-notifier.js", () => ({ initDropNotifier: vi.fn<() => void>() }));
  vi.doMock("../session-stats.js", () => ({ requestSessionReset: vi.fn<() => void>() }));
  if (!options.bootPending) boot.resolve("proceed");
  if (!options.ingestPending) ingest.resolve();
  const reader = await import("../reader-process.js");
  const settings = await import("../settings.js");
  await import("../index.js");
  const settle = async () => { for (let i = 0; i < 30; i++) await new Promise<void>((done) => setImmediate(done)); };
  if (!options.readyPending) ready.resolve();
  await settle();
  if (!options.ingestPending) await vi.dynamicImportSettled();
  const send = (channel: string, ...args: unknown[]) => ipc.emit(channel, { sender: windows[0]?.webContents }, ...args);
  const invoke = (channel: string, ...args: unknown[]) => {
    const handler = handlers.get(channel);
    if (!handler) throw new Error(`IPC not registered: ${channel}`);
    return handler({ sender: windows[0]?.webContents }, ...args);
  };
  const quit = () => { app.emit("before-quit"); for (const w of windows) if (!w.destroyed) w.close(); app.emit("will-quit"); };
  return { app, windows, reader, settings, ready, boot, ingest, updater, tray, runs, live, ingestor,
    children, spawn, reap, stops, stopSnapshots, power, cursor, send, invoke, settle, quit,
    disk: () => JSON.parse(readFileSync(settingsFile, "utf8")) as Partial<AppSettings>,
    cleanup: async () => {
      quit(); boot.resolve("updated"); ingest.resolve(); ready.resolve();
      await vi.dynamicImportSettled(); await settle(); reader.stopReader();
      vi.clearAllTimers(); vi.useRealTimers();
      Object.defineProperty(process, "platform", platform);
      if (resourcesPath) Object.defineProperty(process, "resourcesPath", resourcesPath);
      else Reflect.deleteProperty(process, "resourcesPath");
      vi.resetModules();
      rmSync(root, { recursive: true, force: true });
    },
  };
}
export type EntryHarness = Awaited<ReturnType<typeof entryHarness>>;
