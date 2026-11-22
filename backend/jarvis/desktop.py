"""Paired local IPC bridge. The shared secret stays in the OS vault."""
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from .bridge import read_frame,write_frame
from .sheets import canonical,signature
from . import ipc


def runtime_directory():
    base=Path(os.environ.get("LOCALAPPDATA",str(Path.home()/"Library"/"Application Support")))
    path=base/"JarvisTrade"
    path.mkdir(parents=True,exist_ok=True)
    if os.name!="nt": path.chmod(0o700)
    return path


def owner_avatar_path():
    return runtime_directory() / "owner-avatar.jpg"


def save_owner_avatar_bytes(raw_bytes):
    if not raw_bytes:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        w, h = img.size
        side = min(w, h)
        if side > 0:
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
        img = img.resize((120, 120), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=86, optimize=True)
        jpeg_bytes = out.getvalue()
        target = owner_avatar_path()
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(jpeg_bytes)
        os.replace(tmp, target)
        return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
    except Exception:
        return None


def delete_owner_avatar():
    try:
        owner_avatar_path().unlink(missing_ok=True)
    except OSError:
        pass


async def download_whatsapp_avatar(avatar_url):
    if not isinstance(avatar_url, str) or len(avatar_url) > 2048:
        return None
    parsed = urlparse(avatar_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host.endswith(".whatsapp.net") or host.endswith(".fbcdn.net") or host == "web.whatsapp.com"
    ):
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3), follow_redirects=False) as client:
            resp = await client.get(avatar_url, headers={"Referer": "https://web.whatsapp.com/", "User-Agent": "Mozilla/5.0"})
            if not resp.is_success or len(resp.content) > 1_500_000:
                return None
            return save_owner_avatar_bytes(resp.content)
    except Exception:
        return None


def is_chrome_running():
    try:
        if sys.platform == "win32":
            # Only count Chrome instances that own an actual visible browser window
            # (background chrome.exe GPU/extension helpers keep running after closing Chrome).
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found = False
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def callback(hwnd, _lparam):
                nonlocal found
                if user32.IsWindowVisible(hwnd):
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    if cls.value == "Chrome_WidgetWin_1" and user32.GetWindowTextLengthW(hwnd) > 0:
                        found = True
                        return False
                return True

            user32.EnumWindows(enum_proc(callback), 0)
            return found
        if sys.platform == "darwin":
            res = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, timeout=3)
            return res.returncode == 0
    except Exception:
        pass
    return False


