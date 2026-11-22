'use strict';

(() => {
  const vf = window.JarvisVoiceFilter || {};
  const AudioBus = vf.AudioBus;
  const VoiceTurnSession = vf.VoiceTurnSession;
  const Sfx = vf.Sfx;

  const dock = $('jarvis-dock');
  const studio = $('jarvis-studio');
  const core = $('jarvis-core');
  const orbWrap = $('studio-orb-wrap');
  const historyPanel = $('dock-history');
  const studioLog = $('studio-log');
  const composer = $('dock-question');
  const sendButton = $('dock-form').querySelector('button');
  const liveButton = $('live-connect');
  const micButton = $('live-mic');
  const studioMicBtn = $('studio-mic-btn');
  const studioSpeakerBtn = $('studio-speaker-btn');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const agentLabel = () => JarvisApp.agentName || 'Jarvis';

  const historyLabels = new Map();
  const historyEntries = new Map();
  const chatDrafts = new Map();
  const chatIntro = historyPanel.querySelector('.dock-intro').cloneNode(true);

  const CHATS_CACHE_KEY = 'jarvis-chats-cache-v1';
  const ACTIVE_CHAT_KEY = 'jarvis-active-chat';

  let currentChat = crypto.randomUUID();
  let userSelectedNewChat = false;
  const transcripts = new Map();
  let historyLoading = false;
  let historySynced = null;
  let historyCheckedAt = 0;
  let opener = null;
  let pendingChat = null;
  let lastReplyModel = '';
  let retry = null;

  // Dual Voice Engine State ("custom" = Neural Studio + TurnGate + SpeakerGuard; "live" = Gemini Live WebSocket)
  let voiceEngine = localStorage.getItem('jarvis-voice-engine') || 'custom';
  let alwaysMicInStudio = localStorage.getItem('jarvis-always-mic') !== 'false';
  let lockToMyVoice = localStorage.getItem('jarvis-speaker-lock') !== 'false';
  let bargeInEnabled = localStorage.getItem('jarvis-barge-in') !== 'false';
  let vadThreshold = Number(localStorage.getItem('jarvis-vad-threshold') || '0.018');
  let selectedVoiceId = localStorage.getItem('jarvis-tts-voice') || 'default';
  let selectedLiveVoice = localStorage.getItem('jarvis-live-voice') || 'Charon';
  let selectedSpeechRate = Number(localStorage.getItem('jarvis-speech-rate') || '1');
  let speakerMuted = false;
  let voiceArmed = false;
  let voiceStopping = false;

  // Gemini Live & Streaming Audio State
  let liveConnected = false;
  let liveConnecting = false;
  let liveRecovering = false;
  let liveStopping = false;
  let liveModel = '';
  let liveGeneration = 0;
  let liveUserCaption = '';
  let liveJarvisCaption = '';
  let activeVoiceReqId = null;
  let activeVoiceGen = 0;
  let activeVoiceHeard = '';
  let activeVoiceAnswer = '';
  let audioGeneration = 0;
  let pendingDecodes = 0;
  let audioDecodeQueue = Promise.resolve();
  let turnStartedAt = 0;

  // Audio Graph & Custom Filtration Pipeline
  let micGeneration = 0;
  let micStarting = false;
  let micStream = null;
  let micContext = null;
  let micNode = null;
  let micAnalyser = null;
  let micInput = null;
  let speaker = null;
  let speakerAnalyser = null;
  let audioUntil = 0;
  let liveMicResumeAt = 0;
  let audioNodes = [];
  let waveFrame = 0;
  let waveLevel = 0;
  let studioBgFrame = 0;
  let studioWaveFrame = 0;
  let studioState = 'IDLE';
  let filterToastTimer = 0;
  let turnBusy = false;

  const waveBars = [...core.querySelectorAll('.core-wave i')];
  const waveSamples = new Float32Array(256);
  const micFreqBuf = new Uint8Array(128);
  const spkFreqBuf = new Uint8Array(128);

  const filterSession = VoiceTurnSession ? new VoiceTurnSession({
    threshold: vadThreshold,
    strictInterrupt: true,
    lockToMyVoice,
  }) : null;

  function showAssistantError(message = '') {
    const element = $('jarvis-error');
    element.hidden = !message;
    element.textContent = message;
  }

  function showFilterNotice(text) {
    const el = $('studio-filter-pill');
    if (!el) return;
    clearTimeout(filterToastTimer);
    el.textContent = text;
    el.hidden = false;
    Sfx?.filtered?.();
    filterToastTimer = setTimeout(() => { el.hidden = true; }, 2200);
  }

  function setStudioVisualState(state, customLabel) {
    studioState = state;
    if (orbWrap) orbWrap.dataset.state = state;
    const labels = {
      IDLE: 'Ready',
      LISTENING: alwaysMicInStudio && !studio.hidden ? 'Always-On Listening' : 'Listening',
      USER_SPEAKING: 'Hearing You',
      PROCESSING: 'Thinking…',
      JARVIS_SPEAKING: `${agentLabel()} Speaking`,
      ERROR: 'Voice Attention',
    };
    const lbl = customLabel || labels[state] || 'Ready';
    if ($('studio-turn-label')) $('studio-turn-label').textContent = lbl;
    const lockBadge = $('studio-lock-badge');
    if (lockBadge && filterSession) {
      const st = filterSession.speaker.status();
      if (!st.enabled) {
        lockBadge.textContent = 'Google Voice · Open Mic';
        lockBadge.dataset.locked = 'false';
      } else if (st.enrolled) {
        lockBadge.textContent = `Google Voice · Locked (${st.pitchHz} Hz)`;
        lockBadge.dataset.locked = 'true';
      } else {
        lockBadge.textContent = 'Google Voice · Hindlish';
        lockBadge.dataset.locked = 'false';
      }
      if ($('speaker-lock-hint')) {
        $('speaker-lock-hint').textContent = st.enrolled
          ? `Locked to your ${st.pitchHz} Hz pitch signature locally. Other voices are ignored.`
          : `Learns your voice automatically over ${st.required} turns (${st.turns}/${st.required} done).`;
      }
    }
  }

  function phase(name, label) {
    core.dataset.phase = name;
    if ($('live-state').textContent !== label) $('live-state').textContent = label;
    const map = {
      ready: 'IDLE',
      connecting: 'PROCESSING',
      listening: 'LISTENING',
      thinking: 'PROCESSING',
      speaking: 'JARVIS_SPEAKING',
      saving: 'PROCESSING',
      error: 'ERROR',
      connected: 'IDLE',
    };
    setStudioVisualState(map[name] || 'IDLE', label);
  }

  function modelLabel() {
    const modeTag = voiceEngine === 'custom' ? 'Google Voice' : 'Gemini Live';
    const m = liveModel || pendingChat?.model || lastReplyModel || JarvisApp.state?.gemini?.model || 'Gemini';
    $('jarvis-model').textContent = `${m} · ${modeTag}`;
  }

  function chatSwitchBlocked() {
    return !!pendingChat || turnBusy || liveConnected || liveConnecting || liveStopping;
  }

  function isVoiceActive() {
    return voiceEngine === 'custom' ? (voiceArmed || !!micStream) : (liveConnected || liveConnecting);
  }

  function syncLiveControls() {
    const custom = voiceEngine === 'custom';
    JarvisApp.liveBusy = !custom && (liveConnected || liveConnecting || liveStopping);

    liveButton.hidden = custom;
    liveButton.textContent = liveStopping ? 'Stopping…' : liveConnected ? 'Stop Live' : liveConnecting ? 'Cancel' : 'Start Live';
    liveButton.disabled = liveStopping || voiceStopping || !!pendingChat;

    micButton.disabled = custom ? micStarting : (!liveConnected || micStarting);
    micButton.textContent = micStarting ? 'Opening mic…' : micStream ? 'Listening…' : (custom ? 'Speak' : 'Mic off');
    micButton.setAttribute('aria-pressed', String(!!micStream));

    if (studioMicBtn) {
      studioMicBtn.setAttribute('aria-pressed', String(!!micStream));
      studioMicBtn.disabled = micStarting;
    }
    if (studioSpeakerBtn) {
      studioSpeakerBtn.setAttribute('aria-pressed', String(speakerMuted));
    }

    sendButton.disabled = !!pendingChat || turnBusy || liveConnecting || liveRecovering || liveStopping;
    $('jarvis-new-chat').disabled = chatSwitchBlocked();

    const launcher = $('open-jarvis');
    const active = isVoiceActive();
    launcher.dataset.live = active ? 'true' : 'false';
    const labelEl = launcher.querySelector('span');
    const txt = liveRecovering ? 'Reconnecting'
      : active ? (micStream ? 'Voice · Listening' : 'Voice Ready')
      : liveConnecting ? 'Connecting' : agentLabel();
    if (labelEl) labelEl.textContent = txt;
    else launcher.textContent = txt;

    $('live-detail').textContent = custom
      ? (micStream ? 'Filtered Mic Active' : 'Google Voice · Hindlish')
      : ($('live-detail').textContent || 'Voice is off');

    modelLabel();
  }

  function voiceIdle() {
    if (voiceEngine === 'custom') {
      if (micStream) {
        phase('listening', studio.hidden ? 'Listening (Filtered)' : 'Always-On Listening');
      } else if (voiceArmed) {
        phase('ready', 'Voice Armed');
      } else {
        phase('ready', 'Ready');
      }
      return;
    }
    if (liveConnected && !liveRecovering) {
      phase(micStream ? 'listening' : 'connected', micStream ? 'Listening' : 'Live connected');
    } else if (!liveRecovering && !pendingChat && !liveConnecting && !liveStopping) {
      phase('ready', 'Ready');
    }
  }

  function updateHudTelemetry(latencyMs = null) {
    const left = $('hud-telemetry-left');
    const right = $('hud-telemetry-right');
    if (left) {
      const tag = selectedVoiceId === 'default'
        ? 'HINDLISH'
        : selectedVoiceId.replace(/^(google:|gtts:)/i, '').split('-')[0].toUpperCase();
      const modeName = voiceEngine === 'live'
        ? `GEMINI LIVE · ${selectedLiveVoice.toUpperCase()}`
        : `GOOGLE VOICE · ${tag}`;
      left.textContent = latencyMs ? `${modeName} · ${(latencyMs / 1000).toFixed(2)}S` : modeName;
    }
    if (right && filterSession) {
      const st = filterSession.speaker.status();
      right.textContent = st.enabled
        ? (st.enrolled ? `VOICEPRINT · ${st.pitchHz} HZ` : 'ADAPTIVE FILTER · ACTIVE')
        : 'OPEN MIC · ACTIVE';
    }
  }

  // =========================================================================
  // Hardware-2D 3D Holographic Particle Sphere HUD (Jarvis Space Canvas)
  // =========================================================================
  const SPHERE_COUNT = 1150;
  const DUST_COUNT = 340;
  const spherePts = (() => {
    const arr = [];
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < SPHERE_COUNT; i++) {
      const y = 1 - (i / (SPHERE_COUNT - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = goldenAngle * i;
      arr.push({
        x: Math.cos(theta) * r,
        y,
        z: Math.sin(theta) * r,
        band: i % (vf.BAND_COUNT || 28),
        phase: (i * 0.37) % (Math.PI * 2),
      });
    }
    return arr;
  })();
  const dustPts = Array.from({ length: DUST_COUNT }, (_, i) => {
    const ang = (i * 2.39996) % (Math.PI * 2);
    const radial = 1.28 + ((i * 73) % 100) / 38;
    const tilt = (((i * 19) % 100) / 100 - 0.5) * 1.45;
    return {
      ang,
      radial,
      tilt,
      speed: 0.04 + ((i * 13) % 25) * 0.004,
      size: 0.75 + ((i * 7) % 10) * 0.14,
      alpha: 0.16 + ((i * 11) % 30) * 0.014,
    };
  });

  function startStudioVisuals() {
    updateHudTelemetry();
    if (studio.hidden) return;
    startStudioBgLoop();
    if (!reducedMotion.matches) startStudioWaveLoop();
  }

  function startStudioBgLoop() {
    if (studioBgFrame) return;
    const canvas = $('studio-wave-bg');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let energy = 0;
    let rotY = 0;
    let rotX = 0.24;
    let sweep = 0;
    let last = performance.now();

    const drawFrame = (now) => {
      if (studio.hidden) {
        studioBgFrame = 0;
        return;
      }
      if (!reducedMotion.matches) {
        studioBgFrame = requestAnimationFrame(drawFrame);
      } else {
        studioBgFrame = 0;
      }
      const dt = Math.min(0.05, Math.max(0.001, (now - last) / 1000));
      last = now;

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const cw = canvas.clientWidth || window.innerWidth;
      const ch = canvas.clientHeight || window.innerHeight;
      if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
        canvas.width = Math.round(cw * dpr);
        canvas.height = Math.round(ch * dpr);
      }

      const stageRect = studio.getBoundingClientRect();
      const orbRect = orbWrap ? orbWrap.getBoundingClientRect() : null;
      const cx = cw * 0.5;
      const cy = orbRect && orbRect.height > 0
        ? (orbRect.top - stageRect.top + orbRect.height * 0.5)
        : ch * 0.41;
      const baseRadius = orbRect && orbRect.height > 0
        ? orbRect.height * 0.45
        : Math.min(cw, ch) * 0.17;

      const voiceLvl = AudioBus ? AudioBus.level : waveLevel;
      energy += (voiceLvl - energy) * 0.16;

      const spinRate = studioState === 'PROCESSING' ? 1.15
        : studioState === 'JARVIS_SPEAKING' ? 0.58 + energy * 0.55
        : studioState === 'USER_SPEAKING' ? 0.48 + energy * 0.4
        : 0.22;
      rotY += dt * spinRate;
      rotX = 0.24 + Math.sin(now * 0.00055) * 0.10;
      sweep += dt * (studioState === 'PROCESSING' ? 3.2 : 1.4);

      if (orbWrap) {
        orbWrap.style.setProperty('--orb-scale', (1 + energy * 0.12).toFixed(4));
      }

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cw, ch);

      // Volumetric core radial aura (adapts to Light & Dark theme cleanly)
      const isDark = document.documentElement.dataset.theme === 'dark';
      const coreGlow = ctx.createRadialGradient(cx, cy, baseRadius * 0.05, cx, cy, baseRadius * 1.65);
      const isUser = AudioBus?.source === 'user' || studioState === 'USER_SPEAKING';
      const isProc = studioState === 'PROCESSING';
      const isErr = studioState === 'ERROR';
      const cMain = isErr
        ? (isDark ? '244, 63, 94' : '225, 29, 72')
        : isUser
          ? (isDark ? '45, 212, 191' : '5, 150, 105')
          : isProc
            ? (isDark ? '56, 189, 248' : '37, 99, 235')
            : (isDark ? '34, 211, 238' : '3, 105, 161');
      const cCore = isErr
        ? (isDark ? '254, 205, 211' : '244, 63, 94')
        : (isDark ? '165, 243, 252' : '14, 165, 233');
      coreGlow.addColorStop(0, `rgba(${cCore}, ${(isDark ? 0.24 + energy * 0.35 : 0.16 + energy * 0.22).toFixed(3)})`);
      coreGlow.addColorStop(0.28, `rgba(${cMain}, ${(isDark ? 0.22 + energy * 0.25 : 0.14 + energy * 0.18).toFixed(3)})`);
      coreGlow.addColorStop(0.68, `rgba(${cMain}, ${isDark ? 0.05 : 0.04})`);
      coreGlow.addColorStop(1, 'rgba(0, 0, 0, 0)');
      ctx.fillStyle = coreGlow;
      ctx.fillRect(0, 0, cw, ch);

      // Subtle holographic equatorial & orbital reticle ellipses
      ctx.save();
      ctx.translate(cx, cy);
      ctx.strokeStyle = `rgba(${cMain}, ${(isDark ? 0.18 + energy * 0.22 : 0.22 + energy * 0.20).toFixed(3)})`;
      ctx.lineWidth = isDark ? 1 : 1.15;
      for (let r = 0; r < 3; r++) {
        ctx.save();
        ctx.rotate((r * Math.PI) / 3 + rotY * (r === 1 ? -0.35 : 0.25));
        ctx.beginPath();
        ctx.ellipse(0, 0, baseRadius * (1.06 + energy * 0.09), baseRadius * (0.34 + r * 0.12), 0, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }
      ctx.restore();

      const sinY = Math.sin(rotY);
      const cosY = Math.cos(rotY);
      const sinX = Math.sin(rotX);
      const cosX = Math.cos(rotX);
      const focal = baseRadius * 3.2;

      // Ambient cosmos orbital stardust field around globe
      for (let i = 0; i < DUST_COUNT; i++) {
        const d = dustPts[i];
        const a = d.ang + rotY * d.speed;
        const rOrbit = baseRadius * (d.radial + energy * 0.12);
        const dx = Math.cos(a) * rOrbit;
        const dz = Math.sin(a) * rOrbit;
        const dy = Math.sin(a * 0.8 + i) * baseRadius * d.tilt;
        const pScale = focal / (focal - dz + baseRadius * 0.8);
        if (pScale <= 0.1) continue;
        const px = cx + dx * pScale;
        const py = cy + dy * pScale;
        ctx.fillStyle = `rgba(${cMain}, ${(d.alpha * (isDark ? 1 : 0.92) * Math.min(1, pScale)).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(px, py, d.size * pScale, 0, Math.PI * 2);
        ctx.fill();
      }

      // 3D Fibonacci Holographic Particle Sphere (audio-reactive along surface normals)
      const bands = AudioBus ? AudioBus.bands : null;
      ctx.globalCompositeOperation = isDark ? 'lighter' : 'source-over';
      for (let i = 0; i < SPHERE_COUNT; i++) {
        const pt = spherePts[i];
        const x1 = pt.x * cosY - pt.z * sinY;
        const z1 = pt.x * sinY + pt.z * cosY;
        const y2 = pt.y * cosX - z1 * sinX;
        const z2 = pt.y * sinX + z1 * cosX;

        const bandAmp = bands ? (bands[pt.band] || 0) : 0;
        const wave = Math.sin(pt.y * 5.5 + pt.phase + sweep) * 0.035;
        const procWave = isProc ? Math.max(0, Math.sin(pt.y * 6 - sweep * 1.4)) * 0.14 : 0;
        const rMod = baseRadius * (1 + wave + procWave + bandAmp * (0.32 + energy * 0.35));

        const depthNorm = (z2 + 1) * 0.5;
        const scale = focal / (focal - z2 * baseRadius);
        const sx = cx + x1 * rMod * scale;
        const sy = cy + y2 * rMod * scale;
        const ptSize = (0.85 + depthNorm * 1.55 + bandAmp * 1.4) * scale;
        const alpha = (0.14 + depthNorm * 0.70 + bandAmp * 0.30);

        ctx.fillStyle = bandAmp > 0.45 && depthNorm > 0.55
          ? `rgba(${cCore}, ${Math.min(0.95, alpha).toFixed(3)})`
          : `rgba(${cMain}, ${Math.min(0.88, alpha).toFixed(3)})`;
        ctx.fillRect(sx - ptSize * 0.5, sy - ptSize * 0.5, ptSize, ptSize);
      }

      // Inner core nucleus highlight
      const nucleus = ctx.createRadialGradient(cx, cy, 0, cx, cy, baseRadius * 0.32);
      nucleus.addColorStop(0, `rgba(${isDark ? '255, 255, 255' : '255, 255, 255'}, ${(isDark ? 0.42 + energy * 0.42 : 0.72 + energy * 0.25).toFixed(3)})`);
      nucleus.addColorStop(0.45, `rgba(${cCore}, ${(0.22 + energy * 0.25).toFixed(3)})`);
      nucleus.addColorStop(1, 'rgba(34, 211, 238, 0)');
      ctx.fillStyle = nucleus;
      ctx.beginPath();
      ctx.arc(cx, cy, baseRadius * 0.32, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalCompositeOperation = 'source-over';
    };

    studioBgFrame = requestAnimationFrame(drawFrame);
  }

  function startStudioWaveLoop() {
    if (studioWaveFrame) return;
    const canvas = $('studio-voice-wave');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const draw = (t) => {
      if (studio.hidden || reducedMotion.matches) {
        studioWaveFrame = 0;
        return;
      }
      studioWaveFrame = requestAnimationFrame(draw);

      if (AudioBus) {
        if (audioNodes.length && speakerAnalyser) {
          speakerAnalyser.getByteFrequencyData(spkFreqBuf);
          speakerAnalyser.getFloatTimeDomainData(waveSamples);
          let p = 0;
          for (let i = 0; i < waveSamples.length; i++) p += waveSamples[i] * waveSamples[i];
          AudioBus.publish(spkFreqBuf, Math.min(1, Math.sqrt(p / waveSamples.length) * 6), 'model');
        } else if (micAnalyser && micStream) {
          micAnalyser.getByteFrequencyData(micFreqBuf);
          micAnalyser.getFloatTimeDomainData(waveSamples);
          let p = 0;
          for (let i = 0; i < waveSamples.length; i++) p += waveSamples[i] * waveSamples[i];
          AudioBus.publish(micFreqBuf, Math.min(1, Math.sqrt(p / waveSamples.length) * 6), 'user');
        } else {
          AudioBus.publish(null, 0, null);
        }
      }

      const w = 440;
      const h = 76;
      ctx.clearRect(0, 0, w, h);
      const count = vf.BAND_COUNT || 28;
      const mid = h / 2;
      const gap = 5;
      const barW = Math.max(4, (w - gap * (count - 1)) / count);
      const maxRise = h / 2 - 6;

      const isDark = document.documentElement.dataset.theme === 'dark';
      const isModel = AudioBus?.source === 'model';
      const live = isVoiceActive() && (AudioBus?.source !== null);
      const color = isDark
        ? (isModel ? '#22d3ee' : AudioBus?.source === 'user' ? '#34d399' : '#38bdf8')
        : (isModel ? '#0284c7' : AudioBus?.source === 'user' ? '#059669' : '#0369a1');

      ctx.save();
      if (live) {
        ctx.shadowColor = color;
        ctx.shadowBlur = 14;
      }
      for (let i = 0; i < count; i++) {
        const x = i * (barW + gap);
        let amp;
        if (studioState === 'PROCESSING') {
          amp = 0.14 + Math.max(0, Math.sin(i * 0.55 - t * 0.006)) * 0.25;
        } else if (!live) {
          amp = 0.06 + Math.sin(i * 0.4 + t * 0.0014) * 0.025;
        } else {
          const edge = Math.sin((i / (count - 1)) * Math.PI);
          amp = Math.max(0.06, (AudioBus.bands[i] || 0) * (0.45 + edge * 0.55));
        }
        const rise = Math.max(3, amp * maxRise);
        const y = !live ? mid - rise : (isModel ? mid - rise : mid);
        const barH = !live ? rise * 2 : rise;
        ctx.fillStyle = color;
        ctx.globalAlpha = live ? 0.62 + amp * 0.38 : 0.34;
        ctx.beginPath();
        ctx.roundRect(x, y, barW, barH, barW / 2);
        ctx.fill();
      }
      ctx.restore();
    };
    studioWaveFrame = requestAnimationFrame(draw);
  }

  // =========================================================================
  // Conversation History, Actions & Studio Sync
  // =========================================================================
  function openJarvis(event) {
    setStudioOpen(false);
    if (dock.hidden) opener = event?.currentTarget || document.activeElement;
    setSplitDock(true);
    if (micStream || audioNodes.length) startWave();
    $('jarvis-context').textContent = 'Viewing ' + (JarvisApp.labels[JarvisApp.activeView] || JarvisApp.activeView);
    modelLabel();
    ($('jarvis-history').hidden ? composer : $('jarvis-history-search')).focus();
    if (Date.now() - historyCheckedAt > 60000) loadAssistantHistory();
  }

  function saveActiveChat(id) {
    try {
      if (id) localStorage.setItem(ACTIVE_CHAT_KEY, id);
    } catch {}
  }

  function saveChatsCache() {
    try {
      const sorted = [...historyEntries.values()]
        .filter((t) => t && t.request_id)
        .sort((a, b) => (b.at || '').localeCompare(a.at || ''))
        .slice(0, 60)
        .map((t) => ({
          request_id: t.request_id,
          chat_id: t.chat_id,
          chat_title: t.chat_title,
          question: t.question,
          answer: t.answer,
          actions: t.actions,
          tool_runs: t.tool_runs,
          at: t.at,
          status: t.status,
          model: t.model,
        }));
      const raw = JSON.stringify(sorted);
      if (raw.length < 240000) localStorage.setItem(CHATS_CACHE_KEY, raw);
    } catch {}
  }

  function latestChatId() {
    let newest = null;
    for (const turn of historyEntries.values()) {
      if (!newest || (turn.at || '').localeCompare(newest.at || '') > 0) newest = turn;
    }
    return newest ? chatId(newest) : null;
  }

  function restoreChatsCache() {
    try {
      const raw = JSON.parse(localStorage.getItem(CHATS_CACHE_KEY) || '[]');
      if (Array.isArray(raw)) {
        for (const turn of raw) {
          if (turn && turn.request_id && !historyEntries.has(turn.request_id)) {
            historyEntries.set(turn.request_id, turn);
          }
        }
      }
      const savedId = localStorage.getItem(ACTIVE_CHAT_KEY) || '';
      const hasSaved = savedId && [...historyEntries.values()].some((t) => chatId(t) === savedId);
      const fallbackId = latestChatId();
      if (hasSaved) currentChat = savedId;
      else if (fallbackId) currentChat = fallbackId;
      if (historyEntries.size) renderChat();
    } catch {}
  }

  function applyTurnsToState(result) {
    const pending = new Set(result.pending_ids || []);
    for (const turn of result.turns || []) {
      const existing = historyEntries.get(turn.request_id);
      historyEntries.set(turn.request_id, { ...existing, ...turn, status: pending.has(turn.request_id) ? 'pending' : 'saved' });
    }
    historySynced = result.synced;
    historyCheckedAt = Date.now();
    if (!userSelectedNewChat && ![...historyEntries.values()].some((t) => chatId(t) === currentChat)) {
      const savedId = (() => { try { return localStorage.getItem(ACTIVE_CHAT_KEY) || ''; } catch { return ''; } })();
      const hasSaved = savedId && [...historyEntries.values()].some((t) => chatId(t) === savedId);
      const resumeId = hasSaved ? savedId : latestChatId();
      if (resumeId) currentChat = resumeId;
    }
    saveChatsCache();
    if (!chatSwitchBlocked()) renderChat();
  }
  JarvisApp.handlers.bootstrapHistory = applyTurnsToState;

  async function loadAssistantHistory(refresh = false) {
    if (historyLoading) return;
    historyLoading = true;
    $('jarvis-history-refresh').disabled = true;
    renderAssistantHistory();
    try {
      const result = await call('assistant/history', { refresh });
      applyTurnsToState(result);
    } catch {
      historySynced = false;
    } finally {
      historyCheckedAt = Date.now();
      historyLoading = false;
      $('jarvis-history-refresh').disabled = false;
      renderAssistantHistory();
    }
  }

  function setHistoryVisible(visible) {
    $('jarvis-history').hidden = !visible;
    historyPanel.hidden = visible;
    $('jarvis-chat-title').hidden = visible;
    $('dock-form').hidden = visible;
    const button = $('jarvis-history-toggle');
    const lbl = button.querySelector('span');
    if (lbl) lbl.textContent = visible ? 'Back' : 'Chats';
    else button.textContent = visible ? 'Back' : 'Chats';
    button.setAttribute('aria-expanded', String(visible));
    if (visible) {
      renderAssistantHistory();
      $('jarvis-history-search').focus();
      if (historySynced !== true || Date.now() - historyCheckedAt > 60000) loadAssistantHistory();
    } else {
      composer.focus();
    }
  }

  function chatId(turn) { return turn.chat_id || 'legacy'; }
  function chatTitle(turn) { return turn.chat_title || (chatId(turn) === 'legacy' ? 'Earlier chats' : turn.question); }

  function scrollConversation() {
    historyPanel.scrollTop = historyPanel.scrollHeight;
    if (studioLog) studioLog.scrollTop = studioLog.scrollHeight;
  }

  function appendActionPills(container, actions = [], toolRuns = []) {
    const items = [
      ...(toolRuns || []),
      ...(actions || []).filter((a) => a && a.action).map((a) => `UI → ${a.view || a.theme || a.jarvis_mode || a.action}`),
    ];
    if (!items.length) return;
    const row = document.createElement('div');
    row.className = 'action-pill-row';
    for (const txt of items.slice(0, 6)) {
      const pill = document.createElement('span');
      pill.className = 'action-pill';
      pill.textContent = '✓ ' + txt;
      row.append(pill);
    }
    container.append(row);
  }

  function addMessage(role, text, opts = {}) {
    historyPanel.querySelector('.dock-intro')?.remove();
    const article = document.createElement('article');
    article.className = 'dock-message ' + role;
    const who = document.createElement('small');
    who.textContent = role === 'user' ? 'You' : agentLabel();
    const body = document.createElement('div');
    if (role === 'assistant') renderAssistantText(body, text);
    else body.textContent = text;
    if (opts.actions || opts.toolRuns) appendActionPills(body, opts.actions, opts.toolRuns);
    article.append(who, body);
    historyPanel.append(article);

    if (studioLog) {
      const clone = article.cloneNode(true);
      studioLog.append(clone);
    }
    scrollConversation();
    return body;
  }

  function showStudioCaption(userText, jarvisText, actions = [], toolRuns = []) {
    const cap = $('studio-captions');
    if (!cap) return;
    cap.hidden = false;
    const uEl = $('studio-caption-user');
    const jEl = $('studio-caption-jarvis');
    const aEl = $('studio-caption-actions');
    if (userText) {
      uEl.textContent = '“' + userText + '”';
      uEl.hidden = false;
    } else {
      uEl.hidden = true;
    }
    renderAssistantText(jEl, jarvisText || '');
    aEl.replaceChildren();
    appendActionPills(aEl, actions, toolRuns);
    aEl.hidden = !aEl.childElementCount;
  }

  function updateHistoryLabel(label, status) {
    label.hidden = false;
    label.dataset.status = status;
    const history = {
      saved: 'Saved to Google Sheets',
      pending: 'History waiting for Sheets',
      unsaved: 'History queue full · reply is available in this session',
      failed: 'Reply interrupted',
    };
    label.textContent = (history[status] || 'History status unavailable') + (label.dataset.context || '');
  }

  function renderChat() {
    const turns = [...historyEntries.values()]
      .filter((turn) => chatId(turn) === currentChat)
      .sort((a, b) => (a.at || '').localeCompare(b.at || ''));
    $('jarvis-chat-title').textContent = turns.length ? chatTitle(turns[0]) : 'New chat';
    historyPanel.replaceChildren();
    if (studioLog) studioLog.replaceChildren();
    historyLabels.clear();
    if (!turns.length) historyPanel.append(chatIntro.cloneNode(true));
    for (const turn of turns) {
      addMessage('user', turn.question);
      const body = addMessage('assistant', turn.answer, { actions: turn.actions, toolRuns: turn.tool_runs });
      const label = document.createElement('small');
      label.className = 'reply-history';
      body.after(label);
      historyLabels.set(turn.request_id, label);
      updateHistoryLabel(label, turn.status);
    }
  }

  function selectChat(id) {
    if (chatSwitchBlocked()) {
      toast('Finish the current reply before switching chats.');
      return;
    }
    if (id !== currentChat) {
      if (composer.value) chatDrafts.set(currentChat, composer.value);
      else chatDrafts.delete(currentChat);
      currentChat = id;
      saveActiveChat(id);
      composer.value = chatDrafts.get(id) || '';
      retry = null;
      lastReplyModel = '';
      showAssistantError();
      phase('ready', 'Ready');
      modelLabel();
      renderChat();
    }
    setHistoryVisible(false);
  }

  function renderAssistantHistory() {
    if ($('jarvis-history').hidden) return;
    $('jarvis-history-status').textContent = historyLoading
      ? 'Checking Google Sheets…'
      : historySynced === false
        ? 'Sheets unavailable · showing chats available in this session'
        : 'Recent text chats · newest first';
    const list = $('jarvis-history-entries');
    list.replaceChildren();
    const search = $('jarvis-history-search').value.trim().toLowerCase();
    const turns = [...historyEntries.values()].sort((a, b) => (b.at || '').localeCompare(a.at || ''));
    const chats = new Map();
    for (const turn of turns) {
      const id = chatId(turn);
      if (!chats.has(id)) chats.set(id, []);
      chats.get(id).push(turn);
    }
    for (const [id, messages] of chats) {
      const latest = messages[0];
      const name = chatTitle(messages.at(-1));
      if (!name.toLowerCase().includes(search) && !messages.some((turn) => (turn.question + '\n' + turn.answer).toLowerCase().includes(search))) continue;
      const entry = document.createElement('button');
      entry.type = 'button';
      entry.className = 'jarvis-chat-row';
      if (id === currentChat) entry.setAttribute('aria-current', 'true');
      const icon = document.createElement('span');
      icon.className = 'jarvis-chat-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 4h12v9H9l-4 3v-3H4z" stroke-linejoin="round"/><path d="M7 7h6M7 10h4" stroke-linecap="round"/></svg>';
      const summary = document.createElement('span');
      const title = document.createElement('strong');
      title.textContent = name;
      const meta = document.createElement('small');
      const date = new Date(latest.at);
      const status = messages.some((t) => t.status === 'pending') ? 'Save pending' : latest.status === 'saved' ? 'Saved' : 'This session';
      meta.textContent = [Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(), status].filter(Boolean).join(' · ');
      summary.append(title, meta);
      entry.append(icon, summary);
      entry.onclick = () => selectChat(id);
      list.append(entry);
    }
    if (!list.childElementCount) {
      const empty = document.createElement('p');
      empty.textContent = search ? 'No chats match your search.' : historyLoading ? 'Loading saved conversations…' : 'Your text chats will appear here.';
      list.append(empty);
    }
  }

  async function submitTextPrompt(question, fromStudio = false) {
    if (!question || pendingChat || turnBusy || liveConnecting || liveRecovering || liveStopping) return;
    showAssistantError();
    if (voiceEngine === 'live' && liveConnected) {
      transcripts.clear();
      addMessage('user', question);
      composer.value = '';
      phase('thinking', 'Waiting for Live reply');
      try {
        await call('live/text', { text: question });
      } catch (error) {
        if (!composer.value && !fromStudio) composer.value = question;
        voiceIdle();
        showAssistantError(error.message);
      }
      return;
    }

    const request = (!fromStudio && retry?.question === question) ? retry : {
      question, id: crypto.randomUUID(), page: JarvisApp.activeView, chat: currentChat,
    };
    if (request !== retry) addMessage('user', question);
    retry = null;
    if (!fromStudio) composer.value = '';
    const oldLabel = historyLabels.get(request.id);
    const body = oldLabel?.previousElementSibling || addMessage('assistant', '');
    oldLabel?.remove();
    body.textContent = 'Thinking & checking workspace…';
    if (fromStudio) showStudioCaption(question, 'Thinking & checking workspace…');
    const response = { id: request.id, body, text: '', frame: 0 };
    lastReplyModel = '';
    const historyLabel = document.createElement('small');
    historyLabel.className = 'reply-history';
    historyLabel.hidden = true;
    response.body.after(historyLabel);
    historyLabels.set(request.id, historyLabel);
    response.body.dataset.waiting = 'true';
    pendingChat = response;
    const speakReply = !studio.hidden && !speakerMuted && voiceEngine === 'custom';
    activeVoiceReqId = speakReply ? request.id : null;
    const turnGen = audioGeneration;
    activeVoiceGen = turnGen;
    activeVoiceHeard = question;
    activeVoiceAnswer = '';
    turnStartedAt = performance.now();
    if (speakReply) setupSpeaker(24000).catch(() => {});
    phase('thinking', 'Thinking & executing tools…');
    syncLiveControls();
    try {
      const result = speakReply
        ? await call('voice/turn', {
          text: question, request_id: request.id, page: request.page,
          chat_id: request.chat, voice_id: selectedVoiceId, rate: selectedSpeechRate, speak: true,
        })
        : await call('assistant/ask', {
          question, request_id: request.id, page: request.page, chat_id: request.chat,
        });
      renderAssistantText(response.body, result.answer);
      if (result.actions || result.tool_runs) appendActionPills(response.body, result.actions, result.tool_runs);
      if (result.actions?.length) Sfx?.action?.();
      lastReplyModel = result.model || '';
      historyEntries.set(request.id, { ...result, status: historyLabel.dataset.status === 'saved' ? 'saved' : result.history_state });
      saveChatsCache();
      saveActiveChat(request.chat);
      $('jarvis-chat-title').textContent = chatTitle(result);
      historyLabel.dataset.context = result.context_partial ? ' · Limited Sheet context' : '';
      updateHistoryLabel(historyLabel, historyLabel.dataset.status === 'saved' ? 'saved' : result.history_state);
      if (!studio.hidden) showStudioCaption(question, result.answer, result.actions, result.tool_runs);
      if (speakReply) {
        if (result.streamed_audio) {
          updateHudTelemetry(Math.round(performance.now() - turnStartedAt));
        } else if (result.wav_b64) {
          updateHudTelemetry(Math.round(performance.now() - turnStartedAt));
          await playWavBase64(result.wav_b64, turnGen);
        } else if (result.answer && !audioNodes.length && pendingDecodes === 0) {
          speakLocalFallback(result.answer);
        }
      } else {
        phase('ready', 'Reply ready');
      }
    } catch (error) {
      if (!fromStudio) retry = request;
      if (!fromStudio && !composer.value) composer.value = question;
      if (response.text) renderAssistantText(response.body, response.text + '\n\n*Reply could not be confirmed in history.*');
      else response.body.textContent = 'Reply unavailable.';
      historyEntries.set(request.id, { request_id: request.id, chat_id: request.chat, question, answer: response.text || error.message, at: new Date().toISOString(), status: 'failed' });
      phase('error', 'Reply interrupted');
      showAssistantError(error.message);
      if (!studio.hidden) showStudioCaption(question, error.message);
    } finally {
      cancelAnimationFrame(response.frame);
      delete response.body.dataset.waiting;
      if (speakReply) activeVoiceReqId = null;
      pendingChat = null;
      syncLiveControls();
      renderAssistantHistory();
      scrollConversation();
    }
  }

  function sendAssistantQuestion(event) {
    event.preventDefault();
    return submitTextPrompt(composer.value.trim(), false);
  }

  function receiveAssistant(data) {
    if (data.stage === 'history_saved' || data.stage === 'history_pending') {
      const label = historyLabels.get(data.request_id);
      if (label) updateHistoryLabel(label, data.stage === 'history_saved' ? 'saved' : 'pending');
      const entry = historyEntries.get(data.request_id);
      if (entry) entry.status = data.stage === 'history_saved' ? 'saved' : 'pending';
      saveChatsCache();
      renderAssistantHistory();
      return;
    }
    const response = pendingChat;
    if (data.request_id !== response?.id) return;
    if (data.stage === 'tool') {
      phase('thinking', 'Using ' + (data.text || 'workspace tool'));
      response.body.textContent = 'Using ' + (data.text || 'workspace tool') + '…';
    } else if (data.stage === 'generating') {
      phase('thinking', 'Waiting for Gemini');
      response.body.textContent = 'Thinking…';
    } else if (data.stage === 'retrying') {
      response.model = data.text;
      modelLabel();
      phase('thinking', 'Retrying Gemini');
      response.body.textContent = 'Trying ' + data.text + '…';
    } else if (data.stage === 'saving') {
      phase('saving', 'Saving to Google Sheets');
    } else if (data.stage === 'chunk') {
      if (!response.text) phase('speaking', 'Writing reply');
      delete response.body.dataset.waiting;
      response.text = (response.text + data.text).slice(0, 8000);
      if (!response.frame) response.frame = requestAnimationFrame(() => {
        response.frame = 0;
        const nearEnd = historyPanel.scrollHeight - historyPanel.scrollTop - historyPanel.clientHeight < 80;
        renderAssistantText(response.body, response.text);
        if (nearEnd) scrollConversation();
      });
    }
  }

  // =========================================================================
  // WebAudio Gapless Streaming Playback + Barge-In Ownership
  // =========================================================================
  function bytesToBase64(bytes) {
    let bin = '';
    const step = 0x4000;
    for (let i = 0; i < bytes.length; i += step) {
      bin += String.fromCharCode(...bytes.subarray(i, Math.min(i + step, bytes.length)));
    }
    return btoa(bin);
  }

  function setupSpeaker(sampleRate = 24000) {
    if (!speaker) {
      speaker = new AudioContext({ sampleRate });
      speakerAnalyser = speaker.createAnalyser();
      speakerAnalyser.fftSize = waveSamples.length;
      speakerAnalyser.connect(speaker.destination);
    }
    return speaker.resume();
  }

  function clearAudio() {
    audioGeneration++;
    activeVoiceReqId = null;
    for (const node of audioNodes) {
      node.onended = null;
      try { node.stop(); } catch {}
      node.disconnect();
    }
    audioNodes = [];
    audioUntil = 0;
    try { window.speechSynthesis?.cancel(); } catch {}
    filterSession?.setJarvisSpeaking(false);
    AudioBus?.publish(null, 0, null);
  }

  function enqueueAudioBase64(b64, expectedGen = audioGeneration) {
    if (!b64 || speakerMuted || expectedGen !== audioGeneration) return Promise.resolve();
    pendingDecodes++;
    audioDecodeQueue = audioDecodeQueue.then(async () => {
      try {
        if (speakerMuted || expectedGen !== audioGeneration) return;
        await setupSpeaker(24000);
        if (speakerMuted || expectedGen !== audioGeneration) return;
        const bin = atob(b64);
        const buf = new ArrayBuffer(bin.length);
        const view = new Uint8Array(buf);
        for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
        const decoded = await speaker.decodeAudioData(buf);
        if (speakerMuted || expectedGen !== audioGeneration) return;
        const source = speaker.createBufferSource();
        source.buffer = decoded;
        source.connect(speakerAnalyser);
        const startAt = Math.max(speaker.currentTime + 0.015, audioUntil);
        audioUntil = startAt + decoded.duration;
        filterSession?.setJarvisSpeaking(true);
        phase('speaking', `${agentLabel()} Speaking`);
        audioNodes.push(source);
        source.onended = () => {
          source.disconnect();
          audioNodes = audioNodes.filter((n) => n !== source);
          if (!audioNodes.length && pendingDecodes === 0 && expectedGen === audioGeneration) {
            filterSession?.setJarvisSpeaking(false);
            voiceIdle();
          }
        };
        try { window.speechSynthesis?.cancel(); } catch {}
        source.start(startAt);
        startWave();
      } catch {
        if (!audioNodes.length && pendingDecodes === 0) voiceIdle();
      } finally {
        pendingDecodes = Math.max(0, pendingDecodes - 1);
      }
    });
    return audioDecodeQueue;
  }

  async function playWavBase64(b64, expectedGen = null) {
    if (!b64 || speakerMuted) {
      voiceIdle();
      return;
    }
    if (expectedGen !== null && expectedGen !== audioGeneration) {
      return;
    }
    const gen = expectedGen !== null ? expectedGen : ++audioGeneration;
    audioUntil = 0;
    for (const node of audioNodes) {
      node.onended = null;
      try { node.stop(); } catch {}
      node.disconnect();
    }
    audioNodes = [];
    await enqueueAudioBase64(b64, gen);
  }

  function speakLocalFallback(text) {
    if (speakerMuted || !window.speechSynthesis) {
      voiceIdle();
      return;
    }
    try {
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(String(text).replace(/[*#`>_]/g, ' ').slice(0, 600));
      const allVoices = window.speechSynthesis.getVoices?.() || [];
      const naturalLocal = allVoices.find((v) => /natural|microsoft/i.test(v.name) && /^(en-IN|hi-IN|en-)/i.test(v.lang))
        || allVoices.find((v) => /^(en-IN|hi-IN)/i.test(v.lang));
      if (naturalLocal) utter.voice = naturalLocal;
      filterSession?.setJarvisSpeaking(true);
      phase('speaking', `${agentLabel()} Speaking`);
      utter.onend = utter.onerror = () => {
        filterSession?.setJarvisSpeaking(false);
        voiceIdle();
      };
      window.speechSynthesis.speak(utter);
    } catch {
      voiceIdle();
    }
  }

  function playAudio(data) {
    if (!speaker || !liveConnected || speakerMuted) return;
    window.speechSynthesis?.cancel();
    if (speaker.state === 'suspended') speaker.resume().catch(() => {});
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    if (!bytes.length || bytes.length % 2) return;
    const pcm = new Int16Array(bytes.buffer);
    const buffer = speaker.createBuffer(1, pcm.length, 24000);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
    const source = speaker.createBufferSource();
    source.buffer = buffer;
    source.connect(speakerAnalyser);
    audioUntil = Math.max(speaker.currentTime + 0.015, audioUntil);
    source.start(audioUntil);
    audioUntil += buffer.duration;
    liveMicResumeAt = performance.now() + (audioUntil - speaker.currentTime) * 1000 + 150;
    audioNodes.push(source);
    filterSession?.setJarvisSpeaking(true);
    source.onended = () => {
      source.disconnect();
      audioNodes = audioNodes.filter((node) => node !== source);
      if (!audioNodes.length) {
        filterSession?.setJarvisSpeaking(false);
        voiceIdle();
      }
    };
    phase('speaking', `${agentLabel()} is speaking`);
    startWave();
  }

  // =========================================================================
  // Microphone Capture + Edtechh Acoustic TurnGate & SpeakerGuard Pipeline
  // =========================================================================
  async function handleCustomVoiceTurn(wavBase64) {
    if (!wavBase64 || turnBusy) return;
    turnBusy = true;
    const reqId = crypto.randomUUID();
    const turnGen = audioGeneration;
    activeVoiceReqId = reqId;
    activeVoiceGen = turnGen;
    activeVoiceHeard = '';
    activeVoiceAnswer = '';
    turnStartedAt = performance.now();
    setupSpeaker(24000).catch(() => {});
    phase('thinking', 'Understanding speech…');
    syncLiveControls();
    try {
      const result = await call('voice/turn', {
        audio_wav_b64: wavBase64,
        request_id: reqId,
        page: JarvisApp.activeView,
        chat_id: currentChat,
        voice_id: selectedVoiceId,
        rate: selectedSpeechRate,
        speak: !speakerMuted,
      });
      if (turnGen !== audioGeneration) {
        // Interrupted or reset while turn was processing
        return;
      }
      const heard = result.transcript || 'Voice command';
      addMessage('user', heard);
      const body = addMessage('assistant', result.answer, { actions: result.actions, toolRuns: result.tool_runs });
      if (result.actions?.length) Sfx?.action?.();
      lastReplyModel = result.model || '';
      historyEntries.set(reqId, { ...result, question: heard, status: result.history_state || 'pending' });
      saveChatsCache();
      saveActiveChat(currentChat);
      const label = document.createElement('small');
      label.className = 'reply-history';
      body.after(label);
      historyLabels.set(reqId, label);
      updateHistoryLabel(label, result.history_state || 'pending');
      if (!studio.hidden) showStudioCaption(heard, result.answer, result.actions, result.tool_runs);
      updateHudTelemetry(Math.round(performance.now() - turnStartedAt));
      if (result.streamed_audio && !speakerMuted) {
        if (!audioNodes.length && pendingDecodes === 0 && audioGeneration === activeVoiceGen) voiceIdle();
      } else if (result.wav_b64 && !speakerMuted) {
        await playWavBase64(result.wav_b64, turnGen);
      } else if (!speakerMuted && result.answer && !audioNodes.length && pendingDecodes === 0) {
        speakLocalFallback(result.answer);
      } else {
        voiceIdle();
      }
    } catch (error) {
      showAssistantError(error.message);
      phase('error', error.message || 'Voice turn failed');
      setTimeout(() => { if (!turnBusy) voiceIdle(); }, 1800);
    } finally {
      activeVoiceReqId = null;
      turnBusy = false;
      syncLiveControls();
    }
  }

  async function startMic() {
    if (micStream || micStarting) return;
    if (voiceEngine === 'live' && !liveConnected) return;
    const generation = ++micGeneration;
    micStarting = true;
    syncLiveControls();
    let stream, context;
    try {
      if (voiceEngine === 'custom' && !voiceArmed) {
        await call('voice/arm');
        if (generation !== micGeneration) {
          await call('voice/disarm').catch(() => {});
          return;
        }
        voiceArmed = true;
      }
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      if (generation !== micGeneration || (voiceEngine === 'live' && !liveConnected)) {
        if (voiceArmed) { voiceArmed = false; await call('voice/disarm').catch(() => {}); }
        return;
      }
      context = new AudioContext({ sampleRate: 16000 });
      await context.audioWorklet.addModule('mic-worklet.js');
      if (generation !== micGeneration || (voiceEngine === 'live' && !liveConnected)) return;

      const worklet = new AudioWorkletNode(context, 'jarvis-pcm');
      const input = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      const mute = context.createGain();
      analyser.fftSize = waveSamples.length;
      mute.gain.value = 0;
      input.connect(worklet).connect(mute).connect(context.destination);
      input.connect(analyser);

      micStream = stream;
      micContext = context;
      micInput = input;
      micAnalyser = analyser;
      micNode = worklet;
      Sfx?.micOn?.();

      let inFlight = 0;
      worklet.port.onmessage = async (event) => {
        if (generation !== micGeneration) return;
        const int16 = new Int16Array(event.data);
        if (voiceEngine === 'live') {
          if (!liveConnected || liveRecovering || inFlight >= 6) return;
          if (audioNodes.length || performance.now() < liveMicResumeAt) return;
          inFlight++;
          try {
            await call('live/audio', { data: bytesToBase64(new Uint8Array(event.data)) });
          } catch (error) {
            if (generation !== micGeneration || liveRecovering) return;
            showAssistantError(error.message);
            await stopMic();
            voiceIdle();
          } finally {
            inFlight--;
          }
          return;
        }

        // Custom Neural Engine: feed 50ms frame (800 samples) into TurnGate + SpeakerGuard
        if (!filterSession) return;
        const floatBuf = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) floatBuf[i] = int16[i] / 32768;
        const dtMs = (floatBuf.length / 16000) * 1000;
        const outcome = filterSession.pushPcm(floatBuf, dtMs, performance.now());
        if (outcome.kind === 'disturbance') {
          showFilterNotice(`Filtered noise (${outcome.reason})`);
        } else if (outcome.kind === 'filtered-speaker') {
          setStudioVisualState('LISTENING');
          showFilterNotice(`Filtered other voice (match ${(outcome.score * 100).toFixed(0)}%)`);
        } else if (outcome.kind === 'speech-start') {
          if (bargeInEnabled && (audioNodes.length || pendingDecodes || window.speechSynthesis?.speaking)) {
            clearAudio();
            call('voice/disarm').catch(() => {});
          }
          setStudioVisualState('USER_SPEAKING', 'Hearing You');
          if (!dock.hidden) phase('listening', 'Hearing you…');
        } else if (outcome.kind === 'utterance-ready') {
          handleCustomVoiceTurn(outcome.wavBase64);
        }
      };

      voiceIdle();
      startWave();
    } catch (error) {
      if (generation === micGeneration) {
        const msg = error?.message || 'Microphone access was denied.';
        showAssistantError(msg);
        if (!studio.hidden) setStudioVisualState('ERROR', 'Mic unavailable · check OS audio');
      }
    } finally {
      if (stream && stream !== micStream) stream.getTracks().forEach((track) => track.stop());
      if (context && context !== micContext) await context.close().catch(() => {});
      micStarting = false;
      syncLiveControls();
    }
  }

  async function stopMic(sendEnd = false) {
    micGeneration++;
    const hadMic = !!micStream;
    micStream?.getTracks().forEach((track) => track.stop());
    micStream = null;
    if (micNode) {
      micNode.port.onmessage = null;
      micNode.disconnect();
      micNode = null;
    }
    micInput?.disconnect();
    micInput = micAnalyser = null;
    const context = micContext;
    micContext = null;
    if (context) await context.close().catch(() => {});
    if (hadMic) Sfx?.micOff?.();
    filterSession?.reset();
    if (sendEnd && voiceEngine === 'live' && liveConnected) await call('live/mic-end').catch(() => {});
    if (voiceEngine === 'custom' && voiceArmed) {
      voiceArmed = false;
      await call('voice/disarm').catch(() => {});
    }
    syncLiveControls();
  }

  async function toggleMic() {
    if (micStarting) return;
    showAssistantError();
    if (micStream) {
      await stopMic(true);
      voiceIdle();
      return;
    }
    if (voiceEngine === 'live' && !liveConnected) return;
    await startMic();
  }

  async function stopLive() {
    if (voiceEngine === 'custom') {
      voiceStopping = true;
      clearAudio();
      await Promise.all([
        stopMic(false),
        call('voice/disarm').catch(() => {}),
      ]);
      voiceStopping = false;
      voiceIdle();
      syncLiveControls();
      return;
    }
    liveGeneration++;
    liveConnecting = liveRecovering = liveConnected = false;
    liveStopping = true;
    transcripts.clear();
    clearAudio();
    phase('ready', 'Closing Live');
    $('live-detail').textContent = 'Voice is off';
    syncLiveControls();
    try {
      await Promise.all([stopMic(), call('live/stop')]);
    } catch (error) {
      showAssistantError(error.message);
    } finally {
      liveModel = '';
      liveStopping = false;
      syncLiveControls();
      voiceIdle();
    }
  }

  async function toggleLive() {
    if (pendingChat || liveStopping || voiceStopping) return;
    if (voiceEngine === 'custom') {
      if (micStream || voiceArmed) return stopLive();
      await startMic();
      return;
    }
    if (liveConnected || liveConnecting) return stopLive();
    const generation = ++liveGeneration;
    showAssistantError();
    liveConnecting = true;
    phase('connecting', 'Connecting to Gemini Live');
    $('live-detail').textContent = 'Opening secure connection';
    syncLiveControls();
    try { setupSpeaker(24000).catch(() => {}); } catch {}
    try {
      const result = await call('live/start', { page: JarvisApp.activeView, voice: selectedLiveVoice });
      if (generation !== liveGeneration) return;
      liveConnected = true;
      liveModel = result.model;
      const context = result.workspace_partial ? 'Partial Sheet snapshot' : result.workspace_context ? 'Sheet snapshot' : 'Voice only';
      $('live-detail').textContent = (result.setup_ms / 1000).toFixed(1) + ' s setup · ' + context;
      updateHudTelemetry(result.setup_ms);
    } catch (error) {
      if (generation !== liveGeneration) return;
      phase('error', 'Live connection failed');
      $('live-detail').textContent = 'Voice is off';
      showAssistantError(error.message);
    } finally {
      if (generation === liveGeneration) {
        liveConnecting = false;
        syncLiveControls();
        if (liveConnected) voiceIdle();
      }
    }
  }

  // Side HUD 15-bar waveform loop
  function startWave() {
    if (!waveFrame && !reducedMotion.matches) waveFrame = requestAnimationFrame(drawWave);
  }

  function drawWave(now) {
    const analyser = audioNodes.length ? speakerAnalyser : micAnalyser;
    if (!analyser || dock.hidden || reducedMotion.matches) {
      waveFrame = 0;
      waveLevel = 0;
      waveBars.forEach((bar) => bar.style.removeProperty('transform'));
      return;
    }
    analyser.getFloatTimeDomainData(waveSamples);
    let power = 0;
    for (const sample of waveSamples) power += sample * sample;
    const level = Math.min(1, Math.sqrt(power / waveSamples.length) * 6);
    waveLevel = 0.72 * waveLevel + 0.28 * level;
    waveBars.forEach((bar, i) => {
      const shape = 0.35 + 0.65 * Math.sin(Math.PI * (i + 1) / (waveBars.length + 1));
      const variation = 0.65 + 0.35 * Math.sin(now / 85 + i * 1.2);
      bar.style.transform = 'scaleY(' + (0.16 + waveLevel * shape * variation) + ')';
    });
    waveFrame = requestAnimationFrame(drawWave);
  }

  // =========================================================================
  // Studio Controls & Voice Sheet Initialization
  // =========================================================================
  async function loadOsVoices() {
    const sel = $('voice-persona-select');
    if (!sel) return;
    try {
      const res = await call('voice/voices', {});
      sel.replaceChildren();
      const def = document.createElement('option');
      def.value = 'default';
      def.textContent = `${agentLabel()} · Google Studio Hindlish (Default)`;
      sel.append(def);
      for (const v of res.voices || []) {
        const opt = document.createElement('option');
        opt.value = v.id;
        opt.textContent = v.label || v.name;
        sel.append(opt);
      }
      sel.value = [...sel.options].some((o) => o.value === selectedVoiceId) ? selectedVoiceId : 'default';
    } catch {}
  }

  function openStudio() {
    setSplitDock(false);
    setStudioOpen(true);
    setupSpeaker(24000).catch(() => {});
    startStudioVisuals();
    setStudioVisualState(micStream ? 'LISTENING' : 'IDLE');
    if (Date.now() - historyCheckedAt > 60000) loadAssistantHistory();
    loadOsVoices();
    if (alwaysMicInStudio && !micStream && !micStarting) {
      if (voiceEngine === 'custom') {
        startMic();
      } else {
        (async () => {
          if (!liveConnected && !liveConnecting) await toggleLive();
          if (liveConnected && !micStream && !micStarting && !studio.hidden) await startMic();
        })();
      }
    }
  }

  function setVoiceSheetOpen(open) {
    $('studio-voice-sheet').hidden = !open;
    $('studio-voice-backdrop').hidden = !open;
    $('studio-voice-btn')?.setAttribute('aria-expanded', String(open));
  }

  function setTranscriptDrawerOpen(open) {
    $('studio-transcript-drawer').hidden = !open;
    $('studio-chat-btn')?.setAttribute('aria-expanded', String(open));
    if (open) {
      scrollConversation();
      $('studio-question')?.focus();
    }
  }

  // Sync Room Preset & Toggles from saved state
  document.querySelectorAll('#room-preset-grid .room-preset-btn').forEach((btn) => {
    const thresh = Number(btn.dataset.threshold);
    btn.classList.toggle('active', Math.abs(thresh - vadThreshold) < 0.005);
    btn.onclick = () => {
      vadThreshold = thresh;
      try { localStorage.setItem('jarvis-vad-threshold', String(thresh)); } catch {}
      document.querySelectorAll('#room-preset-grid .room-preset-btn').forEach((b) => b.classList.toggle('active', b === btn));
      filterSession?.configure({ threshold: thresh });
      showFilterNotice(`Room profile: ${btn.querySelector('strong').textContent}`);
    };
  });

  if ($('vf-always-mic')) {
    $('vf-always-mic').checked = alwaysMicInStudio;
    $('vf-always-mic').onchange = (e) => {
      alwaysMicInStudio = e.target.checked;
      try { localStorage.setItem('jarvis-always-mic', String(alwaysMicInStudio)); } catch {}
    };
  }
  if ($('vf-speaker-lock')) {
    $('vf-speaker-lock').checked = lockToMyVoice;
    $('vf-speaker-lock').onchange = (e) => {
      lockToMyVoice = e.target.checked;
      try { localStorage.setItem('jarvis-speaker-lock', String(lockToMyVoice)); } catch {}
      filterSession?.configure({ lockToMyVoice });
      setStudioVisualState(studioState);
    };
  }
  if ($('speaker-reset-btn')) {
    $('speaker-reset-btn').onclick = () => {
      filterSession?.speaker.reset();
      setStudioVisualState(studioState);
      showFilterNotice('Voiceprint reset — next 3 turns will enroll you');
    };
  }
  if ($('vf-barge-in')) {
    $('vf-barge-in').checked = bargeInEnabled;
    $('vf-barge-in').onchange = (e) => {
      bargeInEnabled = e.target.checked;
      try { localStorage.setItem('jarvis-barge-in', String(bargeInEnabled)); } catch {}
    };
  }
  if ($('voice-engine-select')) {
    $('voice-engine-select').value = voiceEngine;
    $('voice-engine-select').onchange = async (e) => {
      await stopLive();
      voiceEngine = e.target.value;
      try { localStorage.setItem('jarvis-voice-engine', voiceEngine); } catch {}
      syncLiveControls();
      modelLabel();
      updateHudTelemetry();
      if (!studio.hidden && alwaysMicInStudio) {
        if (voiceEngine === 'custom') startMic();
        else {
          await toggleLive();
          if (liveConnected && !micStream) await startMic();
        }
      }
    };
  }
  if ($('voice-persona-select')) {
    $('voice-persona-select').onchange = (e) => {
      selectedVoiceId = e.target.value;
      try { localStorage.setItem('jarvis-tts-voice', selectedVoiceId); } catch {}
      updateHudTelemetry();
    };
  }
  if ($('live-voice-select')) {
    $('live-voice-select').value = selectedLiveVoice;
    $('live-voice-select').onchange = (e) => {
      selectedLiveVoice = e.target.value || 'Charon';
      try { localStorage.setItem('jarvis-live-voice', selectedLiveVoice); } catch {}
      updateHudTelemetry();
    };
  }
  if ($('voice-rate-select')) {
    $('voice-rate-select').value = String(selectedSpeechRate);
    $('voice-rate-select').onchange = (e) => {
      selectedSpeechRate = Number(e.target.value) || 1;
      try { localStorage.setItem('jarvis-speech-rate', String(selectedSpeechRate)); } catch {}
    };
  }

  // Event Subscriptions
  JarvisApp.handlers.event.push((packet) => {
    const data = packet.data;
    if (packet.event === 'assistant') receiveAssistant(data);
    if (packet.event === 'voice') {
      if (!data || data.request_id !== activeVoiceReqId || audioGeneration !== activeVoiceGen) return;
      if (data.type === 'transcript_heard' && data.text) {
        activeVoiceHeard = data.text;
        if (!studio.hidden) showStudioCaption(activeVoiceHeard, activeVoiceAnswer || 'Preparing reply…');
      } else if (data.type === 'transcript_delta' && data.text) {
        activeVoiceAnswer = (activeVoiceAnswer + data.text).replace(/^HEARD:\s*[^\n]*\n?/i, '');
        if (!studio.hidden) showStudioCaption(activeVoiceHeard, activeVoiceAnswer);
      } else if (data.type === 'audio_chunk' && data.audio_b64) {
        if (data.seq === 0 && turnStartedAt) updateHudTelemetry(Math.round(performance.now() - turnStartedAt));
        enqueueAudioBase64(data.audio_b64, activeVoiceGen);
      }
      return;
    }
    if (packet.event !== 'live') return;
    if (data.type === 'reconnecting' && liveConnected) {
      liveRecovering = true;
      clearAudio();
      transcripts.clear();
      liveUserCaption = '';
      liveJarvisCaption = '';
      phase('connecting', 'Reconnecting to Gemini Live');
      $('live-detail').textContent = 'Waiting for Google';
      syncLiveControls();
      return;
    }
    if (data.type === 'reconnected' && liveConnected) {
      liveRecovering = false;
      $('live-detail').textContent = data.resumed ? 'Live session resumed' : 'Fresh Live session';
      syncLiveControls();
      voiceIdle();
      return;
    }
    if (data.type === 'closed') {
      if (liveStopping || !liveConnected) return;
      liveConnected = liveRecovering = false;
      liveModel = '';
      clearAudio();
      transcripts.clear();
      liveUserCaption = '';
      liveJarvisCaption = '';
      stopMic();
      phase('ready', 'Live session ended');
      $('live-detail').textContent = 'Voice is off';
      syncLiveControls();
    } else if (liveConnected) {
      if (data.type === 'audio') playAudio(data.data);
      if (data.type === 'transcript') {
        if (!transcripts.has(data.role)) transcripts.set(data.role, addMessage(data.role, ''));
        const body = transcripts.get(data.role);
        const nextRaw = ((body.dataset.rawText || body.textContent || '') + data.text).slice(0, 8000);
        body.dataset.rawText = nextRaw;
        if (data.role === 'assistant') renderAssistantText(body, nextRaw);
        else body.textContent = nextRaw;
        scrollConversation();
        if (data.role === 'user') liveUserCaption = (liveUserCaption + data.text).slice(0, 1000);
        else liveJarvisCaption = (liveJarvisCaption + data.text).slice(0, 4000);
        if (!studio.hidden) showStudioCaption(liveUserCaption, liveJarvisCaption || 'Speaking…');
      }
      if (data.type === 'interrupted' || data.type === 'turn_complete') {
        if (data.type === 'interrupted') clearAudio();
        const assistantBody = transcripts.get('assistant');
        if (assistantBody?.dataset?.rawText) renderAssistantText(assistantBody, assistantBody.dataset.rawText);
        transcripts.clear();
        liveUserCaption = '';
        liveJarvisCaption = '';
        if (!audioNodes.length) voiceIdle();
      }
    }
  });

  JarvisApp.handlers.serviceState.push((service) => {
    if (service.state === 'failed' || service.state === 'stopped') {
      liveGeneration++;
      liveConnected = liveConnecting = liveRecovering = false;
      liveModel = '';
      clearAudio();
      stopMic();
      phase('error', 'Local engine disconnected');
      $('live-detail').textContent = 'Voice is off';
      syncLiveControls();
    }
  });

  JarvisApp.handlers.viewChange.push((name) => {
    if (name === 'assistant') return openStudio();
    if (!dock.hidden) $('jarvis-context').textContent = 'Viewing ' + (JarvisApp.labels[name] || name);
  });
  JarvisApp.handlers.statusChange.push(() => modelLabel());
  JarvisApp.handlers.brandingChange.push((name) => {
    const label = name || 'Jarvis';
    syncLiveControls();
    setStudioVisualState(studioState);
    if (composer) composer.placeholder = `Ask or command ${label}`;
    if ($('studio-question')) $('studio-question').placeholder = `Type a message to ${label}…`;
    document.querySelectorAll('.dock-message.assistant > small').forEach((el) => { el.textContent = label; });
    const sel = $('voice-persona-select');
    if (sel?.options?.[0]?.value === 'default') sel.options[0].textContent = `${label} · Google Studio Hindlish (Default)`;
  });

  window.addEventListener('beforeunload', () => micStream?.getTracks().forEach((track) => track.stop()));

  // Side HUD Button Wiring
  $('open-jarvis').onclick = openJarvis;
  $('jarvis-expand-studio').onclick = () => openStudio();
  $('jarvis-new-chat').onclick = () => {
    userSelectedNewChat = true;
    selectChat(crypto.randomUUID());
  };
  $('jarvis-history-toggle').onclick = () => setHistoryVisible($('jarvis-history').hidden);
  $('jarvis-history-refresh').onclick = () => loadAssistantHistory(true);
  $('jarvis-history-search').oninput = renderAssistantHistory;
  $('close-jarvis').onclick = () => {
    setSplitDock(false);
    opener?.focus();
  };
  dock.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') $('close-jarvis').click();
  });
  historyPanel.addEventListener('click', (event) => {
    const button = event.target.closest('.dock-intro .jarvis-suggestion');
    if (button) {
      composer.value = button.textContent;
      composer.focus();
    }
  });
  composer.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!sendButton.disabled) $('dock-form').requestSubmit(sendButton);
    }
  });
  $('dock-form').onsubmit = sendAssistantQuestion;
  liveButton.onclick = toggleLive;
  micButton.onclick = toggleMic;

  // Full-Screen Animated Bubble Studio Button Wiring
  $('studio-back').onclick = async () => {
    setVoiceSheetOpen(false);
    setTranscriptDrawerOpen(false);
    if (voiceEngine === 'custom' && (micStream || micStarting || voiceArmed)) {
      await stopMic(false);
    }
    view(JarvisApp.activeView || 'desk');
  };
  $('studio-split').onclick = () => {
    setVoiceSheetOpen(false);
    setTranscriptDrawerOpen(false);
    setStudioOpen(false);
    openJarvis();
  };
  $('studio-new-chat').onclick = () => {
    userSelectedNewChat = true;
    selectChat(crypto.randomUUID());
    $('studio-captions').hidden = true;
  };
  $('studio-mic-btn').onclick = async () => {
    if (voiceEngine === 'live' && !liveConnected && !liveConnecting) {
      await toggleLive();
      if (liveConnected && !micStream) await startMic();
      return;
    }
    await toggleMic();
  };
  $('studio-speaker-btn').onclick = () => {
    if (audioNodes.length || window.speechSynthesis?.speaking) {
      clearAudio();
      voiceIdle();
      return;
    }
    speakerMuted = !speakerMuted;
    syncLiveControls();
  };
  $('studio-voice-btn').onclick = () => setVoiceSheetOpen($('studio-voice-sheet').hidden);
  $('studio-voice-close').onclick = () => setVoiceSheetOpen(false);
  $('studio-voice-backdrop').onclick = () => setVoiceSheetOpen(false);
  $('studio-chat-btn').onclick = () => setTranscriptDrawerOpen($('studio-transcript-drawer').hidden);
  $('studio-chat-close').onclick = () => setTranscriptDrawerOpen(false);
  $('studio-end-btn').onclick = async () => {
    await stopLive();
    $('studio-back').click();
  };
  $('studio-form').onsubmit = (e) => {
    e.preventDefault();
    const q = $('studio-question').value.trim();
    if (!q) return;
    $('studio-question').value = '';
    submitTextPrompt(q, true);
  };

  // Interactive 3D Reticle Parallax on Side HUD
  const art = core.querySelector('.core-visual');
  core.addEventListener('pointermove', (event) => {
    if (reducedMotion.matches || event.pointerType === 'touch') return;
    const rect = core.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width - 0.5;
    const y = (event.clientY - rect.top) / rect.height - 0.5;
    art.style.transform = 'perspective(260px) rotateX(' + (-y * 20) + 'deg) rotateY(' + (x * 24) +
      'deg) translate(' + (x * 16) + 'px,' + (y * 9) + 'px)';
  });
  core.addEventListener('pointerleave', () => art.style.removeProperty('transform'));
  reducedMotion.addEventListener('change', () => {
    art.style.removeProperty('transform');
    if (!reducedMotion.matches) {
      startWave();
      startStudioVisuals();
    }
  });

  JarvisApp.handlers.resetChats = () => {
    historyEntries.clear();
    historyLabels.clear();
    currentChat = crypto.randomUUID();
    userSelectedNewChat = false;
    try {
      localStorage.removeItem(CHATS_CACHE_KEY);
      localStorage.removeItem(ACTIVE_CHAT_KEY);
    } catch {}
    renderChat();
    renderAssistantHistory();
  };

  restoreChatsCache();
  syncLiveControls();
})();
