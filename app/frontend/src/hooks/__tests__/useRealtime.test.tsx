import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import useRealTime, { classifyClose, RESUME_STORAGE_KEY, WS_CLOSE_IDLE_TIMEOUT, WS_CLOSE_SUPERSEDED } from "../useRealtime";

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
const sent = () => ws.send.mock.calls.map(([msg]) => msg);
const sentTypes = () => sent().map(msg => msg.type);
const serverSays = (message: object) => act(() => last().options.onMessage({ data: JSON.stringify(message) } as MessageEvent));
const storedId = () => sessionStorage.getItem(RESUME_STORAGE_KEY);
const open = () => act(() => last().options.onOpen(new Event("open")));

beforeEach(() => {
    ws.calls = [];
    ws.readyState = 1;
    ws.send.mockReset();
    sessionStorage.clear();
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

        expect(onConnectionLost).toHaveBeenCalledWith({
            code: 1002,
            reason: "Received frame with non-zero reserved bits",
            idle: false,
            kind: "transport",
            resuming: false
        });
        expect(onWebSocketClose).toHaveBeenCalledTimes(1);
        expect(last().connect).toBe(true);
    });

    it("parks the socket after an idle close (4000) and re-opens only when asked", () => {
        const onConnectionLost = vi.fn();
        const { result } = renderHook(() => useRealTime({ onConnectionLost }));

        expect(last().options.shouldReconnect(closeEvent(4000, "idle_timeout"))).toBe(false);
        expect(last().options.shouldReconnect(closeEvent(4001))).toBe(true);

        act(() => last().options.onClose(closeEvent(4000, "idle_timeout")));
        expect(onConnectionLost).toHaveBeenCalledWith({ code: 4000, reason: "idle_timeout", idle: true, kind: "idle", resuming: false });
        expect(last().connect).toBe(false);

        act(() => result.current.reconnect());
        expect(last().connect).toBe(true);
    });

    it("stops connecting once retries are exhausted and re-opens only when asked", () => {
        const onReconnectGaveUp = vi.fn();
        const { result } = renderHook(() => useRealTime({ onReconnectGaveUp }));

        act(() => last().options.onReconnectStop(10));
        expect(last().connect).toBe(false);
        expect(onReconnectGaveUp).toHaveBeenCalledTimes(1);

        act(() => result.current.reconnect());
        expect(last().connect).toBe(true);
    });

    it("backs off 1s, 2s, 4s... capped at 30s, for long enough to outlast the server's hold", () => {
        renderHook(() => useRealTime({}));
        const { reconnectInterval, reconnectAttempts } = last().options;
        const random = vi.spyOn(Math, "random").mockReturnValue(0);
        try {
            expect([0, 1, 2, 3, 4, 5, 9].map(n => reconnectInterval(n))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
        } finally {
            random.mockRestore();
        }
        const total = Array.from({ length: reconnectAttempts }, (_, n) => Math.min(1000 * 2 ** n, 30000)).reduce((a, b) => a + b, 0);
        expect(total).toBeGreaterThan(120_000);
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
        expect(ws.send).not.toHaveBeenCalled();

        open();
        // The voice and session.update survive into the next socket; audio and clears don't.
        expect(sentTypes()).toEqual(["extension.set_voice", "session.update"]);
    });
});

describe("useRealTime middle-tier extensions", () => {
    it("hands extension.rate_limited to its callback", () => {
        const onReceivedRateLimited = vi.fn();
        const onReceivedError = vi.fn();
        renderHook(() => useRealTime({ onReceivedRateLimited, onReceivedError }));

        const payload = { type: "extension.rate_limited", attempt: 2, final: true };
        serverSays(payload);

        expect(onReceivedRateLimited).toHaveBeenCalledWith(payload);
        expect(onReceivedError).not.toHaveBeenCalled();
    });
});

