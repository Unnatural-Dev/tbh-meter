/** Shared log-ID stem for writing and deleting structured logs. Not unique: callers deleting
 *  files must still confirm the embedded run ID because different IDs can share a stem. */
export function safeLogId(id: string): string {
  return id.replace(/[^A-Za-z0-9._-]/g, "-");
}
