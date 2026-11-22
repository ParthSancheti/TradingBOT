const test = require('node:test');
const assert = require('node:assert/strict');
const {
  AudioBus,
  BAND_COUNT,
  TurnGate,
  SpeakerGuard,
  VoiceTurnSession,
  detectPitch,
  encodeWavMono16,
} = require('./renderer/voice-filter.js');

function sineFrame(freqHz, amp, len = 1024, sampleRate = 16000) {
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = amp * Math.sin((2 * Math.PI * freqHz * i) / sampleRate);
  }
  return out;
}

function noiseFrame(amp, len = 1024) {
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = (i % 2 === 0 ? 1 : -1) * amp;
  }
  return out;
}

test('TurnGate rejects sudden clatter/impact while accepting ramp-in voiced speech', () => {
  const gate = new TurnGate({ threshold: 0.018, minSpeechMs: 320 });
  const impact = sineFrame(220, 0.25);
  // Instant impulse on 1st loud frame has zero ramp-up attack frames -> disturbance
  let events = [];
  for (let i = 0; i < 6; i++) {
    const ev = gate.push(impact, 0.25 * Math.sin(Math.PI * 0.5), 64, 1000 + i * 64);
    if (ev) events.push(ev);
  }
  assert.ok(events.some((e) => e.kind === 'disturbance'), 'Expected impact disturbance rejection');

  // High-ZCR clatter -> rejected
  gate.reset();
  const hiss = noiseFrame(0.08);
  const res = [];
  for (const amp of [0.02, 0.04, 0.07, 0.08, 0.08, 0.08]) {
    const ev = gate.push(hiss, amp, 64, 3000 + res.length * 64);
    if (ev) res.push(ev);
  }
  assert.ok(res.some((e) => e.kind === 'disturbance' && e.reason.includes('unvoiced')));

  // Gradual attack + voiced vowel 150 Hz -> accepted
  gate.reset();
  const vowel = sineFrame(150, 0.09);
  const good = [];
  const levels = [0.02, 0.045, 0.07, 0.09, 0.09, 0.09];
  for (let i = 0; i < levels.length; i++) {
    const ev = gate.push(vowel, levels[i], 64, 6000 + i * 64);
    if (ev) good.push(ev);
  }
  assert.ok(good.some((e) => e.kind === 'speech-start' && e.confidence > 0.5));
});

test('SpeakerGuard enrolls primary voice and filters vastly different pitch speakers', () => {
  const guard = new SpeakerGuard(true);
  const lowMale = sineFrame(120, 0.15);
  const highChild = sineFrame(320, 0.15);
  assert.ok(Math.abs(detectPitch(lowMale, 16000) - 120) < 6);
  assert.ok(Math.abs(detectPitch(highChild, 16000) - 320) < 12);

  // Enroll primary speaker (3 turns at ~120 Hz)
  for (let t = 0; t < 3; t++) {
    for (let f = 0; f < 4; f++) guard.push(lowMale, 16000, 0.12);
    const out = guard.endTurn();
    assert.equal(out.score, 1);
  }
  assert.equal(guard.status().enrolled, true);

  // Same speaker verified high score
  for (let f = 0; f < 4; f++) guard.push(lowMale, 16000, 0.12);
  assert.ok(guard.endTurn().score >= 0.75);

  // Different speaker (320 Hz + brighter noise) rejected below 0.45
  for (let f = 0; f < 4; f++) guard.push(highChild, 16000, 0.12);
  const intruder = guard.endTurn();
  assert.ok(intruder.score < 0.45, `Expected low match score, got ${intruder.score}`);
});

test('VoiceTurnSession preserves pre-roll frames and emits valid RIFF WAV on turn end', () => {
  const session = new VoiceTurnSession({ threshold: 0.015, lockToMyVoice: false });
  const vowel = sineFrame(165, 0.10, 1024);
  let t = 1000;
  // quiet pre-roll
  session.pushPcm(sineFrame(165, 0.004, 1024), 64, t += 64);
  session.pushPcm(sineFrame(165, 0.005, 1024), 64, t += 64);
  // ramp in -> speech-start
  let started = false;
  for (const frame of [sineFrame(165, 0.025), sineFrame(165, 0.05), sineFrame(165, 0.08), vowel, vowel, vowel]) {
    const out = session.pushPcm(frame, 64, t += 64);
    if (out.kind === 'speech-start') started = true;
  }
  assert.equal(started, true);
  // silence hangover -> utterance-ready
  let result = null;
  const silence = new Float32Array(1024);
  for (let i = 0; i < 14; i++) {
    const out = session.pushPcm(silence, 64, t += 64);
    if (out.kind === 'utterance-ready') result = out;
  }
  assert.ok(result && result.wavBase64.length > 100);
  const decoded = Buffer.from(result.wavBase64, 'base64');
  assert.equal(decoded.toString('ascii', 0, 4), 'RIFF');
  assert.equal(decoded.toString('ascii', 8, 12), 'WAVE');

  const freq = new Uint8Array(128).fill(180);
  AudioBus.publish(freq, 0.7, 'user');
  assert.equal(AudioBus.bands.length, BAND_COUNT);
  assert.ok(AudioBus.level > 0.3);
  AudioBus.reset();
});
