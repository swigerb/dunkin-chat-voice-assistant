import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Settings from "../settings";
import { DummyDataProvider } from "@/context/dummy-data-context";
import { AzureSpeechProvider } from "@/context/azure-speech-context";
import { DEFAULT_VOICE, VOICE_OPTIONS, resolveVoice } from "@/lib/voices";

// Every voice gpt-realtime-2.1 accepts (the service's own list when it rejects
// anything else). Must match _VALID_VOICES in app/backend/rtmt.py.
const GA_REALTIME_VOICES = ["alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse"];

function renderSettings(voiceChoice: string) {
    return render(
        <AzureSpeechProvider>
            <DummyDataProvider>
                <Settings
                    isMobile={false}
                    showSessionTokens={false}
                    onShowSessionTokensChange={() => {}}
                    voiceChoice={voiceChoice}
                    onVoiceChoiceChange={() => {}}
                />
            </DummyDataProvider>
        </AzureSpeechProvider>
    );
}

async function openPicker() {
    await userEvent.click(screen.getByRole("button", { name: /open settings/i }));
    return (await screen.findByLabelText("AI Voice")) as HTMLSelectElement;
}

describe("Dunkin voice picker", () => {
    beforeEach(() => localStorage.clear());

    it("offers every gpt-realtime-2.1 voice and defaults to marin", async () => {
        renderSettings(resolveVoice(localStorage.getItem("voiceChoice")));
        const picker = await openPicker();
        const offered = within(picker)
            .getAllByRole("option")
            .map(o => (o as HTMLOptionElement).value);

        expect([...offered].sort()).toEqual(GA_REALTIME_VOICES);
        expect(new Set(offered).size).toBe(offered.length);
        expect(picker.value).toBe("marin");
        expect(screen.getByText("Default: Marin")).toBeInTheDocument();
    });

    it("marks the OpenAI-recommended voices", async () => {
        renderSettings(DEFAULT_VOICE);
        const picker = await openPicker();
        const recommended = within(picker)
            .getAllByRole("option")
            .filter(o => /recommended/i.test(o.textContent ?? ""))
            .map(o => (o as HTMLOptionElement).value);
        expect(recommended.sort()).toEqual(["cedar", "marin"]);
        expect(VOICE_OPTIONS.filter(v => v.recommended).map(v => v.value).sort()).toEqual(["cedar", "marin"]);
    });

    it("keeps a stored valid voice and replaces an unknown one with the default", () => {
        expect(DEFAULT_VOICE).toBe("marin");
        expect(resolveVoice("shimmer")).toBe("shimmer");
        expect(resolveVoice("coral")).toBe("coral");
        expect(resolveVoice("nova")).toBe("marin");
        expect(resolveVoice(null)).toBe("marin");
        expect(resolveVoice("")).toBe("marin");
    });
});
