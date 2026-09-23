import "./status-message.css";
import { useTranslation } from "react-i18next";

export type ConnectionNotice = "lost" | null;
/** "retrying": the apology clip is playing and the middle tier is retrying; "busy": it gave up. */
export type RateLimitNotice = "retrying" | "busy" | null;

type Properties = {
    isRecording: boolean;
    notice?: ConnectionNotice;
    rateLimit?: RateLimitNotice;
};

export default function StatusMessage({ isRecording, notice = null, rateLimit = null }: Properties) {
    const { t } = useTranslation();
    if (!isRecording) {
        return (
            <p className="text mb-4 mt-6 text-sm text-muted-foreground" aria-live="polite">
                {t(notice === "lost" ? "status.connectionLost" : "status.notRecordingMessage")}
            </p>
        );
    }

    return (
        <div aria-live="polite">
            <div className="flex items-center">
                <div className="listening-equalizer">
                    {[...Array(4)].map((_, index) => (
                        <span key={index} className={`bar bar-${(index % 3) + 1}`} />
                    ))}
                </div>
                <p className="mb-4 ml-2 mt-6 font-semibold text-primary">
                    {t("status.conversationInProgress")}
                </p>
            </div>
            {rateLimit && (
                <p role="status" className="-mt-2 mb-4 text-sm text-muted-foreground">
                    {t(rateLimit === "busy" ? "status.rateLimitFinal" : "status.rateLimitRetrying")}
                </p>
            )}
        </div>
    );
}
