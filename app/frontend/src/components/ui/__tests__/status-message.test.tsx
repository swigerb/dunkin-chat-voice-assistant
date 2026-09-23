import { render, screen } from "@testing-library/react";
import StatusMessage from "../status-message";

describe("StatusMessage", () => {
    it("renders the idle helper when recording is disabled", () => {
        render(<StatusMessage isRecording={false} />);
        expect(screen.getByText("status.notRecordingMessage")).toBeInTheDocument();
    });

    it("renders the live equalizer label while recording", () => {
        const { container } = render(<StatusMessage isRecording />);
        expect(screen.getByText("status.conversationInProgress")).toBeInTheDocument();
        expect(container.querySelector(".listening-equalizer")).not.toBeNull();
    });

    it("tells the guest the connection dropped", () => {
        render(<StatusMessage isRecording={false} notice="lost" />);
        expect(screen.getByText("status.connectionLost")).toBeInTheDocument();
        expect(screen.queryByText("status.notRecordingMessage")).toBeNull();
    });

    it("tells the guest the session ended for inactivity", () => {
        render(<StatusMessage isRecording={false} notice="idle" />);
        expect(screen.getByText("status.sessionEndedIdle")).toBeInTheDocument();
        expect(screen.queryByText("status.connectionLost")).toBeNull();
    });

    it.each([
        ["reconnecting", "status.reconnecting"],
        ["tapToResume", "status.resumedTapToContinue"],
        ["resumeRejected", "status.resumeRejected"],
        ["superseded", "status.superseded"],
        ["resumed", "status.resumed"]
    ] as const)("shows the %s resume notice", (notice, key) => {
        render(<StatusMessage isRecording={false} notice={notice} />);
        expect(screen.getByText(key)).toBeInTheDocument();
        expect(screen.queryByText("status.notRecordingMessage")).toBeNull();
    });

    it("says the order came back in place of the live label while recording", () => {
        render(<StatusMessage isRecording notice="resumed" />);
        expect(screen.getByText("status.resumed")).toBeInTheDocument();
        expect(screen.queryByText("status.conversationInProgress")).toBeNull();
    });

    it("drops the notice once a conversation is running", () => {
        render(<StatusMessage isRecording notice="lost" />);
        expect(screen.getByText("status.conversationInProgress")).toBeInTheDocument();
        expect(screen.queryByText("status.connectionLost")).toBeNull();
    });
});
