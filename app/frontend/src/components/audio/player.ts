export class Player {
    private playbackNode: AudioWorkletNode | null = null;
    private drainWaiters: Array<() => void> = [];

    async init(sampleRate: number) {
        const audioContext = new AudioContext({ sampleRate });
        await audioContext.audioWorklet.addModule("audio-playback-worklet.js");

        this.playbackNode = new AudioWorkletNode(audioContext, "audio-playback-worklet");
        this.playbackNode.port.onmessage = event => {
            if (event?.data?.type === "drained") {
                const waiters = this.drainWaiters;
                this.drainWaiters = [];
                waiters.forEach(resolve => resolve());
            }
        };
        this.playbackNode.connect(audioContext.destination);
    }

    play(buffer: Int16Array) {
        if (this.playbackNode) {
            this.playbackNode.port.postMessage(buffer);
        }
    }

    waitForDrain(timeoutMs = 2000): Promise<boolean> {
        if (!this.playbackNode) {
            return Promise.resolve(false);
        }

        return new Promise(resolve => {
            const timer = window.setTimeout(() => {
                resolve(false);
            }, timeoutMs);

            this.drainWaiters.push(() => {
                window.clearTimeout(timer);
                resolve(true);
            });
        });
    }

    stop() {
        if (this.playbackNode) {
            this.playbackNode.port.postMessage(null);
        }
    }
}
