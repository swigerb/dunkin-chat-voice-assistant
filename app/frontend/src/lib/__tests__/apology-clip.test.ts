import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { APOLOGY_CLIP_LANGUAGES, apologyClipUrl, playApologyClip } from "../apology-clip";

const localesDir = resolve(__dirname, "../../locales");
const appLanguages = readdirSync(localesDir, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && !entry.name.startsWith("__"))
    .map(entry => entry.name);

describe("apologyClipUrl", () => {
    it("picks the clip for the UI language and falls back to English", () => {
        expect(apologyClipUrl("en")).toBe("/audio/apology-en.wav");
        expect(apologyClipUrl("es")).toBe("/audio/apology-es.wav");
        expect(apologyClipUrl("fr-CA")).toBe("/audio/apology-fr.wav");
        expect(apologyClipUrl("JA")).toBe("/audio/apology-ja.wav");
        expect(apologyClipUrl("de")).toBe("/audio/apology-en.wav");
        expect(apologyClipUrl(undefined)).toBe("/audio/apology-en.wav");
    });

    it("has a clip for every language the app has a translation for", () => {
        expect(appLanguages.length).toBeGreaterThanOrEqual(4);
        expect([...APOLOGY_CLIP_LANGUAGES].sort()).toEqual(appLanguages.sort());
    });

    it.each(APOLOGY_CLIP_LANGUAGES)("ships a short 24 kHz mono PCM16 wav for %s", lng => {
        const wav = readFileSync(resolve(__dirname, `../../../public/audio/apology-${lng}.wav`));
        expect(wav.toString("ascii", 0, 4)).toBe("RIFF");
        expect(wav.toString("ascii", 8, 12)).toBe("WAVE");
        expect(wav.readUInt16LE(20)).toBe(1); // PCM
        expect(wav.readUInt16LE(22)).toBe(1); // mono
        expect(wav.readUInt32LE(24)).toBe(24000);
        expect(wav.readUInt16LE(34)).toBe(16);
        const seconds = wav.readUInt32LE(40) / (24000 * 2);
        expect(seconds).toBeGreaterThan(0.5);
        expect(seconds).toBeLessThan(6);
    });
});

describe("playApologyClip", () => {
    class StubAudio {
        static last: StubAudio;
        listeners: Record<string, () => void> = {};
        play = vi.fn(() => Promise.resolve());
        pause = vi.fn();
        constructor(public src: string) {
            StubAudio.last = this;
        }
        addEventListener(type: string, fn: () => void) {
            this.listeners[type] = fn;
        }
    }

    beforeEach(() => vi.stubGlobal("Audio", StubAudio));
    afterEach(() => vi.unstubAllGlobals());

    it("resolves when the clip ends", async () => {
        const clip = playApologyClip("fr");
        expect(StubAudio.last.src).toBe("/audio/apology-fr.wav");
        StubAudio.last.listeners.ended();
        await expect(clip.done).resolves.toBeUndefined();
    });

    it("resolves when the browser blocks playback, so the mic is never left muted", async () => {
        class BlockedAudio extends StubAudio {
            play = vi.fn(() => Promise.reject(new Error("NotAllowedError")));
        }
        vi.stubGlobal("Audio", BlockedAudio);
        await expect(playApologyClip("en").done).resolves.toBeUndefined();
    });

    it("resolves when the clip fails to load", async () => {
        const clip = playApologyClip("ja");
        StubAudio.last.listeners.error();
        await expect(clip.done).resolves.toBeUndefined();
    });

    it("stop() pauses and resolves", async () => {
        const clip = playApologyClip("es");
        clip.stop();
        expect(StubAudio.last.pause).toHaveBeenCalled();
        await expect(clip.done).resolves.toBeUndefined();
    });
});
