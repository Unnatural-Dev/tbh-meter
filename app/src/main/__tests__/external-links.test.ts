import { describe, expect, it } from "vitest";
import { isAllowedExternalUrl } from "../external-links.js";
import { DISCORD_INVITE_URL, MAINTAINER_URL } from "../config.js";

describe("isAllowedExternalUrl", () => {
  it("allows the Settings footer maintainer link and the Discord invite", () => {
    expect(MAINTAINER_URL).toBe("https://github.com/Unnatural-Dev");
    expect(isAllowedExternalUrl(MAINTAINER_URL)).toBe(true);
    expect(isAllowedExternalUrl(DISCORD_INVITE_URL)).toBe(true);
  });

  it("rejects the old org link, look-alikes and non-strings", () => {
    for (const url of [
      "https://github.com/mad-labs-org",
      `${MAINTAINER_URL}/../evil`,
      `${MAINTAINER_URL}.evil.com`,
      "https://evil.com",
      "",
      null,
      undefined,
      42,
      { toString: () => MAINTAINER_URL },
    ]) {
      expect(isAllowedExternalUrl(url)).toBe(false);
    }
  });
});
