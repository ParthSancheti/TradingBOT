"""Native framing and enrolled extension handshake."""
import json
import os
import re
import struct
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FRAME = 256 * 1024


def exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count-len(chunks))
        if not chunk:
            if not chunks:
                raise EOFError
            raise ValueError("Truncated native frame")
        chunks.extend(chunk)
    return bytes(chunks)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def read_frame(stream):
    size = struct.unpack("=I", exact(stream, 4))[0]
    if size == 0 or size > MAX_FRAME:
        raise ValueError("Invalid native frame length")
    try:
        raw = exact(stream, size)
    except EOFError:
        raise ValueError("Truncated native payload") from None
    def invalid_constant(value):
        raise ValueError("Non-finite JSON value")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Object expected")
    return value


def write_frame(stream, value):
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise ValueError("Native frame too large")
    stream.write(struct.pack("=I", len(raw)))
    stream.write(raw)
    stream.flush()


class Hello(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal["jarvis.bridge.v1"]
    operation: Literal["hello"]
    sequence: Literal[1]
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")


def check_origin(origin, extension_id):
    if not origin or not isinstance(origin, str):
        return False
    if not origin.startswith("chrome-extension://") or not origin.endswith("/"):
        return False
    candidate = origin[len("chrome-extension://"):-1]
    if not re.fullmatch(r"[a-p]{32}", candidate):
        return False
    if not extension_id:
        return False

    allowed = set()
    if isinstance(extension_id, (list, tuple, set)):
        items = extension_id
    else:
        try:
            parsed = json.loads(extension_id)
            items = parsed if isinstance(parsed, list) else str(extension_id).replace(",", " ").split()
        except Exception:
            items = str(extension_id).replace(",", " ").split()

    for item in items:
        clean = str(item).strip()
        if re.fullmatch(r"[a-p]{32}", clean):
            allowed.add(clean)

    return candidate in allowed


def get_allowed_extension_ids(vault):
    ids = set()
    if vault:
        raw = vault.get("extension_id")
        if raw:
            try:
                parsed = json.loads(raw)
                items = parsed if isinstance(parsed, list) else str(raw).replace(",", " ").split()
            except Exception:
                items = str(raw).replace(",", " ").split()
            for s in items:
                clean = str(s).strip()
                if re.fullmatch(r"[a-p]{32}", clean):
                    ids.add(clean)
    try:
        from .desktop import runtime_directory
        manifest_path = runtime_directory() / "com.jarvis.trade.json"
        if manifest_path.is_file():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for origin in data.get("allowed_origins", []):
                if origin.startswith("chrome-extension://") and origin.endswith("/"):
                    eid = origin[len("chrome-extension://"):-1]
                    if re.fullmatch(r"[a-p]{32}", eid):
                        ids.add(eid)
    except Exception:
        pass
    return ids


def main():
    from .secrets import SecretVault
    from .desktop import local_call
    vault=SecretVault()
    allowed_ids=get_allowed_extension_ids(vault)
    if not check_origin(sys.argv[1] if len(sys.argv) > 1 else "", allowed_ids):
        raise SystemExit(2)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        hello = Hello.model_validate(read_frame(sys.stdin.buffer))
        result=local_call(vault,{"operation":"hello","session_id":hello.session_id})
        can_send = bool(result.get("can_send", False))
        write_frame(sys.stdout.buffer,{"protocol":hello.protocol,"operation":"hello_result","session_id":hello.session_id,**result,"max_frame":MAX_FRAME,"can_send":can_send})
        sequence=1
        while True:
            value=read_frame(sys.stdin.buffer)
            if value.get("session_id")!=hello.session_id or type(value.get("sequence")) is not int or value["sequence"]!=sequence+1 or value.get("operation") not in ("poll_config","observations","dispatch_claim","dispatch_authorize","dispatch_result","probe_result"):
                raise ValueError("Invalid bridge command")
            sequence=value["sequence"]
            try:
                result=local_call(vault,value)
            except (OSError, TimeoutError):
                result={"error":"Desktop app is starting or reconnecting. Retrying shortly."}
            write_frame(sys.stdout.buffer,{"sequence":sequence,"session_id":hello.session_id,**result})
    except (ValueError, EOFError, OSError, RuntimeError):
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
