from pathlib import Path
import socket
import pytest

from conftest import FakeVault
from jarvis.bridge import read_frame,write_frame
from jarvis.desktop import LocalBridge,local_call
from jarvis import desktop
from jarvis import ipc


def test_authenticated_ipc_restart_and_wrong_key(tmp_path,monkeypatch):
    # Real Windows named pipe or macOS Unix socket, not a mocked transport.
    monkeypatch.setattr(desktop,"runtime_directory",lambda:tmp_path)
    vault=FakeVault();vault.set("bridge_secret","test-device-secret-"*4)
    calls=[]
    def handle(body): calls.append(body);return {"ok":True,"can_send":False}
    bridge=LocalBridge(vault,handle)
    bridge.start()
    try:
        assert local_call(vault,{"operation":"hello"})["ok"]
        assert bridge.last_seen>0
        with ipc.connect(bridge.path,timeout=2) as stream:
                read_frame(stream)
                write_frame(stream,{"body":{"operation":"observations"},"signature":"0"*64})
                with pytest.raises(EOFError): read_frame(stream)
        assert len(calls)==1
    finally: bridge.stop()
    assert not bridge.path.exists()
    bridge.start()
    try: assert local_call(vault,{"operation":"hello"})["ok"]
    finally: bridge.stop()
    assert len(calls)==2


def test_occupied_endpoint_not_deleted(tmp_path,monkeypatch):
    monkeypatch.setattr(desktop,"runtime_directory",lambda:tmp_path)
    vault=FakeVault();vault.set("bridge_secret","test-device-secret-"*4)
    first=LocalBridge(vault,lambda body:{});first.start()
    bridge=LocalBridge(vault,lambda body:{})
    try:
        with pytest.raises(RuntimeError): bridge.start()
        bridge.stop()
        assert local_call(vault,{"operation":"hello"})=={}
    finally: first.stop()


def test_open_whatsapp_launches_single_url_when_chrome_closed(tmp_path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    launched = []

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(desktop, "Path", lambda *a: tmp_path if str(a[0]).endswith("Files") else __import__("pathlib").Path(*a))
    # Point directly to fake Chrome via PROGRAMFILES lookup
    prog = tmp_path / "Google" / "Chrome" / "Application"
    prog.mkdir(parents=True)
    exe = prog / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))

    monkeypatch.setattr(desktop, "is_chrome_running", lambda: False)
    desktop.open_whatsapp()
    assert launched[-1] == [str(exe), "--new-window", "https://web.whatsapp.com/"]

    monkeypatch.setattr(desktop, "is_chrome_running", lambda: True)
    desktop.open_whatsapp()
    assert launched[-1] == [str(exe)]

    desktop.open_whatsapp(force_url=True)
    assert launched[-1] == [str(exe), "https://web.whatsapp.com/"]


def test_multi_extension_origin_check():
    from jarvis.bridge import check_origin
    id1 = "a" * 32
    id2 = "b" * 32
    id3 = "c" * 32

    # Single string ID
    assert check_origin(f"chrome-extension://{id1}/", id1)
    assert not check_origin(f"chrome-extension://{id2}/", id1)

    # List of IDs
    assert check_origin(f"chrome-extension://{id1}/", [id1, id2])
    assert check_origin(f"chrome-extension://{id2}/", [id1, id2])
    assert not check_origin(f"chrome-extension://{id3}/", [id1, id2])

    # Comma-separated string of IDs
    assert check_origin(f"chrome-extension://{id1}/", f"{id1},{id2}")
    assert check_origin(f"chrome-extension://{id2}/", f"{id1}, {id2}")
    assert not check_origin(f"chrome-extension://{id3}/", f"{id1},{id2}")

    # JSON array string
    assert check_origin(f"chrome-extension://{id1}/", f'["{id1}", "{id2}"]')

    # Adversarial / malformed inputs
    assert not check_origin("", [id1])
    assert not check_origin(None, [id1])
    assert not check_origin(f"chrome-extension://{id1}", [id1])  # missing trailing slash
    assert not check_origin(f"https://chrome-extension://{id1}/", [id1])
    assert not check_origin(f"chrome-extension://{id1.upper()}/", [id1])  # uppercase
    assert not check_origin(f"chrome-extension://{'a'*31}/", ['a'*31])  # 31 chars
    assert not check_origin(f"chrome-extension://{'z'*32}/", ['z'*32])  # z is outside a-p


def test_register_native_host_multi_id(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "runtime_directory", lambda: tmp_path)
    fake_exe = tmp_path / "jarvis-native.exe"
    fake_exe.write_bytes(b"")
    fake_vault = FakeVault()
    monkeypatch.setattr("jarvis.secrets.SecretVault", lambda: fake_vault)

    # Windows registry bypass for unit test
    if desktop.sys.platform == "win32":
        class FakeWinreg:
            HKEY_CURRENT_USER = "HKCU"
            REG_SZ = 1
            @staticmethod
            def CreateKey(*args):
                class Ctx:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                return Ctx()
            @staticmethod
            def SetValueEx(*args): pass
        monkeypatch.setitem(desktop.sys.modules, "winreg", FakeWinreg)

    id1 = "a" * 32
    id2 = "b" * 32
    manifest_file = desktop.register_native_host([id1, id2], fake_exe)
    assert Path(manifest_file).is_file()

    import json
    data = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
    assert data["name"] == "com.jarvis.trade"
    assert data["path"] == str(fake_exe.resolve())
    assert data["allowed_origins"] == [f"chrome-extension://{id1}/", f"chrome-extension://{id2}/"]

    # Vault updated
    stored = fake_vault.get("extension_id")
    assert id1 in stored and id2 in stored
    assert fake_vault.get("bridge_secret") is not None

    # Invalid ID rejected
    with pytest.raises(ValueError):
        desktop.register_native_host(["invalid_short"], fake_exe)


def test_auto_detect_extension_ids(tmp_path, monkeypatch):
    import json
    fake_chrome = tmp_path / "Google" / "Chrome" / "User Data" / "Default"
    fake_chrome.mkdir(parents=True)
    pref_file = fake_chrome / "Secure Preferences"

    jarvis_id = "ajmiggblbcihebfhcajjlmlimenbneil"
    foreign_id = "abcdefghijklmnopabcdefghijklmnop"

    pref_data = {
        "extensions": {
            "settings": {
                jarvis_id: {
                    "manifest": {"name": "Jarvis-Trade Bridge"},
                    "path": str(tmp_path / "ext_jarvis"),
                },
                foreign_id: {
                    "manifest": {"name": "Other Random Plugin"},
                    "path": str(tmp_path / "ext_other"),
                },
            }
        }
    }
    pref_file.write_text(json.dumps(pref_data), encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(desktop.os, "name", "nt")
    detected = desktop.auto_detect_extension_ids()
    assert jarvis_id in detected
    assert foreign_id not in detected

