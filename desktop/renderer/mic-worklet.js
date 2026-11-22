class MicrophonePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSize = Math.round(sampleRate / 20);
    this.frame = new Int16Array(this.frameSize);
    this.offset = 0;
  }

  process(inputs) {
    const source = inputs[0]?.[0];
    if (!source) return true;
    for (const sample of source) {
      this.frame[this.offset++] = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
      if (this.offset === this.frameSize) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Int16Array(this.frameSize);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('jarvis-pcm', MicrophonePCM);
