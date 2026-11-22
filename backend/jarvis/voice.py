"""Natural Google TTS studio voice synthesizer (Gemini Studio Native Audio + Google TTS Classic + Neural/SAPI fallback)."""
import asyncio
import base64
import io
import re
import time
import wave
import weakref

DEFAULT_NEURAL_VOICE = "google:Charon"
GEMINI_TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-2.5-flash-preview-tts")
GEMINI_TTS_MODEL = GEMINI_TTS_MODELS[0]
_GTTS_CLIENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_GEMINI_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_GEMINI_COOLDOWN_UNTIL: float = 0.0


def _gemini_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _GEMINI_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _GEMINI_LOCKS[loop] = lock
    return lock


def reset_tts_cooldown() -> None:
    global _GEMINI_COOLDOWN_UNTIL
    _GEMINI_COOLDOWN_UNTIL = 0.0

NEURAL_VOICES = [
    {
        "id": "google:Charon",
        "name": "Jarvis (Google Studio · Natural Hindlish)",
        "label": "Jarvis · Google Studio Hindlish (Default)",
        "engine": "google",
    },
    {
        "id": "google:Kore",
        "name": "Kore (Google Studio · Clear Warm)",
        "label": "Kore · Google Studio Warm",
        "engine": "google",
    },
    {
        "id": "google:Puck",
        "name": "Puck (Google Studio · Expressive Fast)",
        "label": "Puck · Google Expressive",
        "engine": "google",
    },
    {
        "id": "google:Orus",
        "name": "Orus (Google Studio · Deep Calm)",
        "label": "Orus · Google Deep Calm",
        "engine": "google",
    },
    {
        "id": "google:Aoede",
        "name": "Aoede (Google Studio · Smooth Conversational)",
        "label": "Aoede · Google Conversational",
        "engine": "google",
    },
    {
        "id": "google:Fenrir",
        "name": "Fenrir (Google Studio · Crisp Desk)",
        "label": "Fenrir · Google Crisp Desk",
        "engine": "google",
    },
    {
        "id": "gtts:auto",
        "name": "Google TTS Classic (Hindlish Auto)",
        "label": "Google Classic · Hindlish / India",
        "engine": "google-classic",
    },
    {
        "id": "hi-IN-MadhurNeural",
        "name": "Madhur (Hindi / Hinglish Neural)",
        "label": "Madhur · India Hindi Neural",
        "engine": "neural",
    },
    {
        "id": "en-IN-PrabhatNeural",
        "name": "Prabhat (Indian English Neural)",
        "label": "Prabhat · Natural Indian English",
        "engine": "neural",
    },
    {
        "id": "en-IN-NeerjaExpressiveNeural",
        "name": "Neerja (Expressive India Neural)",
        "label": "Neerja · Expressive Indian English",
        "engine": "neural",
    },
    {
        "id": "en-GB-RyanNeural",
        "name": "Ryan (UK Neural)",
        "label": "Ryan · British AI",
        "engine": "neural",
    },
]
_KNOWN_IDS = {v["id"] for v in NEURAL_VOICES}

_HINDLISH_RE = re.compile(
    r"[\u0900-\u097F]|"
    r"\b(?:bhai|yaar|yarr|kya|hai|hain|haan|nahi|nai|karo|batao|dekho|kitna|kitne|"
    r"kaun|abhi|aaj|kal|acha|accha|theek|thik|matlab|chahiye|bhej|lena|dena|bechna|"
    r"khareed|rupay|lakh|crore|paisa|baat|samjha|bolo|suno|sunno|chalo|arre|oye|bhaiya)\b",
    re.IGNORECASE,
)


def has_hindlish(text: str) -> bool:
    return bool(_HINDLISH_RE.search(text or ""))


