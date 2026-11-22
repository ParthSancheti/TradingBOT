'use strict';

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.JarvisVoiceFilter = factory();
  }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const BAND_COUNT = 28;

  function bucketEdges(bins) {
    const edges = [];
    const usable = Math.max(BAND_COUNT + 2, Math.floor(bins * 0.62));
    for (let i = 0; i <= BAND_COUNT; i++) {
      const t = i / BAND_COUNT;
      edges.push(Math.floor(Math.pow(t, 1.7) * (usable - 1)) + 1);
    }
    return edges;
  }

  class AudioBusImpl {
    constructor() {
      this.bands = new Float32Array(BAND_COUNT);
      this.level = 0;
      this.source = null;
      this._edges = null;
      this._edgesFor = 0;
      this._attack = 0.55;
      this._release = 0.14;
    }

    publish(freq, level, source) {
      this.source = source;
      if (!freq || source === null) {
        for (let i = 0; i < BAND_COUNT; i++) this.bands[i] *= 0.88;
        this.level += (0 - this.level) * 0.15;
        return;
      }
      if (!this._edges || this._edgesFor !== freq.length) {
        this._edges = bucketEdges(freq.length);
        this._edgesFor = freq.length;
      }
      for (let b = 0; b < BAND_COUNT; b++) {
        const lo = this._edges[b];
        const hi = Math.max(this._edges[b + 1], lo + 1);
        let peak = 0;
        for (let i = lo; i < hi && i < freq.length; i++) {
          if (freq[i] > peak) peak = freq[i];
        }
        const tilt = 1 + (b / BAND_COUNT) * 0.85;
        const target = Math.min(1, (peak / 255) * tilt);
        const k = target > this.bands[b] ? this._attack : this._release;
        this.bands[b] += (target - this.bands[b]) * k;
      }
      const k = level > this.level ? this._attack : this._release;
      this.level += (Math.min(1, level) - this.level) * k;
    }

    reset() {
      this.bands.fill(0);
      this.level = 0;
      this.source = null;
    }
  }

  function rmsLevel(pcm) {
    if (!pcm || !pcm.length) return 0;
    let sum = 0;
    for (let i = 0; i < pcm.length; i++) sum += pcm[i] * pcm[i];
    return Math.sqrt(sum / pcm.length);
  }

  function zeroCrossingRate(pcm) {
    if (!pcm || pcm.length < 2) return 0;
    let crossings = 0;
    for (let i = 1; i < pcm.length; i++) {
      if ((pcm[i - 1] >= 0) !== (pcm[i] >= 0)) crossings++;
    }
    return crossings / (pcm.length - 1);
  }

  function spectralCentroid(pcm) {
    if (!pcm || pcm.length < 2) return 0;
    let weighted = 0;
    let total = 0;
    for (let i = 1; i < pcm.length; i++) {
      weighted += Math.abs(pcm[i] - pcm[i - 1]);
      total += Math.abs(pcm[i]);
    }
    return total > 0 ? Math.min(1, weighted / total / 2) : 0;
  }

  function detectPitch(pcm, sampleRate = 16000) {
    const n = pcm.length;
    const minLag = Math.floor(sampleRate / 500);
    const maxLag = Math.min(Math.floor(sampleRate / 70), Math.floor(n / 2));
    if (maxLag <= minLag) return 0;

    let energy = 0;
    for (let i = 0; i < n; i++) energy += pcm[i] * pcm[i];
    if (energy / n < 0.00018) return 0;

    const corr = new Float32Array(maxLag + 1);
    let best = 0;
    for (let lag = minLag; lag <= maxLag; lag++) {
      let s = 0;
      let normA = 0;
      let normB = 0;
      const limit = n - lag;
      for (let i = 0; i < limit; i++) {
        const a = pcm[i];
        const b = pcm[i + lag];
        s += a * b;
        normA += a * a;
        normB += b * b;
      }
      const denom = Math.sqrt(normA * normB);
      const val = denom > 0 ? s / denom : 0;
      corr[lag] = val;
      if (val > best) best = val;
    }
    if (best < 0.3) return 0;

    let bestLag = 0;
    for (let lag = minLag + 1; lag < maxLag; lag++) {
      if (corr[lag] >= best * 0.88 && corr[lag] >= corr[lag - 1] && corr[lag] >= corr[lag + 1]) {
        bestLag = lag;
        break;
      }
    }
    if (!bestLag) return 0;
    return sampleRate / bestLag;
  }

  function variance(xs) {
    if (xs.length < 2) return 0;
    const m = xs.reduce((a, b) => a + b, 0) / xs.length;
    return xs.reduce((s, x) => s + (x - m) ** 2, 0) / xs.length;
  }

  class Vad {
    constructor(opts = {}) {
      this.threshold = opts.threshold ?? 0.018;
      this.minSpeechMs = opts.minSpeechMs ?? 180;
      this.hangoverMs = opts.hangoverMs ?? 700;
      this.noiseSpikeMs = opts.noiseSpikeMs ?? 280;
      this.speaking = false;
      this.aboveMs = 0;
      this.belowMs = 0;
      this.speechStartAt = 0;
    }

    configure(patch = {}) {
      if (patch.threshold !== undefined) this.threshold = patch.threshold;
      if (patch.minSpeechMs !== undefined) this.minSpeechMs = patch.minSpeechMs;
      if (patch.hangoverMs !== undefined) this.hangoverMs = patch.hangoverMs;
      if (patch.noiseSpikeMs !== undefined) this.noiseSpikeMs = patch.noiseSpikeMs;
    }

    push(level, dtMs, nowMs = Date.now()) {
      if (level > this.threshold) {
        this.aboveMs += dtMs;
        this.belowMs = 0;
        if (!this.speaking && this.aboveMs >= this.minSpeechMs) {
          this.speaking = true;
          this.speechStartAt = nowMs;
          return 'speech-start';
        }
      } else {
        this.belowMs += dtMs;
        if (!this.speaking) {
          this.aboveMs = 0;
          return null;
        }
        const dur = nowMs - this.speechStartAt;
        const limit = dur < this.noiseSpikeMs ? 160 : this.hangoverMs;
        if (this.belowMs >= limit) {
          this.speaking = false;
          this.aboveMs = 0;
          return 'speech-end';
        }
      }
      return null;
    }

    reset() {
      this.speaking = false;
      this.aboveMs = 0;
      this.belowMs = 0;
      this.speechStartAt = 0;
    }
  }

  /** 4-signal acoustic turn gate ported from Edtechh turnGate.ts */
  class TurnGate {
    constructor(opts = {}) {
      this.cfg = {
        threshold: opts.threshold ?? 0.018,
        minSpeechMs: opts.minSpeechMs ?? 240,
        hangoverMs: opts.hangoverMs ?? 450,
        maxZcr: opts.maxZcr ?? 0.34,
        minAttackFrames: opts.minAttackFrames ?? 2,
        minSyllables: opts.minSyllables ?? 1,
        syllableGapMs: opts.syllableGapMs ?? 70,
        syllableDip: opts.syllableDip ?? 0.55,
        strictInterrupt: opts.strictInterrupt ?? true,
      };
      this.history = [];
      this.maxHistory = 16;
      this.speaking = false;
      this.aboveMs = 0;
      this.belowMs = 0;
      this.syllables = 0;
      this.peakLevel = 0;
      this.inSyllable = false;
      this.silenceInTurnMs = 0;
      this.lastDisturbanceAt = 0;
      this.tutorSpeaking = false;
    }

    configure(patch = {}) {
      Object.assign(this.cfg, patch);
    }

    setTutorSpeaking(v) {
      this.tutorSpeaking = !!v;
    }

    get isSpeaking() {
      return this.speaking;
    }

    push(pcm, level, dtMs, nowMs = Date.now()) {
      const zcr = zeroCrossingRate(pcm);
      this.history.push({ level, zcr, dtMs });
      if (this.history.length > this.maxHistory) this.history.shift();

      if (level > this.cfg.threshold) {
        this.aboveMs += dtMs;
        this.belowMs = 0;
        this.silenceInTurnMs = 0;
        if (level > this.peakLevel) this.peakLevel = level;

        if (!this.inSyllable && level > this.cfg.threshold * 1.15) {
          this.inSyllable = true;
          this.syllables++;
        }
        if (this.speaking) return null;

        if (this.aboveMs >= this.cfg.minSpeechMs) {
          const check = this._evaluate();
          if (check.ok) {
            this.speaking = true;
            return { kind: 'speech-start', confidence: check.confidence };
          }
          if (nowMs - this.lastDisturbanceAt > 1200) {
            this.lastDisturbanceAt = nowMs;
            this.resetTurnCounters();
            return { kind: 'disturbance', reason: check.reason };
          }
          this.resetTurnCounters();
        }
      } else {
        this.belowMs += dtMs;
        this.silenceInTurnMs += dtMs;
        if (this.inSyllable && (level < this.peakLevel * this.cfg.syllableDip || this.silenceInTurnMs >= this.cfg.syllableGapMs)) {
          this.inSyllable = false;
          this.peakLevel = level;
        }
        if (!this.speaking) {
          if (this.belowMs > 180) this.resetTurnCounters();
          return null;
        }
        if (this.belowMs >= this.cfg.hangoverMs) {
          this.speaking = false;
          this.resetTurnCounters();
          return { kind: 'speech-end' };
        }
      }
      return null;
    }

    forceSpeechEnd() {
      if (!this.speaking) return null;
      this.speaking = false;
      this.resetTurnCounters();
      return { kind: 'speech-end' };
    }

    _evaluate() {
      const recent = this.history.slice(-10);
      const peakIdx = recent.reduce((best, f, i, a) => (f.level > a[best].level ? i : best), 0);
      const peak = recent[peakIdx].level;
      let attackFrames = 0;
      for (let i = Math.max(0, peakIdx - 4); i < peakIdx; i++) {
        if (recent[i].level > peak * 0.12 && recent[i].level <= peak * 0.88) attackFrames++;
      }
      if (attackFrames < this.cfg.minAttackFrames) {
        return { ok: false, reason: 'impact — instant attack' };
      }
      const loudFrames = recent.filter((f) => f.level > this.cfg.threshold);
      const avgZcr = loudFrames.length ? loudFrames.reduce((s, f) => s + f.zcr, 0) / loudFrames.length : 1;
      if (avgZcr > this.cfg.maxZcr) {
        return { ok: false, reason: 'unvoiced — clatter or hiss' };
      }
      if (this.syllables < this.cfg.minSyllables) {
        const spread = variance(loudFrames.map((f) => f.level));
        if (spread < 0.00004) return { ok: false, reason: 'flat — background hum' };
      }
      let confidence = 0.55 + Math.min(this.syllables, 4) * 0.1 + (0.34 - avgZcr);
      if (this.tutorSpeaking && this.cfg.strictInterrupt) {
        if (this.aboveMs < this.cfg.minSpeechMs * 1.6 && this.syllables < 2) {
          return { ok: false, reason: 'too brief to interrupt' };
        }
        confidence -= 0.1;
      }
      return { ok: true, confidence: Math.max(0, Math.min(1, confidence)) };
    }

    resetTurnCounters() {
      this.aboveMs = 0;
      this.syllables = 0;
      this.peakLevel = 0;
      this.inSyllable = false;
      this.silenceInTurnMs = 0;
    }

    reset() {
      this.history = [];
      this.speaking = false;
      this.belowMs = 0;
      this.lastDisturbanceAt = 0;
      this.resetTurnCounters();
    }
  }

  /** Local voiceprint guard ported from Edtechh speakerGuard.ts */
  class SpeakerGuard {
    constructor(enabled = true) {
      this.enabled = !!enabled;
      this.enrollTurns = 3;
      this.profile = null;
      this.turnFeatures = [];
    }

    setEnabled(v) {
      this.enabled = !!v;
    }

    reset() {
      this.profile = null;
      this.turnFeatures = [];
    }

    status() {
      const turns = this.profile?.turns || 0;
      return {
        enabled: this.enabled,
        enrolled: turns >= this.enrollTurns,
        turns,
        required: this.enrollTurns,
        pitchHz: this.profile ? Math.round(this.profile.f0) : 0,
      };
    }

    push(pcm, sampleRate = 16000, level = rmsLevel(pcm)) {
      if (!this.enabled || level < 0.014) return;
      const f0 = detectPitch(pcm, sampleRate);
      if (f0 < 70 || f0 > 500) return;
      this.turnFeatures.push({
        f0,
        centroid: spectralCentroid(pcm),
        zcr: zeroCrossingRate(pcm),
      });
    }

    endTurn() {
      const samples = this.turnFeatures;
      this.turnFeatures = [];
      if (!this.enabled || samples.length < 3) return { score: 1, pitchHz: 0, enrolled: !!this.profile && this.profile.turns >= this.enrollTurns };

      const f0s = samples.map((s) => s.f0).sort((a, b) => a - b);
      const medF0 = f0s[Math.floor(f0s.length / 2)];
      const meanCentroid = samples.reduce((s, a) => s + a.centroid, 0) / samples.length;
      const meanZcr = samples.reduce((s, a) => s + a.zcr, 0) / samples.length;

      if (!this.profile) {
        this.profile = { f0: medF0, centroid: meanCentroid, zcr: meanZcr, turns: 1 };
        return { score: 1, pitchHz: medF0, enrolled: false };
      }

      if (this.profile.turns < this.enrollTurns) {
        this._blend(medF0, meanCentroid, meanZcr, 0.35);
        return { score: 1, pitchHz: medF0, enrolled: this.profile.turns >= this.enrollTurns };
      }

      const f0Ratio = Math.abs(medF0 - this.profile.f0) / Math.max(this.profile.f0, 80);
      const f0Score = Math.max(0, 1 - f0Ratio / 0.45);
      const centScore = Math.max(0, 1 - Math.abs(meanCentroid - this.profile.centroid) / 0.25);
      const zcrScore = Math.max(0, 1 - Math.abs(meanZcr - this.profile.zcr) / 0.15);
      const score = f0Score * 0.55 + centScore * 0.25 + zcrScore * 0.20;

      if (score > 0.70) this._blend(medF0, meanCentroid, meanZcr, 0.12);
      return { score, pitchHz: medF0, enrolled: true };
    }

    _blend(f0, centroid, zcr, k) {
      if (!this.profile) return;
      this.profile.f0 = this.profile.f0 * (1 - k) + f0 * k;
      this.profile.centroid = this.profile.centroid * (1 - k) + centroid * k;
      this.profile.zcr = this.profile.zcr * (1 - k) + zcr * k;
      this.profile.turns++;
    }
  }

  function encodeWavMono16(chunks, sampleRate = 16000) {
    let total = 0;
    for (const c of chunks) total += c.length;
    const clamped = Math.min(total, sampleRate * 30);
    const buffer = new ArrayBuffer(44 + clamped * 2);
    const view = new DataView(buffer);
    const writeStr = (offset, s) => { for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i)); };
    writeStr(0, 'RIFF');
    view.setUint32(4, 36 + clamped * 2, true);
    writeStr(8, 'WAVE');
    writeStr(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, 'data');
    view.setUint32(40, clamped * 2, true);

    let offset = 44;
    let written = 0;
    for (const c of chunks) {
      for (let i = 0; i < c.length && written < clamped; i++, written++, offset += 2) {
        const s = Math.max(-1, Math.min(1, c[i]));
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      }
    }
    const bytes = new Uint8Array(buffer);
    let bin = '';
    const step = 0x4000;
    for (let i = 0; i < bytes.length; i += step) {
      bin += String.fromCharCode(...bytes.subarray(i, Math.min(i + step, bytes.length)));
    }
    return btoa(bin);
  }

  class VoiceTurnSession {
    constructor(opts = {}) {
      this.sampleRate = opts.sampleRate ?? 16000;
      this.preRollLimit = opts.preRollFrames ?? 8; // ~480-512ms pre-roll
      this.gate = new TurnGate({
        threshold: opts.threshold ?? 0.018,
        strictInterrupt: opts.strictInterrupt ?? true,
      });
      this.speaker = new SpeakerGuard(opts.lockToMyVoice ?? true);
      this.preRoll = [];
      this.utterance = [];
    }

    configure(patch = {}) {
      if (patch.threshold !== undefined || patch.strictInterrupt !== undefined || patch.minSpeechMs !== undefined) {
        this.gate.configure(patch);
      }
      if (patch.lockToMyVoice !== undefined) this.speaker.setEnabled(patch.lockToMyVoice);
    }

    setJarvisSpeaking(v) {
      this.gate.setTutorSpeaking(v);
    }

    pushPcm(pcm, dtMs, nowMs = Date.now()) {
      const level = rmsLevel(pcm);
      const copy = new Float32Array(pcm);
      if (!this.gate.isSpeaking) {
        this.preRoll.push(copy);
        if (this.preRoll.length > this.preRollLimit) this.preRoll.shift();
      } else {
        this.utterance.push(copy);
      }

      let ev = this.gate.push(copy, level, dtMs, nowMs);
      if (this.gate.isSpeaking) {
        let totalSamples = 0;
        for (const c of this.utterance) totalSamples += c.length;
        if (totalSamples >= this.sampleRate * 30) {
          ev = this.gate.forceSpeechEnd() || { kind: 'speech-end' };
        }
      }
      if (ev?.kind === 'disturbance') {
        return { kind: 'disturbance', reason: ev.reason, level };
      }
      if (ev?.kind === 'speech-start') {
        this.utterance = [...this.preRoll];
        this.preRoll = [];
        this.speaker.push(copy, this.sampleRate, level);
        return { kind: 'speech-start', confidence: ev.confidence, level };
      }
      if (ev?.kind === 'speech-end') {
        const who = this.speaker.endTurn();
        const chunks = this.utterance;
        this.utterance = [];
        if (who.score < 0.45) {
          return { kind: 'filtered-speaker', score: who.score, speaker: this.speaker.status(), level };
        }
        return {
          kind: 'utterance-ready',
          wavBase64: encodeWavMono16(chunks, this.sampleRate),
          sampleCount: chunks.reduce((s, c) => s + c.length, 0),
          speaker: this.speaker.status(),
          score: who.score,
          level,
        };
      }
      if (this.gate.isSpeaking) {
        this.speaker.push(copy, this.sampleRate, level);
      }
      return { kind: this.gate.isSpeaking ? 'speaking' : 'idle', level };
    }

    reset() {
      this.gate.reset();
      this.preRoll = [];
      this.utterance = [];
    }
  }

  function createSfx() {
    let ctx = null;
    let master = null;
    let muted = false;
    const ensure = () => {
      if (typeof window === 'undefined' || !window.AudioContext) return null;
      if (!ctx) {
        try {
          ctx = new AudioContext();
          master = ctx.createGain();
          master.gain.value = 0.28;
          master.connect(ctx.destination);
        } catch {
          return null;
        }
      }
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      return ctx;
    };
    const tone = (freq, dur, delay = 0, gain = 0.45, to = 0) => {
      const c = ensure();
      if (!c || !master || muted) return;
      const t0 = c.currentTime + delay;
      const osc = c.createOscillator();
      const env = c.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, t0);
      if (to) osc.frequency.exponentialRampToValueAtTime(Math.max(1, to), t0 + dur);
      env.gain.setValueAtTime(0.0001, t0);
      env.gain.exponentialRampToValueAtTime(gain, t0 + 0.012);
      env.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(env).connect(master);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    };
    return {
      setMuted: (v) => { muted = !!v; },
      micOn: () => { tone(523, 0.12, 0, 0.42); tone(659, 0.14, 0.08, 0.42); },
      micOff: () => { tone(494, 0.11, 0, 0.36); tone(392, 0.16, 0.07, 0.36); },
      filtered: () => { tone(260, 0.06, 0, 0.18, 190); },
      action: () => { tone(784, 0.09, 0, 0.32); tone(988, 0.12, 0.07, 0.34); },
    };
  }

  return {
    BAND_COUNT,
    AudioBus: new AudioBusImpl(),
    Vad,
    TurnGate,
    SpeakerGuard,
    VoiceTurnSession,
    Sfx: createSfx(),
    rmsLevel,
    zeroCrossingRate,
    spectralCentroid,
    detectPitch,
    encodeWavMono16,
  };
}));
