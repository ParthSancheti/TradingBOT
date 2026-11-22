"""Local byte streams: user-only Windows named pipes or private macOS Unix sockets.

Never deserialize pickle or listen on a network port. JSON framing and HMAC are
implemented above this transport layer.
"""
import hashlib
import os
import socket


def pipe_name(path):
    return r"\\.\pipe\JarvisTrade-"+hashlib.sha256(str(path).encode()).hexdigest()[:32]


def cancel_pipe(handle):
    import ctypes
    from ctypes import wintypes
    cancel=ctypes.WinDLL("kernel32",use_last_error=True).CancelIoEx
    cancel.argtypes=[wintypes.HANDLE,wintypes.LPVOID];cancel.restype=wintypes.BOOL
    # Cancellation is completed/observed by the thread that owns the OVERLAPPED.
    cancel(int(handle),None)


class SocketStream:
    def __init__(self,conn): self.conn=conn;self.stream=conn.makefile("rwb",buffering=0)
    def read(self,n): return self.stream.read(n)
    def write(self,value): return self.stream.write(value)
    def flush(self): self.stream.flush()
    def __enter__(self): return self
    def __exit__(self,*args): self.stream.close();self.conn.close()


class UnixListener:
    def __init__(self,path):
        self.path=path;self.handle=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try: self.handle.bind(str(path))
        except OSError:
            self.handle.close();raise RuntimeError("Bridge endpoint is occupied; stop the old instance before recovery") from None
        path.chmod(0o600);self.handle.listen(2);self.handle.settimeout(.5)
    def accept(self):
        conn,_=self.handle.accept();conn.settimeout(30);return SocketStream(conn)
    def reset(self): pass
    def close(self): self.handle.close()
    def cleanup(self): self.path.unlink(missing_ok=True)


class PipeStream:
    def __init__(self,handle,timeout=30,server=False): self.handle,self.timeout,self.server=handle,timeout,server
    def complete(self,overlapped):
        import win32event,win32file
        if win32event.WaitForSingleObject(overlapped.hEvent,int(self.timeout*1000))!=win32event.WAIT_OBJECT_0:
            cancel_pipe(self.handle)
            try: win32file.GetOverlappedResult(self.handle,overlapped,True)
            except Exception: pass
            raise TimeoutError("Local pipe deadline exceeded")
        return win32file.GetOverlappedResult(self.handle,overlapped,False)
    def io(self,value,reading):
        import pywintypes,win32event,win32file
        overlapped=pywintypes.OVERLAPPED();overlapped.hEvent=win32event.CreateEvent(None,True,False,None)
        buffer=win32file.AllocateReadBuffer(value) if reading else value
        try:
            try:
                (win32file.ReadFile if reading else win32file.WriteFile)(self.handle,buffer,overlapped)
                count=self.complete(overlapped)
            except pywintypes.error as error:
                if reading and error.winerror in (109,232,233): return b""
                raise OSError("Local pipe operation failed") from None
            return bytes(buffer[:count]) if reading else count
        finally: overlapped.hEvent.Close()
    def read(self,n): return self.io(n,True)
    def write(self,value):
        if self.io(value,False)!=len(value): raise OSError("Short pipe write")
    def flush(self): pass
    def __enter__(self): return self
    def __exit__(self,*args):
        if self.server:
            import win32pipe
            try: win32pipe.DisconnectNamedPipe(self.handle)
            except Exception: pass
        else: self.handle.Close()


class WindowsListener:
    def __init__(self,path):
        import pywintypes,win32api,win32con,win32pipe,win32security
        token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
        try: sid=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
        finally: token.Close()
        descriptor=win32security.ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;GA;;;"+win32security.ConvertSidToStringSid(sid)+")",1)
        attributes=pywintypes.SECURITY_ATTRIBUTES();attributes.SECURITY_DESCRIPTOR=descriptor
        try:
            self.handle=win32pipe.CreateNamedPipe(pipe_name(path),win32pipe.PIPE_ACCESS_DUPLEX|win32con.FILE_FLAG_OVERLAPPED|0x00080000,
                win32pipe.PIPE_TYPE_BYTE|win32pipe.PIPE_READMODE_BYTE|0x00000008,1,262144,262144,30000,attributes)
        except pywintypes.error: raise RuntimeError("Bridge pipe is occupied or permission was denied") from None
    def accept(self):
        import pywintypes,win32event,win32pipe
        overlapped=pywintypes.OVERLAPPED();overlapped.hEvent=win32event.CreateEvent(None,True,False,None)
        try:
            result=win32pipe.ConnectNamedPipe(self.handle,overlapped)
            if result!=535: PipeStream(self.handle).complete(overlapped)
        except pywintypes.error: raise OSError("Local pipe connection ended") from None
        finally: overlapped.hEvent.Close()
        return PipeStream(self.handle,server=True)
    def reset(self):
        import win32pipe
        try: win32pipe.DisconnectNamedPipe(self.handle)
        except Exception: pass
    def close(self):
        cancel_pipe(self.handle)
    def cleanup(self): self.handle.Close()


def listen(path): return WindowsListener(path) if os.name=="nt" else UnixListener(path)


def connect(path,timeout=180):
    if os.name=="nt":
        import pywintypes,time,win32con,win32file,win32pipe
        pname = pipe_name(path)
        # One pipe instance serves requests one at a time; wait long enough
        # for a slow observation batch instead of failing the native host.
        deadline = time.monotonic() + 10.0
        last_err = None
        while True:
            try:
                win32pipe.WaitNamedPipe(pname, 1000)
                handle = win32file.CreateFile(
                    pname,
                    win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                    0,
                    None,
                    win32con.OPEN_EXISTING,
                    win32con.FILE_FLAG_OVERLAPPED | 0x00100000, # SECURITY_SQOS_PRESENT + anonymous impersonation
                    None,
                )
                return PipeStream(handle, timeout)
            except pywintypes.error as err:
                last_err = err
                if getattr(err, "winerror", None) in (2, 231) and time.monotonic() < deadline:
                    time.sleep(0.06)
                    continue
                raise OSError("Local bridge is unavailable") from None
            except Exception:
                if time.monotonic() < deadline and isinstance(last_err, pywintypes.error):
                    time.sleep(0.06)
                    continue
                raise OSError("Local bridge is unavailable") from None
    conn=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);conn.settimeout(timeout)
    try: conn.connect(str(path))
    except Exception: conn.close();raise
    return SocketStream(conn)
