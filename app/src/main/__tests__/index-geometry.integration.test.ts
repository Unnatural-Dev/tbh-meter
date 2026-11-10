import { afterEach, describe, expect, it, vi } from "vitest";
import { entryHarness, type EntryHarness } from "./index-test-harness.js";

let h: EntryHarness;
afterEach(async () => { if (h) await h.cleanup(); });

// Characterization only: real index.ts -> IPC -> drag/settings -> window events.
// Synthetic DIP bounds are not native Electron/high-DPI pixel evidence.
describe("actual overlay IPC and window geometry", () => {
  it.each([
    [undefined, 435], [0, 435], [419, 435], [420, 420], [760, 760],
  ])("restores saved width %s as %s and retains it when height pins", async (saved, expected) => {
    h = await entryHarness({ packaged: false, settings: saved === undefined ? {} : {
      liveBounds: { x: -900, y: 75, width: saved, height: 99 },
    } });
    const win = h.windows[0];
    expect(win.bounds.width).toBe(expected);
    h.send("meter:set-live-height", 64);
    expect(win.bounds.width).toBe(expected);
    expect(win.bounds.height).toBe(64);
    expect(win.setMinimumSize).toHaveBeenLastCalledWith(420, 64);
    expect(win.setMaximumSize).toHaveBeenLastCalledWith(0, 64);
    if (saved !== undefined) expect(win.bounds).toMatchObject({ x: -900, y: 75 });
  });

  it("freezes pinning and persistence during inflated move bounds, then settles on release", async () => {
    h = await entryHarness({ packaged: false, settings: {
      liveBounds: { x: 100, y: 80, width: 620, height: 48 },
    } });
    const win = h.windows[0];
    h.send("meter:window-drag-start", "move");
    win.transientWidth = 1240;
    h.cursor.x = 410;
    h.cursor.y = 190;
    h.send("meter:window-drag-move", { screenX: 820, screenY: 380, devicePixelRatio: 2 });
    h.send("meter:set-live-height", 70);
    h.invoke("meter:set-settings", { liveFontScale: 1.5 });
    expect(win.setPosition).toHaveBeenLastCalledWith(400, 180);
    expect(win.setBounds).not.toHaveBeenCalled();
    expect(win.setMinimumSize).not.toHaveBeenCalled();
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds).toEqual({ x: 100, y: 80, width: 620, height: 48 });
    win.transientWidth = undefined;
    h.send("meter:window-drag-end");
    expect(win.bounds).toEqual({ x: 400, y: 180, width: 620, height: 105 });
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds).toEqual({ x: 400, y: 180, width: 620, height: 105 });
    h.cursor.x = 900;
    h.send("meter:window-drag-move");
    expect(win.bounds.x).toBe(400);
  });

  it("pins from stable restored width instead of a transient inflated bounds read", async () => {
    h = await entryHarness({ packaged: false, settings: { liveBounds: { x: 100, y: 80, width: 610, height: 48 } } });
    const win = h.windows[0];
    win.transientWidth = 1220;
    h.send("meter:set-live-height", 68);
    expect(win.setBounds).toHaveBeenLastCalledWith({ x: 100, y: 80, width: 610, height: 68 });
    win.transientWidth = undefined;
    win.emit("resized");
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds?.width).toBe(610);
  });

  it("grows and shrinks using DIP cursor, clamps at minimum, and saves resized events", async () => {
    h = await entryHarness({ packaged: false });
    const win = h.windows[0];
    h.send("meter:set-live-height", 60);
    h.invoke("meter:set-settings", { liveFontScale: 1.5 });
    h.send("meter:window-drag-start", "resize");
    h.cursor.x = 850;
    h.send("meter:window-drag-move", { screenX: 1700, devicePixelRatio: 2 });
    expect(win.bounds).toEqual({ x: 100, y: 80, width: 750, height: 90 });
    h.cursor.x = 650;
    h.send("meter:window-drag-move", { screenX: 1300, devicePixelRatio: 2 });
    expect(win.bounds.width).toBe(550);
    h.cursor.x = 50;
    h.send("meter:window-drag-move");
    expect(win.bounds.width).toBe(420);
    h.send("meter:window-drag-end");
    h.send("meter:set-live-height", 62);
    expect(win.bounds).toEqual({ x: 100, y: 80, width: 420, height: 93 });
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds).toEqual(win.bounds);
  });

  it.each([[0.1, 0.8, 49], [3, 1.5, 92], [Number.NaN, 1, 61]])(
    "CSS height follows clamped zoom %s -> %s", async (scale, zoom, height) => {
      h = await entryHarness({ packaged: false });
      const win = h.windows[0];
      h.send("meter:set-live-height", 61);
      h.invoke("meter:set-settings", { liveFontScale: scale });
      expect(win.webContents.setZoomFactor).toHaveBeenLastCalledWith(zoom);
      expect(win.bounds).toMatchObject({ width: 435, height });
      expect(win.setMinimumSize).toHaveBeenLastCalledWith(420, height);
      expect(win.setMaximumSize).toHaveBeenLastCalledWith(0, height);
      h.send("meter:set-live-height", -4);
      expect(win.bounds.height).toBe(zoom === 1.5 ? 2 : 1);
    },
  );

  it("reset centers at default width in work-area DIP and persists settled bounds", async () => {
    h = await entryHarness({ packaged: false, settings: { liveBounds: { x: -5000, y: -100, width: 900, height: 48 } } });
    const win = h.windows[0];
    h.send("meter:set-live-height", 72);
    win.hide();
    h.send("meter:reset-window-position");
    expect(win.bounds).toEqual({ x: -817, y: 74, width: 435, height: 72 });
    expect(win.visible).toBe(true);
    h.send("meter:set-live-height", 80);
    expect(win.bounds.width).toBe(435);
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds).toEqual({ x: -817, y: 74, width: 435, height: 80 });
  });

  it("destroyed windows ignore geometry and activation targets a fresh restored window", async () => {
    h = await entryHarness({ packaged: false });
    const old = h.windows[0];
    h.cursor.x = 780;
    h.send("meter:window-drag-start", "resize");
    h.send("meter:window-drag-move");
    h.send("meter:window-drag-end");
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds?.width).toBe(680);
    old.destroyed = true;
    old.setBounds.mockClear();
    h.send("meter:set-live-height", 66);
    h.send("meter:reset-window-position");
    h.send("meter:window-drag-start", "resize");
    h.send("meter:window-drag-move");
    h.send("meter:window-drag-end");
    expect(old.setBounds).not.toHaveBeenCalled();
    h.app.emit("activate");
    const current = h.windows[1];
    expect(current).not.toBe(old);
    expect(current.bounds.width).toBe(680);
    current.emit("ready-to-show");
    h.send("meter:set-live-height", 66);
    expect(current.bounds).toEqual({ x: 100, y: 80, width: 680, height: 66 });
    current.bounds.x = -200;
    current.emit("moved");
    vi.advanceTimersByTime(300);
    expect(h.disk().liveBounds).toEqual({ x: -200, y: 80, width: 680, height: 66 });
    expect(old.setBounds).not.toHaveBeenCalled();
  });
});
