import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, test } from "node:test";
import { packagingCommand } from "./package-windows.mjs";

const fixtures = [];
const PIN = "10.34.9";
afterEach(() => {
  for (const dir of fixtures.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function writePnpmShim(dir, label) {
  writeFileSync(join(dir, process.platform === "win32" ? "pnpm.cmd" : "pnpm"),
    process.platform === "win32" ? `@echo off\r\necho ${label}\r\n` : `#!/bin/sh\nprintf '%s\\n' '${label}'\n`,
    { mode: 0o755 });
}

function fixture({ version = PIN, local = true } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "tbh-package-launcher-"));
  fixtures.push(dir);
  mkdirSync(join(dir, "scripts"));
  const script = join(dir, "scripts", "package-windows.mjs");
  writeFileSync(script, readFileSync(join(dirname(fileURLToPath(import.meta.url)), "package-windows.mjs")));
  writeFileSync(join(dir, "package.json"), JSON.stringify({ packageManager: `pnpm@${PIN}` }));
  const binDir = join(dir, "node_modules", ".bin");
  mkdirSync(binDir, { recursive: true });
  const hostDir = join(dir, "host-pnpm-12");
  mkdirSync(hostDir);
  writePnpmShim(hostDir, "HOST-PNPM-12");
  if (local) {
    const packageDir = join(dir, "node_modules", "pnpm");
    mkdirSync(join(packageDir, "bin"), { recursive: true });
    writeFileSync(join(packageDir, "package.json"), JSON.stringify({ name: "pnpm", version, bin: { pnpm: "bin/pnpm.cjs" } }));
    writeFileSync(join(packageDir, "bin", "pnpm.cjs"), `
      const fs = require('node:fs');
      const lookupCommand = process.platform === 'win32'
        ? [process.env.ComSpec, ['/d', '/s', '/c', 'pnpm --version']]
        : ['pnpm', ['--version']];
      const lookup = process.env.TEST_LOOKUP_PNPM
        ? require('node:child_process').spawnSync(...lookupCommand, { encoding: 'utf8' }) : null;
      fs.writeFileSync(process.env.TEST_OUTPUT, JSON.stringify({
        args: process.argv.slice(2), path: process.env.PATH, cwd: process.cwd(),
        pathKeys: Object.keys(process.env).filter(key => key.toLowerCase() === 'path'),
        marker: process.env.TEST_MARKER,
        lookupStatus: lookup?.status, lookupOutput: lookup?.stdout?.trim(),
      }));
      process.exitCode = Number(process.env.TEST_CHILD_EXIT || 0);
    `);
    writePnpmShim(binDir, `LOCAL-PNPM-${PIN}`);
  }
  return { dir, script, binDir, hostDir, output: join(dir, "result.json") };
}

test("uses the validated local CLI and places its bin directory ahead of a hostile host PATH", () => {
  const f = fixture();
  const args = ["--win", "--dir", "--config", "config with spaces.json", "-c.extraMetadata.note=a&b;$(literal)", 'a"b'];
  const env = { ...process.env, Path: "host-pnpm-12", PATH: undefined, TEST_OUTPUT: f.output, TEST_MARKER: "preserved" };
  const command = packagingCommand(f.dir, args, env);
  assert.equal(command.command, process.execPath);
  assert.equal(command.options.shell, false);
  const child = spawnSync(command.command, command.args, { ...command.options, stdio: "pipe", encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  const result = JSON.parse(readFileSync(f.output, "utf8"));
  assert.deepEqual(result.args, ["exec", "electron-builder", ...args]);
  assert.equal(result.path, f.binDir + delimiter + "host-pnpm-12");
  assert.deepEqual(result.pathKeys, ["PATH"]);
  assert.equal(result.cwd, f.dir);
  assert.equal(result.marker, "preserved");
});

test("a spawned packaging child resolves the local pnpm shim instead of the host shim", () => {
  const f = fixture();
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toLowerCase() !== "path"));
  env.PATH = f.hostDir;
  env.TEST_OUTPUT = f.output;
  env.TEST_LOOKUP_PNPM = "1";
  // Prove the inherited environment actually selects a different executable first.
  // Windows .cmd files require cmd.exe; this fixed lookup receives no caller arguments.
  const lookupCommand = process.platform === "win32"
    ? [process.env.ComSpec, ["/d", "/s", "/c", "pnpm --version"]]
    : ["pnpm", ["--version"]];
  const host = spawnSync(...lookupCommand, { env, encoding: "utf8" });
  assert.equal(host.status, 0, host.stderr);
  assert.equal(host.stdout.trim(), "HOST-PNPM-12");
  const child = spawnSync(process.execPath, [f.script, "--win"], { env, encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  const result = JSON.parse(readFileSync(f.output, "utf8"));
  assert.equal(result.lookupStatus, 0);
  assert.equal(result.lookupOutput, `LOCAL-PNPM-${PIN}`);
});

test("fails closed for missing or mismatched local pnpm", () => {
  for (const options of [{ local: false }, { version: "12.0.0" }]) {
    const f = fixture(options);
    const result = spawnSync(process.execPath, [f.script, "--win"], {
      env: { ...process.env, TEST_OUTPUT: f.output }, encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.ok(result.stderr.includes(`local pnpm@${PIN}`), result.stderr);
    assert.throws(() => readFileSync(f.output));
  }
});

test("rejects an unpinned manager and an incomplete local installation", () => {
  const f = fixture();
  writeFileSync(join(f.dir, "package.json"), JSON.stringify({ packageManager: "pnpm@latest" }));
  assert.throws(() => packagingCommand(f.dir, []), /exact pnpm/);
  writeFileSync(join(f.dir, "package.json"), JSON.stringify({ packageManager: `pnpm@${PIN}` }));
  rmSync(join(f.binDir, process.platform === "win32" ? "pnpm.cmd" : "pnpm"));
  assert.throws(() => packagingCommand(f.dir, []), /shim is missing/);
});

test("the executable launcher forwards arguments and preserves a failing child exit code", () => {
  const f = fixture();
  const args = ["--win", "--dir", "--publish", "never", "a b", "x&y"];
  const result = spawnSync(process.execPath, [f.script, ...args], {
    env: { ...process.env, TEST_OUTPUT: f.output, TEST_CHILD_EXIT: "23" }, encoding: "utf8",
  });
  assert.equal(result.status, 23, result.stderr);
  assert.deepEqual(JSON.parse(readFileSync(f.output, "utf8")).args, ["exec", "electron-builder", ...args]);
});
