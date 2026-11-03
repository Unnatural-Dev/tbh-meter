// --------------------------------------------------------------------------- //
// Static config. The meter is a local-only desktop app: it talks to no backend
// (runs live on disk; the only network traffic is the GitHub auto-update feed).
// --------------------------------------------------------------------------- //

/** Community Discord invite (footer/settings "Bugs & feedback" button). */
export const DISCORD_INVITE_URL = "https://discord.gg/eYqUkxu3";

/** The maintainer's GitHub profile (Settings footer "Maintained by" link) — single source in shared/. */
export { MAINTAINER_URL } from "../shared/project-info.js";
