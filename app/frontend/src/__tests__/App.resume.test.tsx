import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";

const rt = vi.hoisted(() => ({
    params: null as any,
    api: {
        startSession: vi.fn(),
        addUserAudio: vi.fn(),
        inputAudioBufferClear: vi.fn(),
        sendVoiceChoice: vi.fn(),
        endSession: vi.fn(),
        reconnect: vi.fn(),
        isConnected: true
    }
}));
const rec = vi.hoisted(() => ({ start: vi.fn(async () => true), stop: vi.fn(async () => {}) }));

vi.mock("@/hooks/useRealtime", () => ({
    default: (params: any) => {
        rt.params = params;
        return rt.api;
    }
}));
vi.mock("@/hooks/useAudioRecorder", () => ({ default: () => rec }));
vi.mock("@/hooks/useAudioPlayer", () => ({
    default: () => ({ reset: vi.fn(async () => {}), play: vi.fn(), stop: vi.fn(), waitForDrain: vi.fn(async () => {}) })
}));
vi.mock("@/hooks/useAzureSpeech", () => ({
    default: () => ({ startSession: vi.fn(), addUserAudio: vi.fn(), inputAudioBufferClear: vi.fn() })
}));
vi.mock("darkreader", () => ({ enable: vi.fn(), disable: vi.fn(), auto: vi.fn(), setFetchMethod: vi.fn() }));

const LATTE = { item: "Latte", size: "Medium", quantity: 1, price: 3.49, display: "Resume Test Latte" };
const DONUT = { item: "Boston Kreme", size: "Standard", quantity: 1, price: 1.59, display: "Resume Test Donut" };
const orderOf = (...items: (typeof LATTE)[]) => {
    const total = items.reduce((s, i) => s + i.price * i.quantity, 0);
    return { items, total, tax: total * 0.08, finalTotal: total * 1.08 };
};
const resumedMsg = (order = orderOf(LATTE, DONUT)) => ({
    type: "extension.session_resumed" as const,
    order_summary: order,
    session_token: "SESSION-1",
    round_trip_index: 3,
    round_trip_token: "RT-3",
    resume_id: "RID-NEW"
});
const transportDrop = { code: 1011, reason: "", idle: false, kind: "transport", resuming: true };
const ended = { code: 1000, reason: "session_ended", idle: false, kind: "ended", resuming: false };
const shows = (text: string) => screen.queryAllByText(text).length > 0;

const micButton = () => screen.getByRole("button", { name: /app\.(start|stop)Recording/ });
const tapMic = async () => {
    await act(async () => {
        fireEvent.click(micButton());
    });
};

async function startConversationWithLatte() {
    render(<App />);
    await tapMic();
    act(() => rt.params.onReceivedExtensionMiddleTierToolResponse({ tool_name: "update_order", tool_result: JSON.stringify(orderOf(LATTE)), previous_item_id: "x" }));
    expect(shows("Resume Test Latte")).toBe(true);
}

beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    rec.start.mockImplementation(async () => true);
    rt.api.isConnected = true;
});

