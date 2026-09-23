import "./status-message.css";
import { useTranslation } from "react-i18next";

export type ConnectionNotice = "lost" | null;

type Properties = {
    isRecording: boolean;
    notice?: ConnectionNotice;
};

export default function StatusMessage({ isRecording, notice = null }: Properties) {
    const { t } = useTranslation();
    if (!isRecording) {
        return (
            <p className="text mb-4 mt-6 text-sm text-muted-foreground" aria-live="polite">
                {t(notice === "lost" ? "status.connectionLost" : "status.notRecordingMessage")}
            </p>
        );
    }

    return (
        <div className="flex items-center" aria-live="polite">
            <div className="listening-equalizer">
                {[...Array(4)].map((_, index) => (
                    <span key={index} className={`bar bar-${(index % 3) + 1}`} />
                ))}
            </div>
            <p className="mb-4 ml-2 mt-6 font-semibold text-primary">
                {t("status.conversationInProgress")}
            </p>
        </div>
    );
}
