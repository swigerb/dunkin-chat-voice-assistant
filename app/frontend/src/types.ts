// Represents a grounding file
export type GroundingFile = {
    id: string;
    name: string;
    content: string;
};

// Represents an item in the history
export type HistoryItem = {
    id: string;
    transcript: string;
    groundingFiles?: GroundingFile[];
    sender: "user" | "assistant";
    timestamp: Date; // Add timestamp field
};

// Represents a command to update the session
export type SessionUpdateCommand = {
    type: "session.update";
    session: {
        turn_detection?: {
            type: "server_vad" | "none";
            threshold?: number;
            prefix_padding_ms?: number;
            silence_duration_ms?: number;
        };
        input_audio_transcription?: {
            model: "whisper-1";
        };
    };
};

// Represents a command to append audio to the input buffer
export type InputAudioBufferAppendCommand = {
    type: "input_audio_buffer.append";
    audio: string; // Ensure this is a valid base64-encoded string
};

// Represents a command to clear the input audio buffer
export type InputAudioBufferClearCommand = {
    type: "input_audio_buffer.clear";
};

// Represents a generic message
export type Message = {
    type: string;
};

// Represents a response containing an audio delta
export type ResponseAudioDelta = {
    type: "response.audio.delta";
    delta: string; // Ensure this is a valid base64-encoded string
};

// Represents a response containing an audio transcript delta
export type ResponseAudioTranscriptDelta = {
    type: "response.audio_transcript.delta";
    delta: string;
};

// Represents a response indicating that input audio transcription is completed
export type ResponseInputAudioTranscriptionCompleted = {
    type: "conversation.item.input_audio_transcription.completed";
    event_id: string;
    item_id: string;
    content_index: number;
    transcript: string;
};

// Represents a response indicating that the response is done
export type ResponseDone = {
    type: "response.done";
    event_id: string;
    response: {
        id: string;
        output: { id: string; content?: { transcript: string; type: string }[] }[];
    };
};

// Represents a response from an extension middle tier tool
export type ExtensionMiddleTierToolResponse = {
    type: "extension.middle_tier_tool_response";
    previous_item_id: string;
    tool_name: string;
    tool_result: string; // JSON string that needs to be parsed into ToolResult
};

export type ExtensionSessionMetadata = {
    type: "extension.session_metadata";
    sessionToken: string;
    roundTripIndex: number;
    roundTripToken: string;
    /** Per-tab credential for resuming this session after a transport drop (docs/order_resume.md). */
    resumeId?: string;
};

/** The server re-attached this socket to the held session; the ticket comes back as it was. */
export type ExtensionSessionResumed = {
    type: "extension.session_resumed";
    order_summary: {
        items: Array<{ item: string; size: string; quantity: number; price: number; display: string }>;
        total: number;
        tax: number;
        finalTotal: number;
    };
    session_token: string;
    round_trip_index: number;
    round_trip_token: string;
    /** Rotated credential: the old one is spent. */
    resume_id: string;
};

/** The held session could not be resumed; a fresh session (with its own metadata) follows. */
export type ExtensionResumeRejected = {
    type: "extension.resume_rejected";
    reason: "disabled" | "malformed" | "unknown" | "expired" | "not_first_frame" | string;
};

export type ExtensionRoundTripToken = {
    type: "extension.round_trip_token";
    sessionToken: string;
    roundTripIndex: number;
    roundTripToken: string;
};

/** The middle tier's rate-limit ladder: attempt 1 = retrying (play the apology clip), final = gave up. */
export type ExtensionRateLimited = {
    type: "extension.rate_limited";
    attempt: number;
    final?: boolean;
};
