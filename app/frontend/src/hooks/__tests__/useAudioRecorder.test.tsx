import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import useAudioRecorder from "../useAudioRecorder";

const recorder = vi.hoisted(() => ({ start: vi.fn(async () => true), stop: vi.fn(async () => {}) }));

vi.mock("@/components/audio/recorder", () => ({
    Recorder: vi.fn(function (this: any) {
        this.start = recorder.start;
        this.stop = recorder.stop;
    })
}));

afterEach(() => {
    vi.unstubAllGlobals();
    recorder.start.mockReset();
});

describe("useAudioRecorder.start", () => {
    it.each([true, false])("reports whether capture started (%s)", async started => {
        vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn(async () => ({ getTracks: () => [] })) } });
        recorder.start.mockImplementation(async () => started);
        const { result } = renderHook(() => useAudioRecorder({ onAudioRecorded: () => {} }));
        await expect(result.current.start()).resolves.toBe(started);
    });

    it("lets a getUserMedia refusal propagate", async () => {
        vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn(async () => Promise.reject(new Error("NotAllowedError"))) } });
        const { result } = renderHook(() => useAudioRecorder({ onAudioRecorded: () => {} }));
        await expect(result.current.start()).rejects.toThrow("NotAllowedError");
    });
});
