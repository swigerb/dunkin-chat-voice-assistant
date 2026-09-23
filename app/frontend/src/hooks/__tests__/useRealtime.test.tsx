import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import useRealTime from "../useRealtime";

const ws = vi.hoisted(() => ({
    calls: [] as Array<{ url: string | null; options: any; connect: boolean | undefined }>,
    readyState: 1,
    send: vi.fn()
}));

vi.mock("react-use-websocket", () => ({
    default: (url: string | null, options: any, connect?: boolean) => {
        ws.calls.push({ url, options, connect });
        return { sendJsonMessage: ws.send, readyState: ws.readyState };
    },
    ReadyState: { UNINSTANTIATED: -1, CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 }
}));

const last = () => ws.calls[ws.calls.length - 1];
const closeEvent = (code: number, reason = "") => ({ code, reason, wasClean: false }) as CloseEvent;

beforeEach(() => {
    ws.calls = [];
    ws.readyState = 1;
    ws.send.mockReset();
});

describe("useRealTime connection lifecycle", () => {
    it("reports every close as a lost connection and keeps reconnecting in the background", () => {
        const onConnectionLost = vi.fn();
        const onWebSocketClose = vi.fn();
        renderHook(() => useRealTime({ enableInputAudioTranscription: true, onConnectionLost, onWebSocketClose }));

        expect(last().url).toBe("/realtime");
        expect(last().connect).toBe(true);
        expect(last().options.shouldReconnect(closeEvent(1006))).toBe(true);
        expect(last().options.shouldReconnect(closeEvent(1002))).toBe(true);

        act(() => last().options.onClose(closeEvent(1002, "Received frame with non-zero reserved bits")));

        expect(onConnectionLost).toHaveBeenCalledWith({ code: 1002, reason: "Received frame with non-zero reserved bits", idle: false });
        expect(onWebSocketClose).toHaveBeenCalledTimes(1);
        expect(last().connect).toBe(true);
    });

    it("parks the socket after an idle close (4000) and re-opens only when asked", () => {
        const onConnectionLost = vi.fn();
        const { result } = renderHook(() => useRealTime({ onConnectionLost }));

        expect(last().options.shouldReconnect(closeEvent(4000, "idle_timeout"))).toBe(false);
        expect(last().options.shouldReconnect(closeEvent(4001))).toBe(true);

        act(() => last().options.onClose(closeEvent(4000, "idle_timeout")));
        expect(onConnectionLost).toHaveBeenCalledWith({ code: 4000, reason: "idle_timeout", idle: true });
        expect(last().connect).toBe(false);

        act(() => result.current.reconnect());
        expect(last().connect).toBe(true);
    });

    it("stops connecting once retries are exhausted and re-opens only when asked", () => {
        const { result } = renderHook(() => useRealTime({}));

        act(() => last().options.onReconnectStop(20));
        expect(last().connect).toBe(false);

        act(() => result.current.reconnect());
        expect(last().connect).toBe(true);
    });

    it("exposes whether the socket is open", () => {
        ws.readyState = 3;
        const { result, rerender } = renderHook(() => useRealTime({}));
        expect(result.current.isConnected).toBe(false);

        ws.readyState = 0;
        rerender();
        expect(result.current.isConnected).toBe(false);

        ws.readyState = 1;
        rerender();
        expect(result.current.isConnected).toBe(true);
    });

    it("never queues mic audio or buffer clears for a future socket", () => {
        const { result } = renderHook(() => useRealTime({ enableInputAudioTranscription: true }));
        result.current.addUserAudio("AAAA");
        result.current.inputAudioBufferClear();
        result.current.sendVoiceChoice("marin");
        result.current.startSession();

        const byType = (t: string) => ws.send.mock.calls.find(([msg]) => msg.type === t)!;
        expect(byType("input_audio_buffer.append")[1]).toBe(false);
        expect(byType("input_audio_buffer.clear")[1]).toBe(false);
        // The voice and session.update must survive into the next socket.
        expect(byType("extension.set_voice")[1]).not.toBe(false);
        expect(byType("session.update")[1]).not.toBe(false);
    });
});

describe("useRealTime middle-tier extensions", () => {
    it("hands extension.rate_limited to its callback", () => {
        const onReceivedRateLimited = vi.fn();
        const onReceivedError = vi.fn();
        renderHook(() => useRealTime({ onReceivedRateLimited, onReceivedError }));

        const payload = { type: "extension.rate_limited", attempt: 2, final: true };
        act(() => last().options.onMessage({ data: JSON.stringify(payload) } as MessageEvent));

        expect(onReceivedRateLimited).toHaveBeenCalledWith(payload);
        expect(onReceivedError).not.toHaveBeenCalled();
    });
});