describe("useRealTime order resume", () => {
    it("classifies closes: 4000 idle, 4002 superseded, 1000 session_ended, anything else transport", () => {
        expect(classifyClose(closeEvent(4000, "idle_timeout"))).toBe("idle");
        expect(classifyClose(closeEvent(4002, "superseded"))).toBe("superseded");
        expect(classifyClose(closeEvent(1000, "session_ended"))).toBe("ended");
        expect(classifyClose(closeEvent(1000, ""))).toBe("transport");
        expect(classifyClose(closeEvent(1001, "session_ended"))).toBe("transport");
    });

    it("sends extension.resume as the literal first frame, ahead of anything queued", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        result.current.sendVoiceChoice("marin");
        result.current.startSession();
        expect(ws.send).not.toHaveBeenCalled();

        open();

        expect(sent()[0]).toEqual({ type: "extension.resume", resume_id: "RID-1" });
        expect(sentTypes()).toEqual(["extension.resume", "extension.set_voice", "session.update"]);
    });

    it("a tap after a drop is held for the next socket, behind its resume frame", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        open();
        act(() => last().options.onClose(closeEvent(1011)));
        ws.send.mockClear();

        result.current.sendVoiceChoice("marin");
        result.current.startSession();
        expect(ws.send).not.toHaveBeenCalled();

        open();
        expect(sentTypes()).toEqual(["extension.resume", "extension.set_voice", "session.update"]);
    });

    it("never hands a frame to react-use-websocket's own queue", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        result.current.startSession();
        open();
        result.current.sendVoiceChoice("marin");
        result.current.addUserAudio("AAAA");
        result.current.inputAudioBufferClear();

        expect(ws.send.mock.calls.length).toBe(5);
        expect(ws.send.mock.calls.every(([, keep]) => keep === false)).toBe(true);
    });

    it("sends no resume frame when the tab holds no id", () => {
        const { result } = renderHook(() => useRealTime({}));
        result.current.startSession();
        open();
        expect(sentTypes()).toEqual(["session.update"]);
    });

    it("never resumes on the direct Azure OpenAI path", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const onConnectionLost = vi.fn();
        renderHook(() => useRealTime({ useDirectAoaiApi: true, aoaiEndpointOverride: "wss://x", onConnectionLost }));
        open();
        expect(sentTypes()).not.toContain("extension.resume");
        serverSays({ type: "extension.session_metadata", sessionToken: "S", roundTripIndex: 0, roundTripToken: "T", resumeId: "RID-2" });
        expect(storedId()).toBe("RID-1");
        act(() => last().options.onClose(closeEvent(1006)));
        expect(onConnectionLost).toHaveBeenCalledWith(expect.objectContaining({ resuming: false }));
    });

    it("stores resumeId from session_metadata and presents it on the next open", () => {
        const onReceivedSessionMetadata = vi.fn();
        renderHook(() => useRealTime({ onReceivedSessionMetadata }));
        open();
        serverSays({ type: "extension.session_metadata", sessionToken: "S", roundTripIndex: 0, roundTripToken: "T", resumeId: "RID-A" });
        expect(storedId()).toBe("RID-A");
        expect(onReceivedSessionMetadata).toHaveBeenCalledTimes(1);

        act(() => last().options.onClose(closeEvent(1011)));
        ws.send.mockReset();
        open();
        expect(sent()[0]).toEqual({ type: "extension.resume", resume_id: "RID-A" });
    });

    it("session_resumed stores the rotated id and hands the order to the app", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-OLD");
        const onReceivedSessionResumed = vi.fn();
        renderHook(() => useRealTime({ onReceivedSessionResumed }));
        open();
        const resumed = {
            type: "extension.session_resumed",
            order_summary: { items: [{ item: "Latte", size: "Medium", quantity: 1, price: 3.49, display: "Medium Latte" }], total: 3.49, tax: 0.28, finalTotal: 3.77 },
            session_token: "S",
            round_trip_index: 2,
            round_trip_token: "T",
            resume_id: "RID-NEW"
        };
        serverSays(resumed);
        expect(storedId()).toBe("RID-NEW");
        expect(onReceivedSessionResumed).toHaveBeenCalledWith(resumed);
    });

    it("resume_rejected clears the stored id; the following metadata stores the new one", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-OLD");
        const onReceivedResumeRejected = vi.fn();
        renderHook(() => useRealTime({ onReceivedResumeRejected }));
        open();
        serverSays({ type: "extension.resume_rejected", reason: "expired" });
        expect(storedId()).toBeNull();
        expect(onReceivedResumeRejected).toHaveBeenCalledWith({ type: "extension.resume_rejected", reason: "expired" });

        serverSays({ type: "extension.session_metadata", sessionToken: "S2", roundTripIndex: 0, roundTripToken: "T", resumeId: "RID-FRESH" });
        expect(storedId()).toBe("RID-FRESH");
    });

    it.each([1001, 1002, 1006, 1011])("transport close %i reconnects and keeps the id to resume", code => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const onConnectionLost = vi.fn();
        renderHook(() => useRealTime({ onConnectionLost }));
        open();
        expect(last().options.shouldReconnect(closeEvent(code))).toBe(true);
        act(() => last().options.onClose(closeEvent(code)));
        expect(last().connect).toBe(true);
        expect(storedId()).toBe("RID-1");
        expect(onConnectionLost).toHaveBeenCalledWith(expect.objectContaining({ kind: "transport", resuming: true }));
    });

    it.each([
        ["idle 4000", WS_CLOSE_IDLE_TIMEOUT, "idle_timeout", "idle", null],
        ["superseded 4002", WS_CLOSE_SUPERSEDED, "superseded", "superseded", "RID-1"]
    ])("%s: no reconnect; id afterwards = %s", (_label, code, reason, kind, idAfter) => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const onConnectionLost = vi.fn();
        renderHook(() => useRealTime({ onConnectionLost }));
        open();
        expect(last().options.shouldReconnect(closeEvent(code as number, reason as string))).toBe(false);
        act(() => last().options.onClose(closeEvent(code as number, reason as string)));
        expect(last().connect).toBe(false);
        expect(storedId()).toBe(idAfter);
        expect(onConnectionLost).toHaveBeenCalledWith(expect.objectContaining({ kind, resuming: false }));
    });

    it("frames queued for a session that then ends (idle) never reach the next one", () => {
        const { result } = renderHook(() => useRealTime({}));
        open();
        act(() => last().options.onClose(closeEvent(1006)));
        result.current.sendVoiceChoice("marin"); // queued while reconnecting
        act(() => last().options.onClose(closeEvent(WS_CLOSE_IDLE_TIMEOUT, "idle_timeout")));
        act(() => result.current.reconnect());
        ws.send.mockReset();
        open();
        expect(ws.send).not.toHaveBeenCalled();
    });

    it("session_ended 1000: no background reconnect, id cleared, then a fresh socket opens by itself", async () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const onConnectionLost = vi.fn();
        renderHook(() => useRealTime({ onConnectionLost }));
        open();
        expect(last().options.shouldReconnect(closeEvent(1000, "session_ended"))).toBe(false);
        act(() => last().options.onClose(closeEvent(1000, "session_ended")));
        expect(storedId()).toBeNull();
        expect(onConnectionLost).toHaveBeenCalledWith(expect.objectContaining({ kind: "ended", resuming: false }));
        // The socket is dropped and re-opened (connect false -> true): a new server session.
        expect(last().connect).toBe(false);
        await waitFor(() => expect(last().connect).toBe(true));
    });

    it("a tap right after endSession is held for the fresh session, never sent to the ending one", async () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        open();
        ws.send.mockReset();
        result.current.endSession();
        result.current.sendVoiceChoice("marin");
        result.current.startSession();
        result.current.addUserAudio("AAAA");
        expect(sentTypes()).toEqual(["extension.end_session"]);

        act(() => last().options.onClose(closeEvent(1000, "session_ended")));
        await waitFor(() => expect(last().connect).toBe(true));
        ws.send.mockReset();
        open();
        expect(sentTypes()).toEqual(["extension.set_voice", "session.update"]);
    });

    it("a server-initiated session_ended close drops frames queued for the old session", async () => {
        const { result } = renderHook(() => useRealTime({}));
        open();
        act(() => last().options.onClose(closeEvent(1006)));
        result.current.sendVoiceChoice("marin");
        act(() => last().options.onClose(closeEvent(1000, "session_ended")));
        await waitFor(() => expect(last().connect).toBe(true));
        ws.send.mockReset();
        open();
        expect(ws.send).not.toHaveBeenCalled();
    });

    it("endSession on a socket that is not open sends nothing but still forgets the id", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        result.current.endSession();
        expect(ws.send).not.toHaveBeenCalled();
        expect(storedId()).toBeNull();
    });

    it("endSession while reconnecting drops what was queued for the old session", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        open();
        act(() => last().options.onClose(closeEvent(1006)));
        result.current.sendVoiceChoice("marin");
        result.current.endSession();
        ws.send.mockReset();
        open();
        expect(ws.send).not.toHaveBeenCalled();
    });

    it("endSession sends extension.end_session and forgets the id", () => {
        sessionStorage.setItem(RESUME_STORAGE_KEY, "RID-1");
        const { result } = renderHook(() => useRealTime({}));
        open();
        ws.send.mockReset();
        result.current.endSession();
        expect(sent()).toEqual([{ type: "extension.end_session" }]);
        expect(storedId()).toBeNull();

        act(() => last().options.onClose(closeEvent(1000, "session_ended")));
        ws.send.mockReset();
        open();
        expect(sentTypes()).not.toContain("extension.resume");
    });
});
