import { useState, useEffect, useRef } from "react";
import { Mic, MicOff, Menu, MessageSquare, LogOut, Github } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";

import StatusMessage from "@/components/ui/status-message";
import MenuPanel from "@/components/ui/menu-panel";
import OrderSummary, { calculateOrderSummary, OrderSummaryProps } from "@/components/ui/order-summary";
import TranscriptPanel from "@/components/ui/transcript-panel";
import Settings from "@/components/ui/settings";
// import ImageDialog from "@/components/ui/ImageDialog";

import useRealTime from "@/hooks/useRealtime";
import useAzureSpeech from "@/hooks/useAzureSpeech";
import useAudioRecorder from "@/hooks/useAudioRecorder";
import useAudioPlayer from "@/hooks/useAudioPlayer";

import { ExtensionMiddleTierToolResponse, ExtensionRoundTripToken, ExtensionSessionMetadata } from "./types";

import { ThemeProvider, useTheme } from "./context/theme-context";
import { DummyDataProvider, useDummyDataContext } from "@/context/dummy-data-context";
import { AzureSpeechProvider, useAzureSpeechOnContext } from "@/context/azure-speech-context";
import { AuthProvider, useAuth } from "@/context/auth-context";

import dummyTranscriptsData from "@/data/dummyTranscripts.json";
import dummyOrderData from "@/data/dummyOrder.json";
import azureLogo from "@/assets/azurelogo.svg";
import dunkinLogo from "@/assets/dunkin-logo.svg";

type HighlightTone = "orange" | "pink" | "yellow";

type SessionIdentifiersState = {
    sessionToken: string;
    roundTripIndex: number;
    roundTripToken: string;
};

const heroHighlights: Array<{ title: string; detail: string; tone: HighlightTone }> = [
    {
        title: "Rewards Ready",
        detail: "Voice orders auto-sync with Dunkin' Rewards boosts",
        tone: "orange"
    },
    {
        title: "Azure Infusion",
        detail: "Azure OpenAI + Speech keep conversations flowing",
        tone: "pink"
    },
    {
        title: "Live Menu",
        detail: "Azure AI Search keeps Dunkin' items current",
        tone: "yellow"
    }
];

const heroCallouts = [
    { label: "Donut of the Day", value: "Boston Kreme", accent: "#E3007F" },
    { label: "Crew Pick", value: "Caramel Craze Latte", accent: "#FF671F" }
];

