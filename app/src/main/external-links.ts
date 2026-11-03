// The open-external allowlist — PURE (no Electron imports) so the security boundary is unit-tested.
// The renderer may ask main to open a URL; only these exact targets are honoured, so a compromised or
// buggy renderer can never make the app open an arbitrary link.
import { DISCORD_INVITE_URL, MAINTAINER_URL } from "./config.js";

const ALLOWED_EXTERNAL_URLS: ReadonlySet<string> = new Set([DISCORD_INVITE_URL, MAINTAINER_URL]);

/** True only for an exact allowlisted URL (no prefix/substring matching, non-strings rejected). */
export function isAllowedExternalUrl(url: unknown): url is string {
  return typeof url === "string" && ALLOWED_EXTERNAL_URLS.has(url);
}
