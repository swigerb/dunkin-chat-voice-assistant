export interface VoiceOption {
    value: string;
    label: string;
    recommended?: boolean;
}

// Every built-in voice gpt-realtime-2.1 accepts (the service lists exactly these
// ten when it rejects anything else). OpenAI recommends marin and cedar for
// best quality, so they lead the list.
export const VOICE_OPTIONS: readonly VoiceOption[] = [
    { value: "marin", label: "Marin — Fresh & Modern (recommended)", recommended: true },
    { value: "cedar", label: "Cedar — Deep & Grounded (recommended)", recommended: true },
    { value: "alloy", label: "Alloy — Neutral & Versatile" },
    { value: "ash", label: "Ash — Warm & Friendly" },
    { value: "ballad", label: "Ballad — Caring & Soft" },
    { value: "coral", label: "Coral — Confident & Clear" },
    { value: "echo", label: "Echo — Smooth & Resonant" },
    { value: "sage", label: "Sage — Calm & Thoughtful" },
    { value: "shimmer", label: "Shimmer — Cheerful & Bright" },
    { value: "verse", label: "Verse — Natural & Adaptable" }
];

// Keep in sync with model.default_voice in app/backend/config.yaml.
export const DEFAULT_VOICE = "marin";

export function resolveVoice(stored: string | null | undefined): string {
    return stored && VOICE_OPTIONS.some(v => v.value === stored) ? stored : DEFAULT_VOICE;
}

export function voiceLabel(value: string): string {
    return VOICE_OPTIONS.find(v => v.value === value)?.label.split(" — ")[0] ?? value;
}