function CoffeeApp() {
    const [isRecording, setIsRecording] = useState(false);
    const [isMobile, setIsMobile] = useState(false);
    const { useAzureSpeechOn } = useAzureSpeechOnContext();
    const { useDummyData } = useDummyDataContext();
    const { theme } = useTheme();
    const { logout, authEnabled } = useAuth();

    const [transcripts, setTranscripts] = useState<Array<{ text: string; isUser: boolean; timestamp: Date }>>(() => {
        return [];
    });
    const [dummyTranscripts] = useState<Array<{ text: string; isUser: boolean; timestamp: Date }>>(() => {
        return dummyTranscriptsData.map(transcript => ({
            ...transcript,
            timestamp: new Date(transcript.timestamp)
        }));
    });

    const initialOrder: OrderSummaryProps = {
        items: [],
        total: 0,
        tax: 0,
        finalTotal: 0
    };

    const dummyOrder: OrderSummaryProps = calculateOrderSummary(dummyOrderData);

    const [order, setOrder] = useState<OrderSummaryProps>(initialOrder);
    const [sessionIdentifiers, setSessionIdentifiers] = useState<SessionIdentifiersState | null>(null);
    const [showSessionTokens, setShowSessionTokens] = useState<boolean>(() => {
        if (typeof window === "undefined") return true;
        const stored = localStorage.getItem("showSessionTokens");
        return stored === null ? true : stored === "true";
    });

    useEffect(() => {
        localStorage.setItem("showSessionTokens", showSessionTokens.toString());
    }, [showSessionTokens]);

    const handleSessionIdentifiers = (message: ExtensionSessionMetadata | ExtensionRoundTripToken) => {
        setSessionIdentifiers({
            sessionToken: message.sessionToken,
            roundTripIndex: message.roundTripIndex,
            roundTripToken: message.roundTripToken
        });
    };

    const isSessionActiveRef = useRef(false);
    const awaitingGreetingDoneRef = useRef(false);
    const greetingAudioSeenRef = useRef(false);
    const startMicInFlightRef = useRef<Promise<void> | null>(null);

    const realtime = useRealTime({
        enableInputAudioTranscription: true,
        onWebSocketOpen: () => console.log("WebSocket connection opened"),
        onWebSocketClose: () => console.log("WebSocket connection closed"),
        onWebSocketError: event => console.error("WebSocket error:", event),
        onReceivedError: message => console.error("error", message),
        onReceivedResponseAudioDelta: message => {
            if (!isSessionActiveRef.current) return;
            greetingAudioSeenRef.current = true;
            playAudio(message.delta);
        },
        onReceivedInputAudioBufferSpeechStarted: () => {
            stopAudioPlayer();
        },
        onReceivedExtensionMiddleTierToolResponse: ({ tool_name, tool_result }: ExtensionMiddleTierToolResponse) => {
            if (tool_name === "update_order") {
                const orderSummary: OrderSummaryProps = JSON.parse(tool_result);
                setOrder(orderSummary);

                console.log("Order Total:", orderSummary.total);
                console.log("Tax:", orderSummary.tax);
                console.log("Final Total:", orderSummary.finalTotal);
            }
        },
        onReceivedSessionMetadata: handleSessionIdentifiers,
        onReceivedRoundTripToken: handleSessionIdentifiers,
        onReceivedInputAudioTranscriptionCompleted: message => {
            const newTranscriptItem = {
                text: message.transcript,
                isUser: true,
                timestamp: new Date()
            };
            setTranscripts(prev => [...prev, newTranscriptItem]);
        },
        onReceivedResponseDone: message => {
            const transcript = message.response.output.map(output => output.content?.map(content => content.transcript).join(" ")).join(" ");
            if (!transcript) return;

            const newTranscriptItem = {
                text: transcript,
                isUser: false,
                timestamp: new Date()
            };
            setTranscripts(prev => [...prev, newTranscriptItem]);

            if (awaitingGreetingDoneRef.current && isSessionActiveRef.current) {
                awaitingGreetingDoneRef.current = false;

                if (!startMicInFlightRef.current) {
                    startMicInFlightRef.current = (async () => {
                        // If we received audio deltas for the greeting, wait until playback drains.
                        if (greetingAudioSeenRef.current) {
                            await waitForAudioDrain(2500);
                            // Small extra delay for device/driver buffer drain.
                            await new Promise(resolve => setTimeout(resolve, 150));
                        }

                        if (!isSessionActiveRef.current) return;
                        await startAudioRecording();
                    })().finally(() => {
                        startMicInFlightRef.current = null;
                    });
                }
            }
        }
    });

    const azureSpeech = useAzureSpeech({
        onReceivedToolResponse: ({ tool_name, tool_result }: ExtensionMiddleTierToolResponse) => {
            if (tool_name === "update_order") {
                const orderSummary: OrderSummaryProps = JSON.parse(tool_result);
                setOrder(orderSummary);

                console.log("Order Total:", orderSummary.total);
                console.log("Tax:", orderSummary.tax);
                console.log("Final Total:", orderSummary.finalTotal);
            }
        },
        onSpeechToTextTranscriptionCompleted: (message: { transcript: any }) => {
            const newTranscriptItem = {
                text: message.transcript,
                isUser: true,
                timestamp: new Date()
            };
            setTranscripts(prev => [...prev, newTranscriptItem]);
        },
        onModelResponseDone: (message: { response: { output: any[] } }) => {
            const transcript = message.response.output
                .map(output => output.content?.map((content: { transcript: any }) => content.transcript).join(" "))
                .join(" ");
            if (!transcript) return;

            const newTranscriptItem = {
                text: transcript,
                isUser: false,
                timestamp: new Date()
            };
            setTranscripts(prev => [...prev, newTranscriptItem]);
        },
        onError: (error: any) => console.error("Error:", error)
    });

    const { reset: resetAudioPlayer, play: playAudio, stop: stopAudioPlayer, waitForDrain: waitForAudioDrain } =
        useAudioPlayer();
    const { start: startAudioRecording, stop: stopAudioRecording } = useAudioRecorder({
        onAudioRecorded: useAzureSpeechOn ? azureSpeech.addUserAudio : realtime.addUserAudio
    });

    const onToggleListening = async () => {
        if (!isRecording) {
            setSessionIdentifiers(null);

            // Start session and playback immediately, but delay mic capture until the greeting finishes.
            isSessionActiveRef.current = true;
            awaitingGreetingDoneRef.current = !useAzureSpeechOn;
            greetingAudioSeenRef.current = false;

            await resetAudioPlayer();

            if (useAzureSpeechOn) {
                // AzureSpeech mode doesn't play a synthesized greeting audio stream.
                azureSpeech.startSession();
                await startAudioRecording();
            } else {
                realtime.startSession();

                // Safety: if we never receive the greeting completion, start the mic after a short timeout.
                window.setTimeout(() => {
                    if (!isSessionActiveRef.current) return;
                    if (!awaitingGreetingDoneRef.current) return;
                    awaitingGreetingDoneRef.current = false;
                    if (startMicInFlightRef.current) return;
                    startMicInFlightRef.current = startAudioRecording().finally(() => {
                        startMicInFlightRef.current = null;
                    });
                }, 5000);
            }

            setIsRecording(true);
        } else {
            await stopAudioRecording();
            stopAudioPlayer();
            isSessionActiveRef.current = false;
            awaitingGreetingDoneRef.current = false;
            if (useAzureSpeechOn) {
                azureSpeech.inputAudioBufferClear();
            } else {
                realtime.inputAudioBufferClear();
            }
            setIsRecording(false);
        }
    };

    const { t } = useTranslation();

    useEffect(() => {
        const checkMobile = () => {
            setIsMobile(window.innerWidth < 768);
        };
        checkMobile();
        window.addEventListener("resize", checkMobile);
        return () => window.removeEventListener("resize", checkMobile);
    }, []);

    return (
        <div className={`min-h-screen bg-background p-4 text-foreground ${theme}`}>
            <div className="mx-auto max-w-7xl space-y-6">
                <div className="flex flex-col gap-3 text-sm font-semibold text-primary md:flex-row md:items-center md:justify-between">
                    <a
                        href="https://github.com/swigerb/dunkin-chat-voice-assistant"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 rounded-full bg-white/80 px-3 py-1 text-primary transition hover:text-accent"
                        title="View Dunkin Voice Crew source"
                    >
                        <Github className="h-4 w-4" />
                        <span>Source on GitHub</span>
                    </a>
                    <div className="flex items-center gap-2">
                        <Settings
                            isMobile={isMobile}
                            showSessionTokens={showSessionTokens}
                            onShowSessionTokensChange={setShowSessionTokens}
                        />
                        {authEnabled && (
                            <Button variant="ghost" size="icon" className="rounded-full" onClick={logout} title="Logout">
                                <LogOut className="h-4 w-4" />
                            </Button>
                        )}
                    </div>
                </div>

                {sessionIdentifiers && showSessionTokens && <SessionTokenBanner identifiers={sessionIdentifiers} />}

                <BrandHero />

                <div className="grid grid-cols-1 gap-4 md:grid-cols-3 md:gap-8">
                    {/* Mobile Menu Button */}
                    <Sheet>
                        <SheetTrigger asChild>
                            <Button variant="outline" className="mb-4 flex w-full items-center justify-center md:hidden">
                                <Menu className="mr-2 h-4 w-4" />
                                View Dunkin Menu
                            </Button>
                        </SheetTrigger>
                        <SheetContent side="left" className="w-[300px] sm:w-[400px]">
                            <SheetHeader>
                                <SheetTitle>Dunkin Favorites</SheetTitle>
                            </SheetHeader>
                            <div className="h-[calc(100vh-4rem)] overflow-auto pr-4">
                                <MenuPanel />
                            </div>
                        </SheetContent>
                    </Sheet>

                    {/* Desktop Menu Panel */}
                    <Card className="hidden p-6 md:block">
                        <h2 className="mb-4 text-center font-semibold text-primary">Dunkin Favorites</h2>
                        <div className="h-[calc(100vh-13rem)] overflow-auto pr-4">
                            <MenuPanel />
                        </div>
                    </Card>

                    {/* Center Panel - Recording Button and Order Summary */}
                    <Card className="p-6 md:overflow-auto">
                        <div className="space-y-8">
                            <OrderSummary order={useDummyData ? dummyOrder : order} />
                            <div className="mb-4 flex flex-col items-center justify-center">
                                <Button
                                    onClick={onToggleListening}
                                    className={`h-12 w-60 border-none font-semibold shadow-lg transition-colors ${
                                        isRecording ? "bg-[#E3007F] text-white hover:bg-[#c2006c]" : "bg-[#FF671F] text-white hover:bg-[#d9551a]"
                                    }`}
                                    aria-label={isRecording ? t("app.stopRecording") : t("app.startRecording")}
                                >
                                    {isRecording ? (
                                        <>
                                            <MicOff className="mr-2 h-4 w-4" />
                                            {t("app.stopConversation")}
                                        </>
                                    ) : (
                                        <>
                                            <Mic className="mr-2 h-6 w-6" />
                                        </>
                                    )}
                                </Button>
                                <StatusMessage isRecording={isRecording} />
                            </div>
                        </div>
                    </Card>

                    {/* Mobile Transcript Button */}
                    <Sheet>
                        <SheetTrigger asChild>
                            <Button variant="outline" className="mt-4 flex w-full items-center justify-center md:hidden">
                                <MessageSquare className="mr-2 h-4 w-4" />
                                Transcript
                            </Button>
                        </SheetTrigger>
                        <SheetContent side="right" className="w-[300px] sm:w-[400px]">
                            <SheetHeader>
                                <SheetTitle>Guest Conversation</SheetTitle>
                            </SheetHeader>
                            <div className="h-[calc(100vh-4rem)] overflow-auto pr-4">
                                <TranscriptPanel transcripts={useDummyData ? dummyTranscripts : transcripts} />
                            </div>
                        </SheetContent>
                    </Sheet>

                    {/* Desktop Transcript Panel */}
                    <Card className="hidden p-6 md:block">
                        <h2 className="mb-4 text-center font-semibold text-primary">Guest Conversation</h2>
                        <div className="h-[calc(100vh-13rem)] overflow-auto pr-4">
                            <TranscriptPanel transcripts={useDummyData ? dummyTranscripts : transcripts} />
                        </div>
                    </Card>
                </div>
            </div>
            <footer className="mx-auto mt-8 max-w-4xl space-y-2 text-center text-xs text-muted-foreground">
                <p className="font-semibold uppercase tracking-[0.35em] text-[#C14200]/80">{t("app.footer")}</p>
                <p className="text-[11px] leading-relaxed text-[#7A2E10]/80">
                    Disclaimer: This project is a non-commercial demo application created for educational and illustrative purposes only. It is not
                    affiliated with, endorsed, or sponsored by Inspire Brands, Inc. Any references to Dunkin' or use of Dunkin-inspired colors or
                    themes are solely for demonstration and do not represent an official product.
                </p>
            </footer>
            {/* <Button onClick={() => onUserRequestShowImage("Espresso")}>Show Espresso Image</Button>
            {imageDialogOpen && <ImageDialog imageUrl={imageUrl} onClose={() => setImageDialogOpen(false)} />} */}
        </div>
    );
}