def open_whatsapp(single_url="https://web.whatsapp.com/", *, force_url=False):
    # When no visible Chrome window is open (or force_url is requested because no extension
    # session is verified yet), launch Chrome straight onto https://web.whatsapp.com/.
    already_open = is_chrome_running()
    open_url = force_url or not already_open
    if sys.platform=="darwin":
        cmd = ["/usr/bin/open", "-a", "Google Chrome"] + ([single_url] if open_url else [])
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform=="win32":
        candidates=[Path(os.environ.get("PROGRAMFILES","C:/Program Files"))/"Google/Chrome/Application/chrome.exe",Path(os.environ.get("LOCALAPPDATA",""))/"Google/Chrome/Application/chrome.exe",Path(os.environ.get("PROGRAMFILES(X86)","C:/Program Files (x86)"))/"Google/Chrome/Application/chrome.exe"]
        exe=next((p for p in candidates if p.is_file()),None)
        if not exe: raise RuntimeError("Google Chrome was not found")
        if open_url:
            cmd = [str(exe)] + ([] if already_open else ["--new-window"]) + [single_url]
        else:
            cmd = [str(exe)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    else: raise RuntimeError("This launcher targets Windows and macOS")


class LocalBridge:
    def __init__(self,vault,handler):
        self.vault,self.handler=vault,handler;self.ready=False;self.last_seen=0;self.stop_event=threading.Event();self.listener=None
        self.path=runtime_directory()/"bridge.sock";self._lock=threading.Lock();self.thread=None

    def start(self):
        with self._lock: self._start()

    def _start(self):
        secret=self.vault.get("bridge_secret")
        if not secret: raise RuntimeError("Install and pair the native helper first")
        if self.ready and self.thread and self.thread.is_alive(): return
        # A serve thread that died (pipe error, client race) left ready=True and
        # the extension saw "Desktop app disconnected" forever. Restart it.
        self.ready=False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
            if self.thread and self.thread.is_alive():
                raise RuntimeError("Bridge is still finishing its previous request")
        if self.listener:
            self.listener.cleanup();self.listener=None
        self.stop_event.clear();self.last_seen=0
        self.listener=ipc.listen(self.path);self.ready=True
        self.thread=threading.Thread(target=self._serve,name="jarvis-local-bridge",daemon=True);self.thread.start()

    @property
    def alive(self):
        return bool(self.ready and self.thread and self.thread.is_alive())

    def ensure_running(self):
        """Watchdog: restart the listener if its thread stopped unexpectedly."""
        if self.ready and not (self.thread and self.thread.is_alive()) and not self.stop_event.is_set():
            try: self.start()
            except Exception: return False
        return self.alive

    def _recover_listener(self, failures):
        try: self.listener.reset()
        except Exception: pass
        if failures >= 5:
            try: self.listener.close()
            except Exception: pass
            try: self.listener.cleanup()
            except Exception: pass
            try: self.listener=ipc.listen(self.path); return 0
            except Exception: pass
        time.sleep(min(1.0, 0.1*failures))
        return failures

    def _serve(self):
        failures=0
        while not self.stop_event.is_set():
            try:
                stream=self.listener.accept();failures=0
            except socket.timeout: continue
            except OSError:
                # A client that connects and disconnects before the server
                # accepts it raises here. Recover instead of ending the bridge.
                if self.stop_event.is_set(): break
                failures=self._recover_listener(failures+1)
                continue
            try:
                with stream:
                    challenge=secrets.token_urlsafe(32);write_frame(stream,{"challenge":challenge})
                    envelope=read_frame(stream);secret=self.vault.get("bridge_secret")
                    body=envelope.get("body",{});proof=envelope.get("signature","")
                    if not hmac.compare_digest(proof,signature(secret,{"challenge":challenge,"body":body})): continue
                    response=self.handler(body);self.last_seen=time.time()
                    packet={"challenge":challenge,"result":response};packet["signature"]=signature(secret,packet)
                    write_frame(stream,packet)
                    # Do not discard a pipe's unread output by disconnecting too soon.
                    if read_frame(stream).get("ack")!=challenge: raise ValueError("Missing delivery acknowledgement")
            except Exception:
                # No payloads, secrets or contacts in transport logs.
                continue

    def stop(self):
        self.stop_event.set()
        if self.listener:
            self.listener.close()
            if self.thread: self.thread.join(timeout=3)
            if not self.thread or not self.thread.is_alive():
                self.listener.cleanup();self.listener=None
        self.ready=False


def local_call(vault,body):
    secret=vault.get("bridge_secret")
    if not secret: raise RuntimeError("Device is not enrolled")
    last_error = None
    for attempt in range(3):
        try:
            with ipc.connect(runtime_directory()/"bridge.sock") as stream:
                challenge=read_frame(stream)["challenge"]
                write_frame(stream,{"body":body,"signature":signature(secret,{"challenge":challenge,"body":body})})
                response=read_frame(stream);proof=response.pop("signature","")
                if response.get("challenge")!=challenge or not hmac.compare_digest(proof,signature(secret,response)): raise ValueError("Local response was not verified")
                write_frame(stream,{"ack":challenge})
                return response["result"]
        except (OSError, TimeoutError) as err:
            last_error = err
            if attempt < 2:
                time.sleep(0.08 * (attempt + 1))
                continue
            raise
    if last_error:
        raise last_error


def find_native_executable():
    exe_name = "jarvis-native.exe" if os.name == "nt" else "jarvis-native"
    root = Path(__file__).resolve().parents[2]
    local_appdata = Path(os.environ.get("LOCALAPPDATA", "")) if os.name == "nt" else Path.home() / "Library/Application Support"
    candidates = [
        Path(sys.executable).parent / "resources" / "jarvis-native" / exe_name,
        Path(sys.executable).parent / exe_name,
        root / "release" / "Jarvis-Trade-Production-Package" / "Jarvis Trade" / "resources" / "jarvis-native" / exe_name,
        local_appdata / "Programs" / "Jarvis Trade" / "resources" / "jarvis-native" / exe_name,
        root / "artifacts" / "native" / "jarvis-native" / exe_name,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    import shutil
    which_path = shutil.which("jarvis-native")
    return Path(which_path).resolve() if which_path else None


def auto_detect_extension_ids():
    detected = set()
    user_data_dirs = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if local.is_dir():
            user_data_dirs.append(local / "Google" / "Chrome" / "User Data")
            user_data_dirs.append(local / "Microsoft" / "Edge" / "User Data")
    else:
        home = Path.home()
        user_data_dirs.append(home / "Library" / "Application Support" / "Google" / "Chrome")
        user_data_dirs.append(home / "Library" / "Application Support" / "Microsoft Edge")

    for root_dir in user_data_dirs:
        if not root_dir.is_dir():
            continue
        pref_files = (
            list(root_dir.glob("*/Secure Preferences"))
            + list(root_dir.glob("*/Preferences"))
            + list(root_dir.glob("Secure Preferences"))
            + list(root_dir.glob("Preferences"))
        )
        for p in pref_files:
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                settings = data.get("extensions", {}).get("settings", {})
                for ext_id, info in settings.items():
                    if not re.fullmatch(r"[a-p]{32}", ext_id):
                        continue
                    path_str = str(info.get("path", ""))
                    manifest_name = str(info.get("manifest", {}).get("name", "")).lower()

                    is_jarvis = "jarvis" in manifest_name
                    if not is_jarvis and path_str:
                        mf_path = Path(path_str) / "manifest.json"
                        if mf_path.is_file():
                            try:
                                mf = json.loads(mf_path.read_text(encoding="utf-8", errors="ignore"))
                                if "jarvis" in str(mf.get("name", "")).lower():
                                    is_jarvis = True
                            except Exception:
                                pass
                    if is_jarvis:
                        detected.add(ext_id)
            except Exception:
                pass
    return sorted(detected)


def register_native_host(extension_id, executable=None, vault=None):
    if executable is None:
        executable = find_native_executable()
    if not executable:
        raise ValueError("Build the native helper executable first")
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ValueError("Build the native helper executable first")

    id_list = []
    if isinstance(extension_id, (list, tuple, set)):
        id_list = list(extension_id)
    elif isinstance(extension_id, str):
        id_list = [s.strip() for s in extension_id.replace(",", " ").split() if s.strip()]

    valid_ids = []
    for eid in id_list:
        clean = str(eid).strip()
        if re.fullmatch(r"[a-p]{32}", clean) and clean not in valid_ids:
            valid_ids.append(clean)

    if not valid_ids:
        raise ValueError("Invalid Chrome extension ID")

    allowed_origins = [f"chrome-extension://{eid}/" for eid in valid_ids]
    manifest = {
        "name": "com.jarvis.trade",
        "description": "Jarvis-Trade authenticated local bridge",
        "path": str(executable),
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }

    if sys.platform == "win32":
        import winreg
        path = runtime_directory() / "com.jarvis.trade.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\NativeMessagingHosts\com.jarvis.trade") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Edge\NativeMessagingHosts\com.jarvis.trade") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))
        except Exception:
            pass
    elif sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.jarvis.trade.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        path.chmod(0o600)
    else:
        raise RuntimeError("Unsupported platform")

    if vault is None:
        from .secrets import SecretVault
        vault = SecretVault()
    vault.set("extension_id", ",".join(valid_ids))
    if not vault.get("bridge_secret"):
        vault.set("bridge_secret", secrets.token_urlsafe(48))
    return str(path)


def sync_native_registration(vault=None):
    if vault is None:
        from .secrets import SecretVault
        vault = SecretVault()

    existing = vault.get("extension_id") or ""
    current_ids = [s.strip() for s in str(existing).replace(",", " ").split() if s.strip()]

    known_default = "afbnhhklkdgmljgfcmmdedkndolmkgnd"
    if known_default not in current_ids and re.fullmatch(r"[a-p]{32}", known_default):
        current_ids.append(known_default)

    detected = auto_detect_extension_ids()
    changed = False
    for d in detected:
        if d not in current_ids:
            current_ids.append(d)
            changed = True

    manifest_path = runtime_directory() / "com.jarvis.trade.json"
    if not manifest_path.exists():
        changed = True
    else:
        try:
            m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            m_origins = m_data.get("allowed_origins", [])
            for cid in current_ids:
                if f"chrome-extension://{cid}/" not in m_origins:
                    changed = True
                    break
        except Exception:
            changed = True

    exe = find_native_executable()
    manifest_str = str(manifest_path)
    if exe and (changed or not vault.get("bridge_secret")):
        manifest_str = register_native_host(current_ids, exe, vault=vault)

    return {
        "paired_ids": current_ids,
        "manifest_path": manifest_str,
        "executable": str(exe) if exe else None,
        "detected": detected,
    }


if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser();p.add_argument("--extension-id",required=True);p.add_argument("--native-executable",required=True);args=p.parse_args()
    print(register_native_host(args.extension_id,args.native_executable))