def clean_speech_text(text: str) -> str:
    """Turn markdown and currency/trade notation into fluid spoken English or Hindlish."""
    cleaned = re.sub(r"```[\s\S]*?```", " code snippet omitted. ", text or "")
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"^HEARD:\s*[^\n]*\n?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("→", " to ").replace("->", " to ")
    cleaned = re.sub(r"₹\s*([0-9][0-9,.]*)", r"\1 rupees", cleaned)
    cleaned = re.sub(r"\$\s*([0-9][0-9,.]*)", r"\1 dollars", cleaned)
    cleaned = re.sub(r"€\s*([0-9][0-9,.]*)", r"\1 euros", cleaned)
    cleaned = re.sub(r"£\s*([0-9][0-9,.]*)", r"\1 pounds", cleaned)
    cleaned = re.sub(r"\bWTB\b", "buy request", cleaned)
    cleaned = re.sub(r"\bWTS\b", "stock offer", cleaned)
    cleaned = cleaned.replace("&", " and ")
    cleaned = re.sub(r"[#*_~>|•✓]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:1600]


def _pcm16_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _list_sapi_voices_sync() -> list[dict]:
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return []
    pythoncom.CoInitialize()
    try:
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        out = []
        for idx, token in enumerate(speaker.GetVoices()):
            desc = token.GetDescription()
            short = desc.split(" - ")[0].strip()
            out.append({
                "id": token.Id,
                "index": idx,
                "name": short,
                "label": f"{short} (Offline OS)",
                "engine": "sapi",
            })
        return out
    except Exception:
        return []
    finally:
        pythoncom.CoUninitialize()


def _synthesize_wav_sync(text: str, voice_id: str | None = None, rate: int = 1) -> bytes | None:
    spoken = clean_speech_text(text)
    if not spoken:
        return None
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        voice.Rate = max(-6, min(6, int(rate)))
        if voice_id and voice_id != "default" and voice_id not in _KNOWN_IDS:
            for token in voice.GetVoices():
                if token.Id == voice_id or voice_id in token.GetDescription():
                    voice.Voice = token
                    break
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 22  # SAFT22kHz16BitMono
        stream.Format = fmt
        voice.AudioOutputStream = stream
        voice.Speak(spoken, 0)
        pcm = bytes(stream.GetData())
        return _pcm16_to_wav(pcm, 22050) if pcm else None
    except Exception:
        return None
    finally:
        pythoncom.CoUninitialize()


def _resolve_voice(voice_id: str | None) -> str | None:
    if not voice_id or voice_id == "default":
        return DEFAULT_NEURAL_VOICE
    if voice_id in _KNOWN_IDS:
        return voice_id
    if re.fullmatch(r"google:[A-Za-z]{2,24}", voice_id):
        return voice_id
    if re.fullmatch(r"gtts:(auto|hi|en-IN|en)", voice_id):
        return voice_id
    if re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]+Neural", voice_id):
        return voice_id
    return None


async def _gemini_tts_wav(
    text: str, voice_name: str, *, vault=None, rate: int = 1
) -> tuple[bytes, str] | None:
    global _GEMINI_COOLDOWN_UNTIL
    spoken = clean_speech_text(text)
    if not spoken or vault is None:
        return None
    if time.monotonic() < _GEMINI_COOLDOWN_UNTIL:
        return None
    try:
        from .gemini import GeminiClient
        client_helper = GeminiClient(vault)
        keys = client_helper.config().get("keys", [])
        if not keys:
            return None
        pace = "measured calm" if int(rate) <= 0 else "brisk responsive" if int(rate) >= 2 else "natural conversational"
        prompt = (
            "Read ONLY the transcript verbatim out loud.\n"
            "Audio Profile: A warm, friendly Indian electronics trading assistant speaking crisp English and everyday Hindlish seamlessly.\n"
            f"Director's Notes: Speak at a {pace} pace like a real coworker on a clean microphone. Pronounce Hindi/Hinglish words (bhai, yaar, kitna, batao) and Indian names naturally without robotic breaks.\n"
            f"Transcript:\n{spoken}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}
                },
            },
        }
        async with _gemini_lock():
            if time.monotonic() < _GEMINI_COOLDOWN_UNTIL:
                return None
            saw_rate_limit = False
            async with client_helper._http_client(10) as client:
                for key in keys:
                    for model in GEMINI_TTS_MODELS:
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                        try:
                            resp = await client.post(url, headers={"x-goog-api-key": key}, json=payload)
                        except Exception:
                            continue
                        if not resp.is_success:
                            if resp.status_code == 429:
                                saw_rate_limit = True
                                break
                            if resp.status_code in (500, 502, 503):
                                saw_rate_limit = True
                                continue
                            if resp.status_code in (400, 401, 403, 404):
                                continue
                            continue
                        packet = resp.json()
                        cands = packet.get("candidates") or []
                        parts = cands[0].get("content", {}).get("parts", []) if cands else []
                        for part in parts:
                            inline = part.get("inlineData") or {}
                            data_b64 = inline.get("data")
                            if not data_b64:
                                continue
                            raw = base64.b64decode(data_b64)
                            mime = (inline.get("mimeType") or "").lower()
                            if "mpeg" in mime or "mp3" in mime:
                                return raw, "audio/mpeg"
                            if "wav" in mime:
                                return raw, "audio/wav"
                            rate_match = re.search(r"rate=(\d+)", mime)
                            sample_rate = int(rate_match.group(1)) if rate_match else 24000
                            return _pcm16_to_wav(raw, sample_rate), "audio/wav"
            if saw_rate_limit:
                _GEMINI_COOLDOWN_UNTIL = time.monotonic() + 15.0
    except Exception:
        return None
    return None


