import { afterEach, describe, expect, it, vi } from "vitest";
import { entryHarness, type EntryHarness } from "./index-test-harness.js";

let h: EntryHarness;
afterEach(async () => { if (h) await h.cleanup(); });

describe("actual app entrypoint lifetime", () => {
  it.each(["ready", "live", "blocked", "searching deadline", "hard deadline"])("%s dismisses and disposes only splash resources", async (signal) => {
    h = await entryHarness();
    const overlay = h.windows[0];
    const splash = h.windows[1];
    overlay.emit("ready-to-show");
    expect(overlay.show).not.toHaveBeenCalled();
    if (signal === "ready") {
      h.children[0].stdout.emit("data", Buffer.from("[[STATUS]] ready\n"));
      vi.advanceTimersByTime(7999);
      expect(splash.destroyed).toBe(false);
      vi.advanceTimersByTime(1);
    } else if (signal === "live") {
      h.live.emit("live", null);
      expect(splash.destroyed).toBe(false);
      h.live.emit("live", {});
    } else if (signal === "blocked") {
      for (const delay of [0, 5000, 10000, 20000, 40000]) {
        vi.advanceTimersByTime(delay);
        h.children.at(-1)!.stdout.emit("data", Buffer.from("[[STATUS]] resolving\n"));
        h.children.at(-1)!.emit("exit", 1, null);
      }
      expect(h.reader.getReaderState()).toBe("blocked");
      vi.advanceTimersByTime(2000);
    } else if (signal === "hard deadline") {
      h.children[0].stdout.emit("data", Buffer.from("[[STATUS]] scanning\n"));
      vi.advanceTimersByTime(358_000);
      expect(splash.destroyed).toBe(false);
      vi.advanceTimersByTime(2000);
    } else {
      vi.advanceTimersByTime(44_000);
      expect(splash.destroyed).toBe(false);
      vi.advanceTimersByTime(2000);
    }
    expect(splash.destroyed).toBe(true);
    expect(overlay.show).toHaveBeenCalledOnce();
    expect(h.reader.readerEvents.listenerCount("status")).toBe(1);
    expect(h.live.listenerCount("live")).toBe(1);
    h.quit();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("quit cancels dev-preview timeouts as well as splash watches", async () => {
    h = await entryHarness({ dev: true, packaged: false });
    expect(vi.getTimerCount()).toBeGreaterThan(2);
    h.quit();
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(600_000);
    expect(h.updater.__devSetUpdateStatus).not.toHaveBeenCalled();
    expect(h.windows[0].show).not.toHaveBeenCalled();
  });
  it("quit before app readiness prevents even window and splash startup", async () => {
    h = await entryHarness({ readyPending: true });
    h.quit();
    h.ready.resolve();
    await h.settle();
    await vi.dynamicImportSettled();
    expect(h.windows).toHaveLength(0);
    expect(h.spawn).not.toHaveBeenCalled();
    expect(h.tray.createTray).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a boot-update promise resolved after quit cannot start resources", async () => {
    h = await entryHarness({ bootPending: true });
    expect(h.windows).toHaveLength(2);
    expect(h.spawn).not.toHaveBeenCalled();
    h.quit();
    h.boot.resolve("proceed");
    await h.settle();
    await vi.dynamicImportSettled();
    expect(h.spawn).not.toHaveBeenCalled();
    expect(h.runs.start).not.toHaveBeenCalled();
    expect(h.live.start).not.toHaveBeenCalled();
    expect(h.ingestor.start).not.toHaveBeenCalled();
    expect(h.updater.initAutoUpdate).not.toHaveBeenCalled();
    expect(h.reader.getReaderState()).toBe("idle");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a late ingestor import cannot start watching or install update hooks after quit", async () => {
    h = await entryHarness({ ingestPending: true });
    expect(h.spawn).toHaveBeenCalledOnce();
    expect(h.runs.start).toHaveBeenCalledOnce();
    expect(h.ingestor.start).not.toHaveBeenCalled();
    h.quit();
    h.ingest.resolve();
    await vi.dynamicImportSettled();
    await h.settle();
    expect(h.ingestor.setDir).not.toHaveBeenCalled();
    expect(h.ingestor.start).not.toHaveBeenCalled();
    expect(h.updater.initAutoUpdate).not.toHaveBeenCalled();
    expect(h.app.listenerCount("browser-window-focus")).toBe(0);
    expect(h.power.listenerCount("resume")).toBe(0);
    expect(h.reader.getReaderState()).toBe("idle");
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(600_000);
    expect(h.spawn).toHaveBeenCalledOnce();
  });

  it("flushes an edit queued during window close before every ordered stop", async () => {
    h = await entryHarness();
    expect(h.app.listenerCount("will-quit")).toBe(1);
    h.stops.length = 0;
    h.stopSnapshots.length = 0;
    h.windows[0].on("close", () => h.settings.updateSettings({ opacity: 0.37 }));
    h.quit();
    expect(h.disk().opacity).toBe(0.37);
    expect(h.stops).toEqual(["reader", "runs", "live", "ingestor", "tray"]);
    expect(h.stopSnapshots.map((s) => s.opacity)).toEqual([0.37, 0.37, 0.37, 0.37, 0.37]);
  });

  it("secondary startup and quit neither spawn nor reap the primary reader", async () => {
    h = await entryHarness({ primary: false });
    expect(h.app.quit).toHaveBeenCalledOnce();
    h.quit();
    vi.advanceTimersByTime(600_000);
    expect(h.spawn).not.toHaveBeenCalled();
    expect(h.reap).not.toHaveBeenCalled();
    expect(h.windows).toHaveLength(0);
    expect(h.runs.start).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([false, true])("quit releases real supervisor ownership and timers (pending respawn=%s)", async (exitFirst) => {
    h = await entryHarness();
    expect(h.spawn).toHaveBeenCalledOnce();
    // Dismiss splash independently so this assertion isolates supervisor teardown.
    h.live.emit("live", {});
    const child = h.children[0];
    if (exitFirst) child.emit("exit", 1, null);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    h.quit();
    expect(h.reader.getReaderState()).toBe("idle");
    expect(h.reap).toHaveBeenLastCalledWith("taskkill", ["/f", "/t", "/im", "tbh-reader.exe"], { stdio: "ignore" });
    expect(h.reap).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
    child.emit("exit", 1, null);
    child.emit("error", new Error("late child error"));
    h.reader.retryReader();
    vi.advanceTimersByTime(600_000);
    expect(h.spawn).toHaveBeenCalledOnce();
    expect(h.reader.getReaderState()).toBe("idle");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("quit disposes splash listeners and timers without revealing the overlay", async () => {
    h = await entryHarness();
    h.children[0].stdout.emit("data", Buffer.from("[[STATUS]] ready\n"));
    const overlay = h.windows[0];
    expect(overlay.show).not.toHaveBeenCalled();
    h.quit();
    // IPC fan-out remains; only the splash's additional subscriptions must go.
    expect(h.reader.readerEvents.listenerCount("status")).toBe(1);
    expect(h.live.listenerCount("live")).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(600_000);
    expect(overlay.show).not.toHaveBeenCalled();
  });
});
