import { useCallback, useEffect, useRef, useState } from "react";
import useWebSocket, { ReadyState } from "react-use-websocket";

import {
    InputAudioBufferAppendCommand,
    InputAudioBufferClearCommand,
    Message,
    ResponseAudioDelta,
    ResponseAudioTranscriptDelta,
    ResponseDone,
    SessionUpdateCommand,
    ExtensionMiddleTierToolResponse,
    ResponseInputAudioTranscriptionCompleted,
    ExtensionSessionMetadata,
    ExtensionRoundTripToken,
    ExtensionRateLimited,
    ExtensionSessionResumed,
    ExtensionResumeRejected
} from "@/types";

type Parameters = {
    useDirectAoaiApi?: boolean; // If true, the middle tier will be skipped and the AOAI ws API will be called directly
    aoaiEndpointOverride?: string;
    aoaiApiKeyOverride?: string;
    aoaiModelOverride?: string;

    enableInputAudioTranscription?: boolean;
    onWebSocketOpen?: () => void;
    onWebSocketClose?: () => void;
    /** Fired whenever the socket closes; `info.resuming` says whether a resume attempt follows. */
    onConnectionLost?: (info: ConnectionLostInfo) => void;
    onWebSocketError?: (event: Event) => void;
    onWebSocketMessage?: (event: MessageEvent<any>) => void;

    onReceivedResponseAudioDelta?: (message: ResponseAudioDelta) => void;
    onReceivedInputAudioBufferSpeechStarted?: (message: Message) => void;
    onReceivedResponseDone?: (message: ResponseDone) => void;
    onReceivedExtensionMiddleTierToolResponse?: (message: ExtensionMiddleTierToolResponse) => void;
    onReceivedSessionMetadata?: (message: ExtensionSessionMetadata) => void;
    onReceivedSessionResumed?: (message: ExtensionSessionResumed) => void;
    onReceivedResumeRejected?: (message: ExtensionResumeRejected) => void;
    /** Background reconnect gave up (retries exhausted); the socket stays down until reconnect(). */
    onReconnectGaveUp?: () => void;
    onReceivedRoundTripToken?: (message: ExtensionRoundTripToken) => void;
    onReceivedRateLimited?: (message: ExtensionRateLimited) => void;
    onReceivedResponseAudioTranscriptDelta?: (message: ResponseAudioTranscriptDelta) => void;
    onReceivedInputAudioTranscriptionCompleted?: (message: ResponseInputAudioTranscriptionCompleted) => void;
    onReceivedError?: (message: Message) => void;
};

/** The middle tier closes a session that has been idle for 5 minutes with this code. Never resumable. */
export const WS_CLOSE_IDLE_TIMEOUT = 4000;
/** Another socket resumed this session (session_manager.SUPERSEDED_CLOSE_CODE). */
export const WS_CLOSE_SUPERSEDED = 4002;
/** Reply to extension.end_session: 1000 with this reason. */
export const WS_CLOSE_SESSION_ENDED_REASON = "session_ended";

/** Per-tab resume credential (docs/order_resume.md). Never put it in a URL or localStorage. */
export const RESUME_STORAGE_KEY = "dunkin.resumeId";

export const resumeStore = {
    get(): string | null {
        try {
            return sessionStorage.getItem(RESUME_STORAGE_KEY);
        } catch {
            return null;
        }
    },
    set(id: string) {
        try {
            sessionStorage.setItem(RESUME_STORAGE_KEY, id);
        } catch {
            // storage unavailable: resume just won't work in this tab
        }
    },
    clear() {
        try {
            sessionStorage.removeItem(RESUME_STORAGE_KEY);
        } catch {
            // ignore
        }
    }
};

/** idle: 4000; superseded: 4002; ended: 1000 session_ended; transport: anything else (resumable). */
export type CloseKind = "idle" | "superseded" | "ended" | "transport";

export function classifyClose(event: Pick<CloseEvent, "code" | "reason">): CloseKind {
    if (event.code === WS_CLOSE_IDLE_TIMEOUT) return "idle";
    if (event.code === WS_CLOSE_SUPERSEDED) return "superseded";
    if (event.code === 1000 && event.reason === WS_CLOSE_SESSION_ENDED_REASON) return "ended";
    return "transport";
}

/** `idle`: the server ended the session for inactivity; the socket stays closed until reconnect(). */
export type ConnectionLostInfo = {
    code: number;
    reason: string;
    idle: boolean;
    kind: CloseKind;
    /** A background reconnect follows and presents the stored resume id. */
    resuming: boolean;
};

