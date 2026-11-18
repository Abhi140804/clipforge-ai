"""Password hashing, session tokens, secret encryption, rate limiting, SSRF guard."""
import base64
import hashlib
import hmac
import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urlparse

import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import cfg

SESSION_TTL = 7 * 24 * 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode())


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${_b64(salt)}${_b64(h)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        scheme, s, h = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=_unb64(s), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc, _unb64(h))
    except Exception:
        return False


def make_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + SESSION_TTL}, cfg.SECRET_KEY, algorithm="HS256")


def read_token(token: str):
    try:
        data = jwt.decode(token, cfg.SECRET_KEY, algorithms=["HS256"])
        return int(data["sub"])
    except Exception:
        return None


_fernet = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        key = hashlib.sha256(("clipforge-enc:" + cfg.SECRET_KEY).encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


def encrypt(s: str) -> str:
    return _f().encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    try:
        return _f().decrypt(s.encode()).decode()
    except InvalidToken:
        return ""


class RateLimiter:
    """Simple in-memory sliding window limiter."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            arr = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(arr) >= self.limit:
                self.hits[key] = arr
                return False
            arr.append(now)
            self.hits[key] = arr
            return True


def is_public_host(host: str) -> bool:
    """SSRF guard: every address the host resolves to must be globally routable."""
    if cfg.ALLOW_PRIVATE_URLS:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


def check_url(url: str) -> str:
    """Return '' if url is acceptable, else an error message."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return "Invalid URL."
    if u.scheme not in ("http", "https"):
        return "Only http(s) URLs are supported."
    if not u.hostname:
        return "URL has no host."
    if u.username or u.password:
        return "URLs with embedded credentials are not allowed."
    if not is_public_host(u.hostname):
        return "URL points to a private or unresolvable address."
    return ""


def safe_filename(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in os.path.basename(name))
    return keep[:120] or "file"