function BrandHero() {
    return (
        <section className="hero-card rounded-[32px] border border-white/40 bg-white/80 p-6 shadow-[0_25px_70px_rgba(255,103,31,0.18)] backdrop-blur-lg">
            <div className="flex flex-col gap-8 lg:flex-row lg:items-center">
                <div className="flex-1 space-y-5">
                    <div className="flex flex-wrap items-center gap-3">
                        <img src={dunkinLogo} alt="Dunkin logo" className="h-10 w-auto drop-shadow-sm" loading="lazy" />
                        <span className="rounded-full bg-[#FFE3CB] px-3 py-1 text-xs font-black uppercase tracking-[0.3em] text-[#C14200]">
                            Voice Crew Demo
                        </span>
                    </div>
                    <h1 className="text-4xl font-black leading-tight text-[#FF671F] sm:text-5xl">Dunkin ordering powered by Azure conversation intelligence</h1>
                    <p className="max-w-2xl text-base text-muted-foreground">
                        Recreate the Dunkin drive-thru vibe with donuts, cold brew, and breakfast essentials styled after the current dunkindonuts.com
                        experience—now voice activated with Azure OpenAI + Azure AI Search grounding.
                    </p>
                    <div className="grid gap-3 sm:grid-cols-3">
                        {heroHighlights.map(highlight => (
                            <HeroHighlightCard key={highlight.title} {...highlight} />
                        ))}
                    </div>
                    <div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-[0.3em] text-muted-foreground">
                        <img src={azureLogo} alt="Microsoft Azure" className="h-6 w-auto" loading="lazy" />
                        <span>Azure OpenAI · Azure Speech · Azure AI Search</span>
                    </div>
                </div>
                <div className="relative flex flex-1 items-center justify-center">
                    <div className="absolute inset-0 -z-10 rounded-[32px] bg-gradient-to-br from-[#FFE0CF] via-[#FFF3EC] to-[#FFE4F5] opacity-80 blur-3xl"></div>
                    <div className="grid w-full gap-4 sm:grid-cols-2">
                        <div className="rounded-3xl border border-[#FF9F45]/30 bg-white/90 p-4 shadow-[0_25px_45px_rgba(255,103,31,0.2)]">
                            <div className="mb-3 flex items-center gap-3">
                                <div className="rounded-2xl bg-[#FFF2E5] p-3">
                                    <DonutArt />
                                </div>
                                <div>
                                    <p className="text-xs font-bold uppercase tracking-wide text-[#FF671F]">Fresh donuts</p>
                                    <p className="text-sm font-semibold text-[#7A2E10]">MUNCHKINS assortment</p>
                                </div>
                            </div>
                            <ul className="text-xs font-medium text-[#7A2E10]/80">
                                {heroCallouts.map(callout => (
                                    <li key={callout.label} className="flex items-center justify-between rounded-full bg-white/80 px-3 py-1">
                                        <span>{callout.label}</span>
                                        <span style={{ color: callout.accent }}>{callout.value}</span>
                                    </li>
                                ))}
                            </ul>
                        </div>
                        <div className="rounded-3xl border border-[#E3007F]/25 bg-gradient-to-br from-[#FFE5F2] to-[#FFECE0] p-4 shadow-[0_25px_45px_rgba(227,0,127,0.15)]">
                            <div className="mb-3 flex items-center gap-3">
                                <div className="rounded-2xl bg-white/60 p-3">
                                    <CoffeeArt />
                                </div>
                                <div>
                                    <p className="text-xs font-bold uppercase tracking-wide text-[#E3007F]">Crew favorite</p>
                                    <p className="text-sm font-semibold text-[#7A2E10]">Brown Sugar Cream Cold Brew</p>
                                </div>
                            </div>
                            <div className="rounded-2xl bg-white/80 p-3 text-sm font-semibold text-[#7A2E10]">
                                <p>Layered with cinnamon-sugar cold foam</p>
                                <p className="text-xs text-[#E3007F]">Perfect pairing: Bacon Egg & Cheese</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </section>
    );
}

