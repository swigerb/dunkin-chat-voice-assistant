import { describe, expect, it } from "vitest";

import en from "../en/translation.json";
import es from "../es/translation.json";
import fr from "../fr/translation.json";
import ja from "../ja/translation.json";

describe("connection-lost notice", () => {
    it.each([
        ["en", en],
        ["es", es],
        ["fr", fr],
        ["ja", ja]
    ])("is translated in %s", (_locale, strings: any) => {
        expect(typeof strings.status.connectionLost).toBe("string");
        expect(strings.status.connectionLost.length).toBeGreaterThan(0);
    });
});
