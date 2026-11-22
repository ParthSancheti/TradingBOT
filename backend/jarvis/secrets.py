"""Secrets belong to the OS credential service, never to Google Sheets."""
import threading


class SecretVault:
    def __init__(self):
        self._lock = threading.Lock()
        self._backend_obj = None
        self._cache = {}

    def _backend(self):
        with self._lock:
            if self._backend_obj is not None:
                return self._backend_obj
            import keyring
            backend = keyring.get_keyring()
            if not backend.__class__.__module__.startswith(("keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService")):
                raise RuntimeError("A supported OS credential store is required; plaintext fallback is disabled")
            self._backend_obj = backend
            return backend

    def get(self, name):
        with self._lock:
            if name in self._cache:
                return self._cache[name]
        value = self._backend().get_password("Jarvis-Trade", name)
        with self._lock:
            self._cache[name] = value
        return value

    def set(self, name, value):
        if not isinstance(value, str) or len(value) > 16000:
            raise ValueError("Invalid secret")
        self._backend().set_password("Jarvis-Trade", name, value)
        with self._lock:
            self._cache[name] = value

