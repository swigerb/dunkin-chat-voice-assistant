import { describe, expect, it } from "vitest";

import en from "../en/translation.json";
import es from "../es/translation.json";
import fr from "../fr/translation.json";
import ja from "../ja/translation.json";

const LOCALES: [string, any][] = [
    ["en", en],
    ["es", es],
    ["fr", fr],
    ["ja", ja]
];

// Wording left over from the upstream VoiceRAG template (Contoso employee benefits).
const TEMPLATE_LEFTOVERS = [/contoso/i, /mercer/i, /employee benefits/i, /talk to your data/i, /Habla con tus datos/i, /Parlez à vos données/i, /データと話す/];

function flatten(value: any, prefix = ""): [string, string][] {
    if (typeof value === "string") return [[prefix, value]];
    return Object.entries(value ?? {}).flatMap(([key, child]) => flatten(child, prefix ? `${prefix}.${key}` : key));
}

describe("connection-lost notice", () => {
    it.each(LOCALES)("is translated in %s", (_locale, strings: any) => {
        expect(typeof strings.status.connectionLost).toBe("string");
        expect(strings.status.connectionLost.length).toBeGreaterThan(0);
    });
});

describe("rate-limit notices", () => {
    it.each(LOCALES.filter(([locale]) => locale !== "en"))("are translated in %s, not copied from English", (_locale, strings: any) => {
        for (const key of ["rateLimitRetrying", "rateLimitFinal"]) {
            expect(strings.status[key].length).toBeGreaterThan(0);
            expect(strings.status[key]).not.toBe((en as any).status[key]);
        }
    });
});

describe("locale strings", () => {
    it.each(LOCALES)("%s has no template leftovers", (_locale, strings) => {
        const offenders = flatten(strings).filter(([, text]) => TEMPLATE_LEFTOVERS.some(re => re.test(text)));
        expect(offenders).toEqual([]);
    });

    it.each(LOCALES)("%s has every key the English file has", (_locale, strings) => {
        const keys = new Set(flatten(strings).map(([key]) => key));
        const missing = flatten(en).map(([key]) => key).filter(key => !keys.has(key));
        expect(missing).toEqual([]);
    });

    it("the leftover guard is not vacuous", () => {
        expect(flatten({ a: { b: "Ask about Contoso benefits" } }).filter(([, t]) => TEMPLATE_LEFTOVERS.some(re => re.test(t)))).toHaveLength(1);
    });
});
