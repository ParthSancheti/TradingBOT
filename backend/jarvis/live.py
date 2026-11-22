"""Server-side Gemini Live transport. Standard API keys never enter the renderer."""
import asyncio
import base64
import json
import re
import time
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from .gemini import GeminiClient,GeminiError
from .intelligence import DeskSettings, settings as load_desk_settings
from .workspace import AgentToolbox, build_agent_prompt

DEFAULT_LIVE_VOICE = "Charon"
LIVE_MODEL_FALLBACKS = (
    "gemini-3.8-live",
    "gemini-2.5-flash-native-audio-latest",
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-2.0-flash-live-001",
)


class LiveSession:
    def __init__(self,repo,vault,emit,workspace=None):
        self.repo,self.vault,self.emit,self.workspace=repo,vault,emit,workspace
        self.socket=self.receiver=self.starting=None
        self.started=0;self.frames=0;self.always_on=True;self._agent_cfg={}

    async def start(self,page="desk",voice_name=None):
        if self.starting or self.receiver:
            raise GeminiError("LIVE_ACTIVE","A Live session is already connecting or open.")
        resolved_voice = voice_name if (voice_name and re.fullmatch(r"[A-Za-z]{2,24}", str(voice_name))) else DEFAULT_LIVE_VOICE
        self.always_on = True
        self.starting=asyncio.create_task(self._connect(page,resolved_voice))
        try:
            return await self.starting
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise GeminiError("LIVE_CANCELLED","Live connection cancelled.") from None
        finally:
            self.starting=None

    def remember_model(self,model):
        if self.vault.get("desktop_live_model") != model:
            self.vault.set("desktop_live_model",model)

    async def _connect(self,page,voice_name=DEFAULT_LIVE_VOICE):
        gemini=GeminiClient(self.vault);cfg=gemini.config()
        keys=cfg.get("keys",[])
        if not keys:raise GeminiError("MODEL_KEY_REQUIRED","Add a Gemini key in Connections first.")
        if cfg.get("paused_until",0)>time.time() and not GeminiClient(self.vault).status().get("active_key_count"):
            raise GeminiError("MODEL_COOLDOWN","Gemini quota cooldown is active.")
        preferred_model=DeskSettings(live_model=self.vault.get("desktop_live_model") or DeskSettings().live_model).live_model
        discovered=cfg.get("discovered_live_models") or []
        models=[]
        for m in (preferred_model, *discovered, *LIVE_MODEL_FALLBACKS):
            if m and m not in models:
                models.append(m)
        context=self.workspace.current() if self.workspace else None
        safe_context={k:v for k,v in (context or {}).items() if k!="recent_messages"}
        agent_cfg = dict(self._agent_cfg or {})
        if not agent_cfg and self.repo is not None:
            try:
                agent_cfg = await asyncio.wait_for(load_desk_settings(self.repo), timeout=0.05)
                self._agent_cfg = agent_cfg
            except Exception:
                agent_cfg = {}
        agent_name = str(agent_cfg.get("agent_name") or "Jarvis").strip() or "Jarvis"
        instruction=(
            build_agent_prompt(agent_cfg, voice_mode=True) + "\n"
            f"Remember your name is {agent_name}. Suggest {agent_name} text if a full Sheet write fails.\n"
            "Call lookup_inventory for stock questions before answering.\n"
            "Current screen: "+page+"\n"+json.dumps(safe_context,ensure_ascii=False)
        )
        base_setup={"generationConfig":{"responseModalities":["AUDIO"],
               "speechConfig":{"voiceConfig":{"prebuiltVoiceConfig":{"voiceName":voice_name}}}},
               "systemInstruction":{"parts":[{"text":instruction}]},"inputAudioTranscription":{},
               "outputAudioTranscription":{},"contextWindowCompression":{"slidingWindow":{}},
               "tools":[{"functionDeclarations":[{
                   "name":"lookup_inventory","description":"Look up recorded stock by device or variant.",
                   "parameters":{"type":"OBJECT","properties":{"search":{"type":"STRING"}},"required":["search"]},
               }]}]}
        started=time.monotonic()
        active_key, active_model = keys[0], preferred_model
        last_err = None
        for model in models[:4]:
            setup = {"model": "models/" + model, **base_setup}
            for key in keys:
                try:
                    self.socket = await self._open(key, setup)
                    active_key, active_model = key, model
                    break
                except GeminiError as err:
                    last_err = err
                    continue
            if self.socket is not None:
                break
        if self.socket is None:
            raise last_err or GeminiError("LIVE_UNAVAILABLE", "Gemini Live could not connect across configured keys/models.")
        if active_model != preferred_model:
            self.remember_model(active_model)
        if self.workspace and not context:
            self.workspace.warm()
        setup = {"model": "models/" + active_model, **base_setup}
        self.started=time.monotonic();self.frames=0
        self.receiver=asyncio.create_task(self.receive(active_key,setup,keys=keys))
        has_context=bool(context and set(safe_context)-{"snapshot_at","unavailable"})
        return {"connected":True,"model":active_model,"voice":voice_name,"key_exposed":False,
                "setup_ms":round((time.monotonic()-started)*1000),"workspace_context":has_context,
                "workspace_partial":bool(has_context and context["unavailable"])}

    async def _open(self,key,setup,handle=None):
        sock=None
        try:
            sock=await connect("wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent",
                additional_headers={"x-goog-api-key":key},open_timeout=8,close_timeout=1,max_size=2000000)
            await sock.send(json.dumps({"setup":{**setup,"sessionResumption":{"handle":handle} if handle else {}}}))
            async with asyncio.timeout(8):
                while "setupComplete" not in json.loads(await sock.recv()):
                    pass
        except asyncio.CancelledError:
            if sock:await sock.close()
            raise
        except Exception:
            if sock:await sock.close()
            raise GeminiError("LIVE_UNAVAILABLE","Gemini Live could not connect. Check the connection in Settings.") from None
        return sock

    async def receive(self,key,setup,*,keys=None):
        handle=None
        reconnects=0
        key_ring = list(keys or [key])
        key_idx = key_ring.index(key) if key in key_ring else 0
        try:
            while self.socket:
                sock=self.socket
                try:
                    handle=await self.read_session(sock,handle)
                finally:
                    self.socket=None
                    await sock.close()
                if not self.always_on:
                    break
                if time.monotonic()-self.started>=30:
                    reconnects=0
                self.emit("live",{"type":"reconnecting"})
                while self.always_on:
                    delay = min(8.0, 0.5 * (2 ** min(reconnects, 4)))
                    await asyncio.sleep(delay)
                    reconnects+=1
                    cur_key = key_ring[key_idx % len(key_ring)]
                    try:
                        self.socket=await self._open(cur_key,setup,handle)
                    except GeminiError:
                        handle=None
                        key_idx += 1
                    else:
                        self.started=time.monotonic();self.frames=0
                        self.emit("live",{"type":"reconnected","resumed":bool(handle)})
                        break
        except asyncio.CancelledError:
            raise
        else:
            self.emit("live",{"type":"closed","message":"Live session ended. Start Live to reconnect."})
        finally:
            self.receiver=None

    async def respond_to_tools(self, sock, calls):
        responses = []
        toolbox = AgentToolbox(self.repo, self.vault, workspace=self.workspace)
        for call in calls:
            name, call_id = call.get("name"), call.get("id")
            if not isinstance(name, str) or not isinstance(call_id, str):
                continue
            args = call.get("args")
            search = args.get("search") if isinstance(args, dict) else None
            if name != "lookup_inventory":
                result = {"error": "Tool unavailable in Live"}
            elif not isinstance(search, str) or not search.strip() or len(search) > 120:
                result = {"error": "Provide a device or variant to look up"}
            else:
                try:
                    result = await asyncio.wait_for(toolbox.execute(
                        "query_sheets", {"collection": "inventory", "search": search, "limit": 8},
                    ), timeout=3)
                except TimeoutError:
                    result = {"error": "Inventory lookup timed out"}
            responses.append({"id": call_id, "name": name, "response": result})
        if responses:
            await sock.send(json.dumps({"toolResponse": {"functionResponses": responses}}))

    async def read_session(self,sock,handle):
        try:
            async for raw in sock:
                packet=json.loads(raw);content=packet.get("serverContent",{})
                calls=packet.get("toolCall",{}).get("functionCalls",[])
                if calls:await self.respond_to_tools(sock,calls)
                update=packet.get("sessionResumptionUpdate",{})
                if update.get("resumable") and update.get("newHandle"):
                    handle=update["newHandle"]
                if content.get("interrupted"):self.emit("live",{"type":"interrupted"})
                for name,role in (("inputTranscription","user"),("outputTranscription","assistant")):
                    text=content.get(name,{}).get("text")
                    if text:self.emit("live",{"type":"transcript","role":role,"text":text[:8000]})
                for part in content.get("modelTurn",{}).get("parts",[]):
                    audio=part.get("inlineData",{})
                    if audio.get("mimeType","").startswith("audio/pcm") and len(audio.get("data",""))<500000:
                        self.emit("live",{"type":"audio","data":audio["data"]})
                if content.get("turnComplete"):self.emit("live",{"type":"turn_complete"})
                if packet.get("goAway"):break
        except Exception:pass
        return handle

    async def audio(self,data):
        sock=self.socket
        if not sock:return {"accepted":False}
        raw=base64.b64decode(data,validate=True)
        if not 320<=len(raw)<=12800 or len(raw)%2:raise ValueError("Unsupported PCM chunk")
        self.frames+=1
        if self.frames>max(30,(time.monotonic()-self.started)*32):raise ValueError("Audio rate exceeded")
        try:
            await sock.send(json.dumps({"realtimeInput":{"audio":{"mimeType":"audio/pcm;rate=16000","data":data}}}))
        except ConnectionClosed:
            return {"accepted":False}
        return {"accepted":True}

    async def text(self,text):
        if not self.socket:raise GeminiError("LIVE_DISCONNECTED","Start Live first.")
        await self.socket.send(json.dumps({"realtimeInput":{"text":text}}));return {"accepted":True}

    async def mic_end(self):
        if self.socket:
            await self.socket.send(json.dumps({"realtimeInput":{"audioStreamEnd":True}}))
        return {"accepted":True}

    async def stop(self):
        self.always_on = False
        sock=self.socket;self.socket=None
        active=bool(sock or self.receiver or self.starting)
        current=asyncio.current_task()
        waiting=[t for t in (self.starting,self.receiver) if t and t is not current and not t.done()]
        for task in waiting:task.cancel()
        if waiting:await asyncio.gather(*waiting,return_exceptions=True)
        self.receiver=None
        if sock:
            try:await sock.close()
            except Exception:pass
        if active:
            self.emit("live",{"type":"closed","message":"Live stopped. Microphone off."})
        return {"connected":False}