// Backoff 1s, 2s, 4s, 8s, 16s, then 30s: the first retry lands well inside the
// server's 120 s hold, and ten attempts outlast it.
const MAX_RETRIES = 10;
const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;

export default function useRealTime({
    useDirectAoaiApi,
    aoaiEndpointOverride,
    aoaiApiKeyOverride,
    aoaiModelOverride,
    enableInputAudioTranscription,
    onWebSocketOpen,
    onWebSocketClose,
    onConnectionLost,
    onWebSocketError,
    onWebSocketMessage,
    onReceivedResponseDone,
    onReceivedResponseAudioDelta,
    onReceivedResponseAudioTranscriptDelta,
    onReceivedInputAudioBufferSpeechStarted,
    onReceivedExtensionMiddleTierToolResponse,
    onReceivedInputAudioTranscriptionCompleted,
    onReceivedSessionMetadata,
    onReceivedSessionResumed,
    onReceivedResumeRejected,
    onReconnectGaveUp,
    onReceivedRoundTripToken,
    onReceivedRateLimited,
    onReceivedError
}: Parameters) {
    const wsEndpoint = useDirectAoaiApi
        ? `${aoaiEndpointOverride}/openai/v1/realtime?api-key=${aoaiApiKeyOverride}&model=${aoaiModelOverride}`
        : `/realtime`;

    // False once background retries are exhausted or the server idle-closed the
    // session; the next mic tap re-opens it.
    const [shouldConnect, setShouldConnect] = useState(true);

    const sendJsonMessageRef = useRef<(msg: object, keep?: boolean) => void>(() => {});
    // The hook owns the outgoing queue: react-use-websocket is only ever called
    // with keep=false, so its own queue stays empty and can never flush anything
    // ahead of extension.resume, which the server honours only as the first frame.
    const openRef = useRef(false);
    const pendingRef = useRef<object[]>([]);
    // Set by endSession(): the coming 1000 session_ended close is ours, and frames
    // sent after it (a fast tap) belong to the fresh session that replaces it.
    const endingRef = useRef(false);
    const send = useCallback((msg: object, keep = true) => {
        if (openRef.current) {
            sendJsonMessageRef.current(msg, false);
        } else if (keep) {
            pendingRef.current.push(msg);
        }
    }, []);

    const { sendJsonMessage, readyState } = useWebSocket(
        wsEndpoint,
        {
            onOpen: () => {
                openRef.current = true;
                // Literal first frame on every open when this tab holds a resume id.
                const resumeId = useDirectAoaiApi ? null : resumeStore.get();
                if (resumeId) sendJsonMessageRef.current({ type: "extension.resume", resume_id: resumeId }, false);
                for (const queued of pendingRef.current.splice(0)) sendJsonMessageRef.current(queued, false);
                onWebSocketOpen?.();
            },
            onClose: event => {
                openRef.current = false;
                const kind = classifyClose(event);
                if (kind === "ended") {
                    // Explicit new order: open a fresh session straight away, as a page load would.
                    if (!endingRef.current) pendingRef.current = [];
                    endingRef.current = false;
                    resumeStore.clear();
                    setShouldConnect(false);
                    window.setTimeout(() => setShouldConnect(true), 0);
                } else if (kind !== "transport") {
                    // Final for this session (idle 4000 / superseded 4002): park the
                    // socket until the next tap, and let nothing queued for it leak
                    // into the next one.
                    setShouldConnect(false);
                    pendingRef.current = [];
                    // 4002 keeps the id: another socket owns the session now.
                    if (kind !== "superseded") resumeStore.clear();
                }
                const resuming = kind === "transport" && !useDirectAoaiApi && !!resumeStore.get();
                onConnectionLost?.({ code: event.code, reason: event.reason ?? "", idle: kind === "idle", kind, resuming });
                onWebSocketClose?.();
            },
            onError: event => onWebSocketError?.(event),
            onMessage: event => onMessageReceived(event),
            shouldReconnect: event => classifyClose(event) === "transport",
            onReconnectStop: () => {
                setShouldConnect(false);
                onReconnectGaveUp?.();
            },
            reconnectAttempts: MAX_RETRIES,
            reconnectInterval: (attemptNumber: number) =>
                Math.min(BASE_DELAY_MS * Math.pow(2, attemptNumber), MAX_DELAY_MS) + Math.random() * 500
        },
        shouldConnect
    );

    useEffect(() => {
        sendJsonMessageRef.current = sendJsonMessage;
    }, [sendJsonMessage]);

    const isConnected = readyState === ReadyState.OPEN;

    const reconnect = useCallback(() => {
        if (!shouldConnect) setShouldConnect(true);
    }, [shouldConnect]);

    const startSession = () => {
        const command: SessionUpdateCommand = {
            type: "session.update",
            session: {
                turn_detection: {
                    type: "server_vad",
                    threshold: 0.7,
                    prefix_padding_ms: 300,
                    silence_duration_ms: 500
                }
            }
        };

        if (enableInputAudioTranscription) {
            command.session.input_audio_transcription = {
                model: "whisper-1"
            };
        }

        // Kept for the next socket; sent after extension.resume when one is pending.
        send(command);
    };

    const addUserAudio = (base64Audio: string) => {
        const command: InputAudioBufferAppendCommand = {
            type: "input_audio_buffer.append",
            audio: base64Audio
        };

        // keep=false: drop, never queue, mic audio while the socket is down.
        // Queued frames would be replayed onto the next socket ahead of the
        // guest's session.update.
        send(command, false);
    };

    const inputAudioBufferClear = () => {
        const command: InputAudioBufferClearCommand = {
            type: "input_audio_buffer.clear"
        };

        send(command, false);
    };

    const onMessageReceived = useCallback((event: MessageEvent<any>) => {
        onWebSocketMessage?.(event);

        let message: Message;
        try {
            message = JSON.parse(event.data);
        } catch (e) {
            console.error("Failed to parse JSON message:", e);
            throw e;
        }

        switch (message.type) {
            case "response.done":
                onReceivedResponseDone?.(message as ResponseDone);
                break;
            case "response.audio.delta":
                onReceivedResponseAudioDelta?.(message as ResponseAudioDelta);
                break;
            case "response.audio_transcript.delta":
                onReceivedResponseAudioTranscriptDelta?.(message as ResponseAudioTranscriptDelta);
                break;
            case "input_audio_buffer.speech_started":
                onReceivedInputAudioBufferSpeechStarted?.(message);
                break;
            case "conversation.item.input_audio_transcription.completed":
                onReceivedInputAudioTranscriptionCompleted?.(message as ResponseInputAudioTranscriptionCompleted);
                break;
            case "extension.middle_tier_tool_response":
                onReceivedExtensionMiddleTierToolResponse?.(message as ExtensionMiddleTierToolResponse);
                break;
            case "extension.session_metadata": {
                const metadata = message as ExtensionSessionMetadata;
                if (!useDirectAoaiApi && metadata.resumeId) resumeStore.set(metadata.resumeId);
                onReceivedSessionMetadata?.(metadata);
                break;
            }
            case "extension.session_resumed": {
                const resumed = message as ExtensionSessionResumed;
                if (resumed.resume_id) resumeStore.set(resumed.resume_id);
                onReceivedSessionResumed?.(resumed);
                break;
            }
            case "extension.resume_rejected":
                // The fresh session's extension.session_metadata (with a new id) follows.
                resumeStore.clear();
                onReceivedResumeRejected?.(message as ExtensionResumeRejected);
                break;
            case "extension.round_trip_token":
                onReceivedRoundTripToken?.(message as ExtensionRoundTripToken);
                break;
            case "extension.rate_limited":
                onReceivedRateLimited?.(message as ExtensionRateLimited);
                break;
            case "error":
                onReceivedError?.(message);
                break;
        }
    }, [
        onWebSocketMessage,
        onReceivedResponseDone,
        onReceivedResponseAudioDelta,
        onReceivedResponseAudioTranscriptDelta,
        onReceivedInputAudioBufferSpeechStarted,
        onReceivedInputAudioTranscriptionCompleted,
        onReceivedExtensionMiddleTierToolResponse,
        onReceivedSessionMetadata,
        onReceivedSessionResumed,
        onReceivedResumeRejected,
        onReceivedRoundTripToken,
        onReceivedRateLimited,
        onReceivedError,
        useDirectAoaiApi
    ]);

    const sendVoiceChoice = (voice: string) => {
        send({ type: "extension.set_voice", voice });
    };

    // Explicit new order: the server deletes the order and closes 1000
    // session_ended, after which a fresh socket opens. The id is dropped either
    // way so no later open resumes it; frames sent from here on wait for the new socket.
    const endSession = () => {
        resumeStore.clear();
        pendingRef.current = [];
        if (!useDirectAoaiApi && openRef.current) {
            send({ type: "extension.end_session" }, false);
            endingRef.current = true;
            openRef.current = false;
        }
    };

    return { startSession, addUserAudio, inputAudioBufferClear, sendVoiceChoice, endSession, isConnected, reconnect };
}
