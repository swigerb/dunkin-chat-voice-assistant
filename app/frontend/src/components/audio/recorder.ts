export class Recorder {
    onDataAvailable: (buffer: Iterable<number>) => void;
    private audioContext: AudioContext | null = null;
    private mediaStream: MediaStream | null = null;
    private mediaStreamSource: MediaStreamAudioSourceNode | null = null;
    private workletNode: AudioWorkletNode | null = null;
    // A suspended AudioContext only resumes after a user gesture; don't hang on it.
    private static readonly RESUME_TIMEOUT_MS = 1500;

    public constructor(onDataAvailable: (buffer: Iterable<number>) => void) {
        this.onDataAvailable = onDataAvailable;
    }

    /** Resolves true when capture is running; false (mic released) otherwise. */
    async start(stream: MediaStream): Promise<boolean> {
        try {
            if (this.audioContext) {
                await this.audioContext.close();
            }

            this.audioContext = new AudioContext({ sampleRate: 24000 });

            if (this.audioContext.state === "suspended") {
                await Promise.race([this.audioContext.resume(), new Promise(resolve => setTimeout(resolve, Recorder.RESUME_TIMEOUT_MS))]);
                if ((this.audioContext.state as AudioContextState) !== "running") {
                    throw new Error("AudioContext is suspended until a user gesture");
                }
            }

            await this.audioContext.audioWorklet.addModule("./audio-processor-worklet.js");

            this.mediaStream = stream;
            this.mediaStreamSource = this.audioContext.createMediaStreamSource(this.mediaStream);

            this.workletNode = new AudioWorkletNode(this.audioContext, "audio-processor-worklet");
            this.workletNode.port.onmessage = event => {
                this.onDataAvailable(event.data.buffer);
            };

            this.mediaStreamSource.connect(this.workletNode);
            this.workletNode.connect(this.audioContext.destination);
            return true;
        } catch (error) {
            await this.stop();
            stream.getTracks().forEach(track => track.stop());
            return false;
        }
    }

    async stop() {
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(track => track.stop());
            this.mediaStream = null;
        }

        if (this.audioContext) {
            await this.audioContext.close();
            this.audioContext = null;
        }

        this.mediaStreamSource = null;
        this.workletNode = null;
    }
}
