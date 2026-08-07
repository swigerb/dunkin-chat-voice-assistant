import { useState, useEffect } from "react";
import { SettingsIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { useDummyDataContext } from "@/context/dummy-data-context";
import { useAzureSpeechOnContext } from "@/context/azure-speech-context";
import { Tooltip } from "@/components/ui/tooltip";

interface SettingsProps {
    isMobile: boolean;
    showSessionTokens: boolean;
    onShowSessionTokensChange: (checked: boolean) => void;
    voiceChoice: string;
    onVoiceChoiceChange: (voice: string) => void;
}

export default function Settings({ isMobile, showSessionTokens, onShowSessionTokensChange, voiceChoice, onVoiceChoiceChange }: SettingsProps) {
    const [isDarkMode, setIsDarkMode] = useState(() => {
        return localStorage.getItem("isDarkMode") === "true";
    });
    const { useAzureSpeechOn, setUseAzureSpeechOn } = useAzureSpeechOnContext();
    const { useDummyData, setUseDummyData } = useDummyDataContext();

    useEffect(() => {
        localStorage.setItem("isDarkMode", isDarkMode.toString());
        if (isDarkMode) {
            document.documentElement.classList.add("dark");
        } else {
            document.documentElement.classList.remove("dark");
        }
    }, [isDarkMode]);

    const handleDarkModeChange = (checked: boolean) => {
        setIsDarkMode(checked);
    };

    const handleAzureBackendChange = (checked: boolean) => {
        setUseAzureSpeechOn(checked);
    };

    const handleDummyDataChange = (checked: boolean) => {
        setUseDummyData(checked);
    };

    const handleSessionTokensChange = (checked: boolean) => {
        onShowSessionTokensChange(checked);
    };

    const SettingsContent = () => (
        <div className="space-y-6">
            <div className="flex items-start justify-between">
                <div className="flex-1 space-y-0.5">
                    <Label htmlFor="dark-mode" className="text-gray-900 dark:text-gray-100">
                        Dark Mode
                    </Label>
                    <p className="text-sm text-gray-600 dark:text-gray-400">Toggle between light and dark theme</p>
                </div>
                <div className="ml-4 flex items-center gap-3 shrink-0">
                    <span className="min-w-[5rem] text-right text-xs text-muted-foreground">{isDarkMode ? "Dark Mode" : "Light Mode"}</span>
                    <Switch id="dark-mode" checked={isDarkMode} onCheckedChange={handleDarkModeChange} aria-label="Toggle dark mode" />
                </div>
            </div>
            <div className="flex items-start justify-between">
                <div className="flex-1 space-y-0.5">
                    <Label htmlFor="voice-choice" className="text-gray-900 dark:text-gray-100">
                        AI Voice
                    </Label>
                    <p className="text-sm text-gray-600 dark:text-gray-400">Choose your drive-thru assistant's voice</p>
                </div>
                <div className="ml-4 flex flex-col items-end gap-1 shrink-0">
                    <select
                        id="voice-choice"
                        value={voiceChoice}
                        onChange={(e) => onVoiceChoiceChange(e.target.value)}
                        className="w-56 rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground"
                    >
                        <option value="alloy">Alloy — Neutral &amp; Versatile</option>
                        <option value="ash">Ash — Warm &amp; Friendly</option>
                        <option value="ballad">Ballad — Caring &amp; Soft</option>
                        <option value="coral">Coral — Confident &amp; Clear</option>
                        <option value="echo">Echo — Smooth &amp; Resonant</option>
                        <option value="sage">Sage — Calm &amp; Thoughtful</option>
                        <option value="shimmer">Shimmer — Cheerful &amp; Bright</option>
                        <option value="verse">Verse — Natural &amp; Adaptable</option>
                        <option value="marin">Marin — Fresh &amp; Modern</option>
                        <option value="cedar">Cedar — Deep &amp; Grounded</option>
                    </select>
                    <span className="text-xs text-muted-foreground">Default: Coral</span>
                </div>
            </div>
            <div className="flex items-start justify-between">
                <div className="flex-1 space-y-0.5">
                    <Label htmlFor="azure-backend" className="text-gray-900 dark:text-gray-100">
                        Azure Backend
                    </Label>
                    <p className="text-sm text-gray-600 dark:text-gray-400">
                        Toggle between Azure OpenAI real-time API and Azure Speech SDK (STT, LLM(GPT-4o), TTS)
                    </p>
                </div>
                <div className="ml-4 flex items-center gap-3 shrink-0">
                    <Tooltip content="Work in progress">
                        <div>
                            <Switch
                                id="azure-backend"
                                checked={useAzureSpeechOn}
                                onCheckedChange={handleAzureBackendChange}
                                aria-label="Toggle Azure backend"
                                disabled
                            />
                        </div>
                    </Tooltip>
                    <span className="min-w-[5rem] text-right text-xs text-muted-foreground">{useAzureSpeechOn ? "STT->LLM->TTS" : "Realtime API"}</span>
                </div>
            </div>
            <div className="flex items-start justify-between">
                <div className="flex-1 space-y-0.5">
                    <Label htmlFor="dummy-data" className="text-gray-900 dark:text-gray-100">
                        Dummy Data
                    </Label>
                    <p className="text-sm text-gray-600 dark:text-gray-400">Toggle between real data and dummy data</p>
                </div>
                <div className="ml-4 flex items-center gap-3 shrink-0">
                    <span className="min-w-[5rem] text-right text-xs text-muted-foreground">{useDummyData ? "Dummy Data" : "Real Data"}</span>
                    <Switch id="dummy-data" checked={useDummyData} onCheckedChange={handleDummyDataChange} aria-label="Toggle dummy data" />
                </div>
            </div>
            <div className="flex items-start justify-between">
                <div className="flex-1 space-y-0.5">
                    <Label htmlFor="session-token-visibility" className="text-gray-900 dark:text-gray-100">
                        Show Session Tokens
                    </Label>
                    <p className="text-sm text-gray-600 dark:text-gray-400">Toggle visibility of session token and round-trip IDs</p>
                </div>
                <div className="ml-4 flex items-center gap-3 shrink-0">
                    <span className="min-w-[5rem] text-right text-xs text-muted-foreground">{showSessionTokens ? "Visible" : "Hidden"}</span>
                    <Switch
                        id="session-token-visibility"
                        checked={showSessionTokens}
                        onCheckedChange={handleSessionTokensChange}
                        aria-label="Toggle session token visibility"
                    />
                </div>
            </div>
        </div>
    );

    if (isMobile) {
        return (
            <Sheet>
                <SheetTrigger asChild>
                    <Button variant="outline" size="icon">
                        <SettingsIcon className="h-[1.2rem] w-[1.2rem]" />
                        <span className="sr-only">Open settings</span>
                    </Button>
                </SheetTrigger>
                <SheetContent>
                    <SheetHeader>
                        <SheetTitle>Settings</SheetTitle>
                        <SheetDescription>Adjust your app preferences here.</SheetDescription>
                    </SheetHeader>
                    <SettingsContent />
                </SheetContent>
            </Sheet>
        );
    }

    return (
        <Dialog>
            <DialogTrigger asChild>
                <Button variant="outline" size="icon">
                    <SettingsIcon className="h-[1.2rem] w-[1.2rem]" />
                    <span className="sr-only">Open settings</span>
                </Button>
            </DialogTrigger>
            <DialogContent className="sm:max-w-[425px]">
                <DialogHeader>
                    <DialogTitle>Settings</DialogTitle>
                    <DialogDescription>Adjust your app preferences here.</DialogDescription>
                </DialogHeader>
                <SettingsContent />
            </DialogContent>
        </Dialog>
    );
}
