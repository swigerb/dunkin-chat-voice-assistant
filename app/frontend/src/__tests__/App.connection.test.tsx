import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";

const rt = vi.hoisted(() => ({
    options: null as any,
    isConnected: true,
    reconnect: vi.fn(),
    startSession: vi.fn(),
    sendVoiceChoice: vi.fn(),
    inputAudioBufferClear: vi.fn(),
    addUserAudio: vi.fn()
}));
const mic = vi.hoisted(() => ({ start: vi.fn(async () => {}), stop: vi.fn(async () => {}) }));

vi.mock("@/hooks/useRealtime", () => ({
    default: (options: any) => {
        rt.options = options;
        return {
            startSession: rt.startSession,
            sendVoiceChoice: rt.sendVoiceChoice,
            inputAudioBufferClear: rt.inputAudioBufferClear,
            addUserAudio: rt.addUserAudio,
            isConnected: rt.isConnected,
            reconnect: rt.reconnect
        };
    }
}));
vi.mock("@/hooks/useAudioRecorder", () => ({ default: () => ({ start: mic.start, stop: mic.stop }) }));
vi.mock("@/hooks/useAudioPlayer", () => ({
    default: () => ({ reset: vi.fn(async () => {}), play: vi.fn(), stop: vi.fn(), waitForDrain: vi.fn(async () => {}) })
}));
// jsdom has no layout / color-scheme APIs.
vi.mock("darkreader", () => ({ enable: vi.fn(), disable: vi.fn(), auto: vi.fn(), setFetchMethod: vi.fn() }));
Element.prototype.scrollIntoView = vi.fn();

vi.mock("@/hooks/useAzureSpeech", () => ({
    default: () => ({ startSession: vi.fn(), addUserAudio: vi.fn(), inputAudioBufferClear: vi.fn() })
}));

const micButton = () => screen.getByRole("button", { name: /app\.(start|stop)Recording/ });
const orderUpdate = (name: string) => ({
    tool_name: "update_order",
    tool_result: JSON.stringify({ items: [{ item: name, size: "Medium", quantity: 1, price: 3.49, display: name }], total: 3.49, tax: 0.28, finalTotal: 3.77 })
});

async function tapMic() {
    await act(async () => {
        fireEvent.click(micButton());
    });
}

async function dropSocket(code = 1006) {
    await act(async () => {
        rt.options.onConnectionLost({ code, reason: code === 4000 ? "idle_timeout" : "", idle: code === 4000 });
    });
}

beforeEach(() => {
    localStorage.clear();
    rt.isConnected = true;
    for (const fn of [rt.reconnect, rt.startSession, rt.sendVoiceChoice, rt.inputAudioBufferClear, mic.start, mic.stop]) fn.mockClear();
});

describe("App connection loss", () => {
    it("ends the conversation, stops the mic and tells the guest", async () => {
        render(<App />);
        await tapMic();
        expect(micButton()).toHaveAccessibleName("app.stopRecording");

        await dropSocket();

        expect(mic.stop).toHaveBeenCalledTimes(1);
        expect(micButton()).toHaveAccessibleName("app.startRecording");
        expect(screen.getByText("status.connectionLost")).toBeInTheDocument();
    });

    it("never restarts the mic on its own after the socket comes back", async () => {
        vi.useFakeTimers();
        try {
            render(<App />);
            await tapMic();
            mic.start.mockClear();
            rt.startSession.mockClear();
            await dropSocket();

            await act(async () => {
                rt.options.onWebSocketOpen?.();
                await vi.advanceTimersByTimeAsync(6000);
            });

            expect(mic.start).not.toHaveBeenCalled();
            expect(rt.startSession).not.toHaveBeenCalled();
        } finally {
            vi.useRealTimers();
        }
    });

    it("stays quiet about a drop when no conversation or order was in progress", async () => {
        render(<App />);
        await dropSocket();
        expect(screen.queryByText("status.connectionLost")).toBeNull();
        expect(screen.getByText("status.notRecordingMessage")).toBeInTheDocument();
    });

    it("starts a fresh order on the next tap and re-opens a parked socket", async () => {
        render(<App />);
        await tapMic();
        await act(async () => {
            rt.options.onReceivedExtensionMiddleTierToolResponse(orderUpdate("Parity Test Cruller"));
        });
        expect(screen.getAllByText(/Parity Test Cruller/).length).toBeGreaterThan(0);

        rt.isConnected = false;
        await dropSocket();
        // The order is kept on screen until the guest starts over.
        expect(screen.getAllByText(/Parity Test Cruller/).length).toBeGreaterThan(0);

        await tapMic();

        expect(rt.reconnect).toHaveBeenCalledTimes(1);
        expect(screen.queryByText(/Parity Test Cruller/)).toBeNull();
        expect(screen.queryByText("status.connectionLost")).toBeNull();
        expect(rt.sendVoiceChoice).toHaveBeenCalledTimes(2);
        expect(rt.startSession).toHaveBeenCalledTimes(2);

        // The stale notice doesn't come back when the guest ends the new conversation.
        await tapMic();
        expect(screen.queryByText("status.connectionLost")).toBeNull();
        expect(screen.getByText("status.notRecordingMessage")).toBeInTheDocument();
    });

    it("says the session ended for inactivity after an idle close", async () => {
        render(<App />);
        await tapMic();
        await dropSocket(4000);

        expect(mic.stop).toHaveBeenCalledTimes(1);
        expect(micButton()).toHaveAccessibleName("app.startRecording");
        expect(screen.getByText("status.sessionEndedIdle")).toBeInTheDocument();
        expect(screen.queryByText("status.connectionLost")).toBeNull();
    });

    it("starts a fresh session and order on the tap after an idle close", async () => {
        render(<App />);
        await tapMic();
        await act(async () => {
            rt.options.onReceivedExtensionMiddleTierToolResponse(orderUpdate("Idle Test Cruller"));
        });
        rt.isConnected = false;
        await dropSocket(4000);
        expect(screen.getAllByText(/Idle Test Cruller/).length).toBeGreaterThan(0);

        await tapMic();

        expect(rt.reconnect).toHaveBeenCalledTimes(1);
        expect(screen.queryByText(/Idle Test Cruller/)).toBeNull();
        expect(screen.queryByText("status.sessionEndedIdle")).toBeNull();
        expect(rt.startSession).toHaveBeenCalledTimes(2);
    });

    it("does not reconnect or clear the order when the socket never dropped", async () => {
        render(<App />);
        await tapMic();
        await act(async () => {
            rt.options.onReceivedExtensionMiddleTierToolResponse(orderUpdate("Parity Test Cruller"));
        });
        await tapMic();
        await tapMic();

        expect(rt.reconnect).not.toHaveBeenCalled();
        expect(screen.getAllByText(/Parity Test Cruller/).length).toBeGreaterThan(0);
    });
});