function HeroHighlightCard({ title, detail, tone }: { title: string; detail: string; tone: HighlightTone }) {
    const gradientMap: Record<HighlightTone, string> = {
        orange: "from-[#FF671F] to-[#FF9F45]",
        pink: "from-[#E3007F] to-[#FF9FC5]",
        yellow: "from-[#FFD400] to-[#FFE784]"
    };

    return (
        <div className={`rounded-2xl bg-gradient-to-br ${gradientMap[tone]} p-3 text-white shadow-[0_10px_25px_rgba(0,0,0,0.08)]`}>
            <p className="text-xs uppercase tracking-[0.25em] text-white/80">{title}</p>
            <p className="text-sm font-semibold leading-tight">{detail}</p>
        </div>
    );
}

function SessionTokenBanner({ identifiers }: { identifiers: SessionIdentifiersState }) {
    const truncatedSession = formatToken(identifiers.sessionToken);
    const truncatedRoundTrip = formatToken(identifiers.roundTripToken, 6);

    return (
        <div className="flex flex-wrap gap-2 rounded-3xl border border-white/40 bg-white/90 p-3 font-mono text-xs text-primary shadow-sm">
            <div className="flex items-center gap-2" title={identifiers.sessionToken}>
                <span className="rounded-full bg-[#FFE3CB] px-2 py-1 font-semibold uppercase tracking-widest text-[#C14200]">Session Token</span>
                <span className="text-sm text-[#7A2E10]">{truncatedSession}</span>
            </div>
            <div className="flex items-center gap-2" title={identifiers.roundTripToken}>
                <span className="rounded-full bg-[#FEE6F3] px-2 py-1 font-semibold uppercase tracking-widest text-[#E3007F]">
                    Round {identifiers.roundTripIndex}
                </span>
                <span className="text-sm text-[#7A2E10]">{truncatedRoundTrip}</span>
            </div>
        </div>
    );
}

