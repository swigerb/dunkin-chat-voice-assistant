import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";

const rt = vi.hoisted(() => ({
    options: null as any,
    addUserAudio: vi.fn()
}));
const mic = vi.hoisted(() => ({
    onAudioRecorded: null as null | ((base64: string) => void),
    start: vi.fn(async () => {}),
    stop: vi.fn(async () => {})
}));
const lang = vi.hoisted(() => ({ current: "en" }));

vi.mock("react-i18next", () => ({
    useTranslation: () => ({
        t: (key: string) => key,
        i18n: { language: lang.current, resolvedLanguage: lang.current, changeLanguage: () => Promise.resolve() }
    }),
    initReactI18next: { type: "3rdParty", init: () => undefined }
}));
vi.mock("@/hooks/useRealtime", () => ({
    default: (options: any) => {
        rt.options = options;
        return {
            startSession: vi.fn(),
            sendVoiceChoice: vi.fn(),
            inputAudioBufferClear: vi.fn(),
            addUserAudio: rt.addUserAudio,
            isConnected: true,
            reconnect: vi.fn()
        };
    }
}));
vi.mock("@/hooks/useAudioRecorder", () => ({
    default: ({ onAudioRecorded }: { onAudioRecorded: (base64: string) => void }) => {
        mic.onAudioRecorded = onAudioRecorded;
        return { start: mic.start, stop: mic.stop };
    }
}));
vi.mock("@/hooks/useAudioPlayer", () => ({
    default: () => ({ reset: vi.fn(async () => {}), play: vi.fn(), stop: vi.fn(), waitForDrain: vi.fn(async () => {}) })
}));
vi.mock("darkreader", () => ({ enable: vi.fn(), disable: vi.fn(), auto: vi.fn(), setFetchMethod: vi.fn() }));
vi.mock("@/hooks/useAzureSpeech", () => ({
    default: () => ({ startSession: vi.fn(), addUserAudio: vi.fn(), inputAudioBufferClear: vi.fn() })
}));
Element.prototype.scrollIntoView = vi.fn();

class FakeAudio {
    static instances: FakeAudio[] = [];
    listeners: Record<string, Array<() => void>> = {};
    play = vi.fn(() => Promise.resolve());
    pause = vi.fn();
    constructor(public src: string) {
        FakeAudio.instances.push(this);
    }
    addEventListener(type: string, fn: () => void) {
        (this.listeners[type] ??= []).push(fn);
    }
    emit(type: string) {
        (this.listeners[type] ?? []).forEach(fn => fn());
    }
}

const micButton = () => screen.getByRole("button", { name: /app\.(start|stop)Recording/ });

async function startConversation() {
    render(<App />);
    await act(async () => {
        fireEvent.click(micButton());
    });
}

async function rateLimited(message: object) {
    await act(async () => {
        rt.options.onReceivedRateLimited({ type: "extension.rate_limited", ...message });
    });
}

async function guestSpeaks() {
    await act(async () => {
        mic.onAudioRecorded!("AAAA");
    });
}

beforeEach(() => {
    localStorage.clear();
    lang.current = "en";
    FakeAudio.instances = [];
    rt.addUserAudio.mockClear();
    vi.stubGlobal("Audio", FakeAudio);
});

afterEach(() => {
    vi.unstubAllGlobals();
});

describe("App rate-limit recovery", () => {
    it("plays the apology clip for the selected language and mutes the mic while it plays", async () => {
        lang.current = "es";
        await startConversation();
        await guestSpeaks();
        expect(rt.addUserAudio).toHaveBeenCalledTimes(1);

        await rateLimited({ attempt: 1 });

        expect(FakeAudio.instances).toHaveLength(1);
        expect(FakeAudio.instances[0].src).toBe("/audio/apology-es.wav");
        expect(FakeAudio.instances[0].play).toHaveBeenCalledTimes(1);
        expect(screen.getByRole("status")).toHaveTextContent("status.rateLimitRetrying");

        await guestSpeaks();
        expect(rt.addUserAudio).toHaveBeenCalledTimes(1);

        await act(async () => {
            FakeAudio.instances[0].emit("ended");
        });
        await guestSpeaks();
        expect(rt.addUserAudio).toHaveBeenCalledTimes(2);
    });

    it("falls back to the English clip for a language without one", async () => {
        lang.current = "de";
        await startConversation();
        await rateLimited({ attempt: 1 });
        expect(FakeAudio.instances[0].src).toBe("/audio/apology-en.wav");
    });

    it("clears the notice once the retried answer starts playing", async () => {
        await startConversation();
        await rateLimited({ attempt: 1 });
        expect(screen.getByRole("status")).toHaveTextContent("status.rateLimitRetrying");

        await act(async () => {
            rt.options.onReceivedResponseAudioDelta({ type: "response.audio.delta", delta: "AAAA" });
        });
        expect(screen.queryByRole("status")).toBeNull();
    });

    it("shows the final notice, stops the clip and unmutes the mic when the retries are exhausted", async () => {
        await startConversation();
        await rateLimited({ attempt: 1 });
        await rateLimited({ attempt: 2, final: true });

        expect(FakeAudio.instances).toHaveLength(1);
        expect(FakeAudio.instances[0].pause).toHaveBeenCalled();
        expect(screen.getByRole("status")).toHaveTextContent("status.rateLimitFinal");
        await guestSpeaks();
        expect(rt.addUserAudio).toHaveBeenCalledTimes(1);

        // The guest's next turn clears it.
        await act(async () => {
            rt.options.onReceivedInputAudioBufferSpeechStarted({ type: "input_audio_buffer.speech_started" });
        });
        expect(screen.queryByRole("status")).toBeNull();
    });

    it("ignores the event when no conversation is running", async () => {
        render(<App />);
        await rateLimited({ attempt: 1 });
        expect(FakeAudio.instances).toHaveLength(0);
        expect(screen.queryByRole("status")).toBeNull();
    });

    it("never plays two clips on top of each other", async () => {
        await startConversation();
        await rateLimited({ attempt: 1 });
        await rateLimited({ attempt: 1 });
        expect(FakeAudio.instances).toHaveLength(1);
    });

    it("stopping the conversation stops the clip, clears the notice and unmutes the mic", async () => {
        await startConversation();
        await rateLimited({ attempt: 1 });
        await act(async () => {
            fireEvent.click(micButton());
        });
        expect(FakeAudio.instances[0].pause).toHaveBeenCalled();

        await act(async () => {
            fireEvent.click(micButton());
        });
        expect(screen.queryByRole("status")).toBeNull();
        await guestSpeaks();
        expect(rt.addUserAudio).toHaveBeenCalledTimes(1);
    });
});