describe("order resume in the app", () => {
    it("a mid-conversation drop pauses the mic, keeps the ticket and says it is reconnecting", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        expect(rec.stop).toHaveBeenCalled();
        expect(screen.getByText("status.reconnecting")).toBeInTheDocument();
        expect(shows("Resume Test Latte")).toBe(true);
    });

    it("a drop with nothing in progress stays quiet while it resumes", async () => {
        render(<App />);
        await act(async () => rt.params.onConnectionLost(transportDrop));
        expect(screen.queryByText("status.reconnecting")).toBeNull();
        expect(screen.getByText("status.notRecordingMessage")).toBeInTheDocument();
    });

    it("session_resumed restores the ticket, re-sends voice + session.update and restarts the mic at once", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockClear();
        rt.api.startSession.mockClear();
        rt.api.sendVoiceChoice.mockClear();

        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));

        expect(shows("Resume Test Latte")).toBe(true);
        expect(shows("Resume Test Donut")).toBe(true);
        expect(rec.start).toHaveBeenCalledTimes(1);
        expect(rt.api.sendVoiceChoice).toHaveBeenCalledTimes(1);
        expect(rt.api.startSession).toHaveBeenCalledTimes(1);
        expect(rt.api.endSession).not.toHaveBeenCalled();
        expect(rt.api.reconnect).not.toHaveBeenCalled();
        expect(screen.getByText("status.resumed")).toBeInTheDocument();
        expect(micButton()).toHaveAccessibleName("app.stopRecording");
    });

    it("the resumed notice clears itself after a few seconds", async () => {
        vi.useFakeTimers();
        try {
            await startConversationWithLatte();
            await act(async () => rt.params.onConnectionLost(transportDrop));
            await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
            expect(screen.getByText("status.resumed")).toBeInTheDocument();
            await act(async () => {
                await vi.advanceTimersByTimeAsync(4100);
            });
            expect(screen.queryByText("status.resumed")).toBeNull();
            expect(screen.getByText("status.conversationInProgress")).toBeInTheDocument();
        } finally {
            vi.useRealTimers();
        }
    });

    it("shows the session identifiers from the resumed session", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        expect(screen.getAllByText(/SESSION-1/).length).toBeGreaterThan(0);
    });

    it("falls back to tap-to-continue when the browser won't restart the mic", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockImplementation(async () => false);

        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));

        expect(screen.getByText("status.resumedTapToContinue")).toBeInTheDocument();
        expect(micButton()).toHaveAccessibleName("app.startRecording");
        expect(shows("Resume Test Donut")).toBe(true);

        rec.start.mockImplementation(async () => true);
        rec.start.mockClear();
        await tapMic();
        // Resumed session: no greeting will come, so the mic starts without the greeting wait.
        expect(rec.start).toHaveBeenCalledTimes(1);
        expect(rt.api.reconnect).not.toHaveBeenCalled();
        expect(shows("Resume Test Donut")).toBe(true);
    });

    it("getUserMedia refusing after a resume also falls back to tap-to-continue", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockImplementation(async () => {
            throw new DOMException("denied", "NotAllowedError");
        });
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        expect(screen.getByText("status.resumedTapToContinue")).toBeInTheDocument();
    });

    it("a resume while the guest was not talking restores the ticket without touching the mic", async () => {
        await startConversationWithLatte();
        await tapMic(); // guest stops the conversation
        rec.start.mockClear();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        expect(rec.start).not.toHaveBeenCalled();
        expect(shows("Resume Test Donut")).toBe(true);
        expect(screen.getByText("status.resumedTapToContinue")).toBeInTheDocument();
    });

    it("a failed reconnect attempt keeps the first answer: the guest was mid-conversation", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await act(async () => rt.params.onConnectionLost({ ...transportDrop, code: 1006 }));
        rec.start.mockClear();
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        expect(rec.start).toHaveBeenCalledTimes(1);
    });

    it("an empty resumed order needs no tap-to-continue notice", async () => {
        render(<App />);
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg(orderOf())));
        expect(screen.queryByText("status.resumedTapToContinue")).toBeNull();
    });

    it("resume_rejected clears the ticket and asks for a fresh start", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockClear();
        await act(async () => rt.params.onReceivedResumeRejected({ type: "extension.resume_rejected", reason: "expired" }));
        expect(shows("Resume Test Latte")).toBe(false);
        expect(screen.getByText("status.resumeRejected")).toBeInTheDocument();
        expect(rec.start).not.toHaveBeenCalled();
    });

    it("resume_rejected after the guest already tapped lets that fresh conversation carry on", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await tapMic(); // guest taps while reconnecting
        await act(async () => rt.params.onReceivedResumeRejected({ type: "extension.resume_rejected", reason: "unknown" }));
        expect(screen.queryByText("status.resumeRejected")).toBeNull();
        expect(micButton()).toHaveAccessibleName("app.stopRecording");
    });

    it("a tap while reconnecting: the resume starts the mic at once and sends nothing twice", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await tapMic(); // guest taps while reconnecting: waits for a greeting that won't come
        rec.start.mockClear();
        rt.api.startSession.mockClear();
        rt.api.sendVoiceChoice.mockClear();

        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));

        expect(rec.start).toHaveBeenCalledTimes(1);
        expect(rt.api.startSession).not.toHaveBeenCalled();
        expect(rt.api.sendVoiceChoice).not.toHaveBeenCalled();
        expect(screen.getByText("status.resumed")).toBeInTheDocument();
        expect(shows("Resume Test Donut")).toBe(true);
    });

    it("resume_rejected after an early tap: the fresh order it starts is not wiped by the next tap", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await tapMic();
        await act(async () => rt.params.onReceivedResumeRejected({ type: "extension.resume_rejected", reason: "unknown" }));
        act(() => rt.params.onReceivedExtensionMiddleTierToolResponse({ tool_name: "update_order", tool_result: JSON.stringify(orderOf(DONUT)), previous_item_id: "y" }));
        await tapMic(); // stop
        await tapMic(); // start again
        expect(shows("Resume Test Donut")).toBe(true);
    });

    it("a continuing tap keeps the resumed session's identifiers", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockImplementation(async () => false);
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        rec.start.mockImplementation(async () => true);
        await tapMic();
        expect(screen.getAllByText(/SESSION-1/).length).toBeGreaterThan(0);
    });

    it("retries exhausted after a resume: the next tap is a fresh start that waits for the greeting", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockImplementation(async () => false);
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await act(async () => rt.params.onReconnectGaveUp());
        rec.start.mockImplementation(async () => true);
        rec.start.mockClear();
        rt.api.isConnected = false;
        await tapMic();
        expect(rec.start).not.toHaveBeenCalled();
        expect(screen.queryAllByText(/SESSION-1/).length).toBe(0);
    });

    it("idle close: no resume, ticket reset on the next tap", async () => {
        await startConversationWithLatte();
        rt.api.isConnected = false;
        await act(async () => rt.params.onConnectionLost({ code: 4000, reason: "idle_timeout", idle: true, kind: "idle", resuming: false }));
        expect(screen.getByText("status.sessionEndedIdle")).toBeInTheDocument();
        await tapMic();
        expect(rt.api.reconnect).toHaveBeenCalled();
        expect(shows("Resume Test Latte")).toBe(false);
    });

    it("superseded: tells the guest the order moved to another window", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost({ code: 4002, reason: "superseded", idle: false, kind: "superseded", resuming: false }));
        expect(screen.getByText("status.superseded")).toBeInTheDocument();
    });

    it("retries exhausted during a resume: connection lost, and the next tap starts fresh", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        await act(async () => rt.params.onReconnectGaveUp());
        expect(screen.getByText("status.connectionLost")).toBeInTheDocument();
        rt.api.isConnected = false;
        await tapMic();
        expect(rt.api.reconnect).toHaveBeenCalledTimes(1);
        expect(shows("Resume Test Latte")).toBe(false);
    });

    it("retries exhausted with no resume in flight changes nothing", async () => {
        render(<App />);
        await act(async () => rt.params.onReconnectGaveUp());
        expect(screen.queryByText("status.connectionLost")).toBeNull();
    });

    it("start a new order: end_session, a clean ticket, and no lost-connection notice for our own close", async () => {
        await startConversationWithLatte();
        await act(async () => {
            fireEvent.click(screen.getByText("app.newOrder"));
        });
        expect(rt.api.endSession).toHaveBeenCalledTimes(1);
        expect(rec.stop).toHaveBeenCalled();
        expect(shows("Resume Test Latte")).toBe(false);
        expect(screen.queryByText("app.newOrder")).toBeNull();
        await act(async () => rt.params.onConnectionLost(ended));
        expect(screen.queryByText("status.connectionLost")).toBeNull();
    });

    it("a tap made before the old session finishes closing carries on into the fresh one", async () => {
        await startConversationWithLatte();
        await tapMic(); // stop
        await act(async () => {
            fireEvent.click(screen.getByText("app.newOrder"));
        });
        await tapMic(); // start the next order at once
        rec.stop.mockClear();
        await act(async () => rt.params.onConnectionLost(ended));
        expect(rec.stop).not.toHaveBeenCalled();
        expect(micButton()).toHaveAccessibleName("app.stopRecording");
        expect(screen.queryByText("status.connectionLost")).toBeNull();
    });

    it("a tap on a resumed session never waits for (or restarts the mic after) a greeting", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockImplementation(async () => false);
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        rec.start.mockImplementation(async () => true);
        rec.start.mockClear();
        await tapMic();
        // The crew member's 30 s nudge finishing must not start a second capture.
        await act(async () => rt.params.onReceivedResponseDone({ response: { output: [{ content: [{ transcript: "Anything else?" }] }] } }));
        expect(rec.start).toHaveBeenCalledTimes(1);
    });

    it("a fresh session's metadata ends the resumed state: the next tap waits for the greeting again", async () => {
        await startConversationWithLatte();
        await act(async () => rt.params.onConnectionLost(transportDrop));
        rec.start.mockImplementation(async () => false);
        await act(async () => rt.params.onReceivedSessionResumed(resumedMsg()));
        act(() => rt.params.onReceivedSessionMetadata({ type: "extension.session_metadata", sessionToken: "S2", roundTripIndex: 0, roundTripToken: "T" }));
        rec.start.mockImplementation(async () => true);
        rec.start.mockClear();
        await tapMic();
        expect(rec.start).not.toHaveBeenCalled();
    });
});
