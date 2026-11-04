export const HISTORY_RECONCILE_MS = 30_000;

/** null requests full reconciliation; undefined ignores temporary/non-JSON entries. */
export function historyJsonName(filename: string | Buffer | null): string | null | undefined {
  if (filename === null) return null;
  const name = filename.toString();
  if (!name || name.includes("/") || name.includes("\\") || name === "." || name === "..") return null;
  return name.endsWith(".json") && !name.endsWith(".tmp.json") ? name : undefined;
}