def _split_gtts_chunks(text: str, limit: int = 180) -> list[str]:
    chunks = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        slice_part = remaining[:limit]
        cut = max(slice_part.rfind("."), slice_part.rfind("?"), slice_part.rfind("!"), slice_part.rfind(","))
        if cut < 40:
            cut = slice_part.rfind(" ")
        if cut < 20:
            cut = limit - 1
        chunks.append(remaining[: cut + 1].strip())
        remaining = remaining[cut + 1 :].strip()
    return [c for c in chunks if c]


async def _google_tts_classic_mp3(text: str, lang: str = "auto") -> bytes | None:
    spoken = clean_speech_text(text)
    if not spoken:
        return None
    try:
        import httpx
    except ImportError:
        return None
    tl = "hi" if (lang == "hi" or (lang == "auto" and has_hindlish(spoken))) else ("en" if lang == "en" else "en-IN")
    loop = asyncio.get_running_loop()
    client = _GTTS_CLIENTS.get(loop)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(6, connect=3),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        _GTTS_CLIENTS[loop] = client
    out = bytearray()
    try:
        for chunk in _split_gtts_chunks(spoken):
            resp = await client.get(
                "https://translate.google.com/translate_tts",
                params={"ie": "UTF-8", "q": chunk, "tl": tl, "client": "tw-ob"},
            )
            if not resp.is_success or not resp.content:
                return None
            out.extend(resp.content)
        return bytes(out) if out else None
    except Exception:
        return None


async def _edge_tts_mp3(text: str, voice_name: str, rate: int = 1) -> bytes | None:
    spoken = clean_speech_text(text)
    if not spoken:
        return None
    try:
        import edge_tts
    except ImportError:
        return None
    clamped = max(-6, min(6, int(rate)))
    pct = max(-30, min(40, clamped * 6))
    rate_str = f"{pct:+d}%"
    try:
        comm = edge_tts.Communicate(
            spoken,
            voice=voice_name,
            rate=rate_str,
            connect_timeout=5,
            receive_timeout=12,
        )
        chunks = bytearray()
        async for msg in comm.stream():
            if msg.get("type") == "audio" and msg.get("data"):
                chunks.extend(msg["data"])
        return bytes(chunks) if chunks else None
    except Exception:
        return None


async def list_voices() -> list[dict]:
    sapi = await asyncio.to_thread(_list_sapi_voices_sync)
    return [*NEURAL_VOICES, *sapi]


def _pick_edge_voice(text: str, explicit: str | None = None) -> str:
    if explicit and re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]+Neural", explicit):
        return explicit
    return "hi-IN-MadhurNeural" if has_hindlish(text) else "en-IN-PrabhatNeural"


