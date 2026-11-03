// Project identity shown in the UI. Lives in shared/ because BOTH the renderer (the Settings footer
// link) and the main process (the open-external allowlist) need the SAME URL — the renderer cannot
// import main-process code, and two copies drift (they did: the footer hardcoded the org URL).

/** Who maintains this build — the fork owner. Shown in the Settings footer ("Maintained by …"). */
export const MAINTAINER_NAME = "Unnatural-Dev";

/** The maintainer's GitHub profile (Settings footer "Maintained by" link). */
export const MAINTAINER_URL = "https://github.com/Unnatural-Dev";
