// Keep electron-builder's pnpm dependency collection on this project's pinned CLI,
// including when the shell that started packaging has another pnpm first on PATH.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export function packagingCommand(projectDir, args, env = process.env) {
  const project = JSON.parse(readFileSync(join(projectDir, "package.json"), "utf8"));
  const pin = /^pnpm@(\d+\.\d+\.\d+)$/.exec(project.packageManager ?? "");
  if (!pin) throw new Error("Packaging requires an exact pnpm@x.y.z packageManager pin.");

  const packageDir = join(projectDir, "node_modules", "pnpm");
  let local;
  try {
    local = JSON.parse(readFileSync(join(packageDir, "package.json"), "utf8"));
  } catch {
    throw new Error(`Install the project's local pnpm@${pin[1]} before packaging.`);
  }
  if (local.name !== "pnpm" || local.version !== pin[1]) {
    throw new Error(`Packaging requires local pnpm@${pin[1]}; found ${local.version ?? "an invalid package"}.`);
  }
  const bin = typeof local.bin === "string" ? local.bin : local.bin?.pnpm;
  if (typeof bin !== "string") throw new Error("The local pnpm package has no CLI entry point.");
  const cli = resolve(packageDir, bin);
  const binDir = join(projectDir, "node_modules", ".bin");
  const shim = join(binDir, process.platform === "win32" ? "pnpm.cmd" : "pnpm");
  if (!existsSync(cli) || !existsSync(shim)) {
    throw new Error("The local pnpm CLI or PATH shim is missing; reinstall dependencies before packaging.");
  }

  // Windows treats environment names case-insensitively. Emit exactly one PATH key so
  // child_process cannot select an older, differently-cased host Path instead.
  const inheritedPath = env.PATH ?? Object.entries(env).find(([key, value]) => key.toLowerCase() === "path" && value != null)?.[1] ?? "";
  const childEnv = Object.fromEntries(Object.entries(env).filter(([key]) => key.toLowerCase() !== "path"));
  childEnv.PATH = binDir + (inheritedPath ? delimiter + inheritedPath : "");
  return {
    command: process.execPath,
    args: [cli, "exec", "electron-builder", ...args],
    options: { cwd: projectDir, env: childEnv, stdio: "inherit", shell: false },
  };
}

function main() {
  try {
    const projectDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
    const command = packagingCommand(projectDir, process.argv.slice(2));
    const result = spawnSync(command.command, command.args, command.options);
    if (result.error) throw result.error;
    if (result.signal) process.stderr.write(`Packaging terminated by ${result.signal}.\n`);
    process.exitCode = result.status ?? 1;
  } catch (error) {
    process.stderr.write(`package-windows: ${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