class VoiceTurnRenderer:
    """Locks a single TTS engine and speaker across all streamed clauses of one reply turn."""

    def __init__(self, *, voice_id: str | None = None, rate: int = 1, vault=None):
        self.raw_voice_id = voice_id
        self.resolved = _resolve_voice(voice_id)
        self.rate = rate
        self.vault = vault
        self._lock = asyncio.Lock()
        self._mode: str | None = None  # "gemini" | "edge" | "gtts" | "sapi"
        self._edge_voice: str | None = None

    async def synthesize(self, text: str) -> tuple[str | None, str | None]:
        clean = clean_speech_text(text)
        if not clean:
            return None, None
        async with self._lock:
            if self._mode is None:
                return await self._synthesize_first_unlocked(clean)
            return await self._synthesize_locked(clean)

    async def _synthesize_first_unlocked(self, clean: str) -> tuple[str | None, str | None]:
        res = self.resolved
        if res and res.startswith("google:"):
            voice_name = res.split(":", 1)[1]
            gem = await _gemini_tts_wav(clean, voice_name, vault=self.vault, rate=self.rate)
            if gem:
                self._mode = "gemini"
                return base64.b64encode(gem[0]).decode("ascii"), gem[1]
            self._edge_voice = _pick_edge_voice(clean)
            e_mp3 = await _edge_tts_mp3(clean, self._edge_voice, rate=self.rate)
            if e_mp3:
                self._mode = "edge"
                return base64.b64encode(e_mp3).decode("ascii"), "audio/mpeg"
        elif res and res.startswith("gtts:"):
            lang = res.split(":", 1)[1]
            g_mp3 = await _google_tts_classic_mp3(clean, lang)
            if g_mp3:
                self._mode = "gtts"
                return base64.b64encode(g_mp3).decode("ascii"), "audio/mpeg"
            self._edge_voice = _pick_edge_voice(clean)
            e_mp3 = await _edge_tts_mp3(clean, self._edge_voice, rate=self.rate)
            if e_mp3:
                self._mode = "edge"
                return base64.b64encode(e_mp3).decode("ascii"), "audio/mpeg"
        elif res:
            self._edge_voice = _pick_edge_voice(clean, res)
            e_mp3 = await _edge_tts_mp3(clean, self._edge_voice, rate=self.rate)
            if e_mp3:
                self._mode = "edge"
                return base64.b64encode(e_mp3).decode("ascii"), "audio/mpeg"

        wav_bytes = await asyncio.to_thread(_synthesize_wav_sync, clean, self.raw_voice_id, self.rate)
        if wav_bytes:
            self._mode = "sapi"
            return base64.b64encode(wav_bytes).decode("ascii"), "audio/wav"
        return None, None

    async def _synthesize_locked(self, clean: str) -> tuple[str | None, str | None]:
        # Keep every clause in the exact same voice & engine locked by clause 1 (no robotic switches).
        if self._mode == "gemini":
            voice_name = (self.resolved or "google:Charon").split(":", 1)[1]
            gem = await _gemini_tts_wav(clean, voice_name, vault=self.vault, rate=self.rate)
            if gem:
                return base64.b64encode(gem[0]).decode("ascii"), gem[1]
            # Retry once briefly inside same Studio voice before dropping audio rather than mixing speakers.
            await asyncio.sleep(0.25)
            gem_retry = await _gemini_tts_wav(clean, voice_name, vault=self.vault, rate=self.rate)
            if gem_retry:
                return base64.b64encode(gem_retry[0]).decode("ascii"), gem_retry[1]
            return None, None
        if self._mode == "edge":
            edge_voice = self._edge_voice or _pick_edge_voice(clean)
            e_mp3 = await _edge_tts_mp3(clean, edge_voice, rate=self.rate)
            return (base64.b64encode(e_mp3).decode("ascii"), "audio/mpeg") if e_mp3 else (None, None)
        if self._mode == "gtts":
            lang = (self.resolved or "gtts:auto").split(":", 1)[1]
            g_mp3 = await _google_tts_classic_mp3(clean, lang)
            return (base64.b64encode(g_mp3).decode("ascii"), "audio/mpeg") if g_mp3 else (None, None)
        if self._mode == "sapi":
            wav_bytes = await asyncio.to_thread(_synthesize_wav_sync, clean, self.raw_voice_id, self.rate)
            return (base64.b64encode(wav_bytes).decode("ascii"), "audio/wav") if wav_bytes else (None, None)
        return None, None


async def speak_audio_base64(
    text: str, *, voice_id: str | None = None, rate: int = 1, vault=None
) -> tuple[str | None, str | None]:
    """Synthesize natural single-voice Google Studio or Edge Neural speech (without robotic gTTS fallback)."""
    renderer = VoiceTurnRenderer(voice_id=voice_id, rate=rate, vault=vault)
    return await renderer.synthesize(text)


async def speak_wav_base64(text: str, *, voice_id: str | None = None, rate: int = 1, vault=None) -> str | None:
    b64, _mime = await speak_audio_base64(text, voice_id=voice_id, rate=rate, vault=vault)
    return b64
