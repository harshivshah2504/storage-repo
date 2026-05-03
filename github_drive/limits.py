import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple


class RateLimitExceeded(RuntimeError):
    pass


_LOCK = threading.Lock()
_BUCKETS: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def check_rate_limit(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    if limit <= 0 or window_seconds <= 0:
        return
    now = time.time()
    cutoff = now - window_seconds
    bucket_key = (bucket, key or "anonymous")
    with _LOCK:
        hits = _BUCKETS[bucket_key]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            raise RateLimitExceeded("Too many attempts. Please wait a bit and try again.")
        hits.append(now)