function formatToken(token: string, prefix: number = 8, suffix: number = 4): string {
    if (!token) {
        return "";
    }
    if (token.length <= prefix + suffix + 3) {
        return token;
    }
    return `${token.slice(0, prefix)}…${token.slice(-suffix)}`;
}

function DonutArt() {
    return (
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none" role="img" aria-label="Dunkin donut illustration">
            <circle cx="24" cy="24" r="22" fill="#FFE0EF" stroke="#E3007F" strokeWidth="2" />
            <circle cx="24" cy="24" r="10" fill="#FFF8FB" stroke="#FF9FC5" strokeWidth="2" />
            <path d="M12 22c3 4 8 6 12 4s8-1 12 1" stroke="#FF671F" strokeWidth="2" strokeLinecap="round" />
            <path d="M16 30c2 1 4 1 6-1" stroke="#FFD400" strokeWidth="2" strokeLinecap="round" />
            <circle cx="18" cy="16" r="1.2" fill="#FF671F" />
            <circle cx="32" cy="18" r="1.2" fill="#FFD400" />
            <circle cx="20" cy="33" r="1.2" fill="#E3007F" />
        </svg>
    );
}

function CoffeeArt() {
    return (
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none" role="img" aria-label="Dunkin iced coffee illustration">
            <rect x="14" y="6" width="20" height="36" rx="10" fill="#FFE8D6" stroke="#FF671F" strokeWidth="2" />
            <rect x="16" y="10" width="16" height="24" rx="8" fill="#F2C097" />
            <rect x="16" y="18" width="16" height="8" fill="#D07A4A" opacity="0.8" />
            <path d="M24 6V2" stroke="#E3007F" strokeWidth="2" strokeLinecap="round" />
            <path d="M18 28l12-4" stroke="#FFECD1" strokeWidth="2" strokeLinecap="round" />
            <circle cx="22" cy="24" r="1.2" fill="#FFECD1" />
            <circle cx="26" cy="21" r="1.2" fill="#FFECD1" />
        </svg>
    );
}

// Main app component with authentication wrapper
function App() {
    const { isAuthenticated, isLoading, authEnabled } = useAuth();

    if (isLoading) {
        return (
            <div className="flex min-h-screen items-center justify-center">
                <div className="text-center">
                    <div className="mx-auto mb-4 h-12 w-12 animate-spin rounded-full border-4 border-primary border-t-transparent"></div>
                    <p className="text-lg">Loading...</p>
                </div>
            </div>
        );
    }

    if (!isAuthenticated && authEnabled) {
        return null; // Auth provider will handle redirect
    }

    return <CoffeeApp />;
}

export default function RootApp() {
    return (
        <AuthProvider>
            <ThemeProvider>
                <DummyDataProvider>
                    <AzureSpeechProvider>
                        <App />
                    </AzureSpeechProvider>
                </DummyDataProvider>
            </ThemeProvider>
        </AuthProvider>
    );
}
