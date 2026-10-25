"""RPC client factory with endpoint failover. Read-only: this module never holds or uses a signing key.

Public Base endpoints rate-limit (429) or block (403) under sustained load; rotate() moves the client to the
next endpoint in settings.rpc_endpoints and the caller retries the block."""
import logging

from web3 import AsyncHTTPProvider, AsyncWeb3

from mev.config import settings

log = logging.getLogger("mev.rpc")
_index = 0


def endpoints() -> list[str]:
    seen, out = set(), []
    for u in [settings.rpc_http] + [x.strip() for x in settings.rpc_http_fallbacks.split(",") if x.strip()]:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def make_web3() -> AsyncWeb3:
    provider = AsyncHTTPProvider(endpoints()[_index % len(endpoints())], request_kwargs={"timeout": settings.rpc_timeout_s})
    return AsyncWeb3(provider)


def current(w3: AsyncWeb3) -> str:
    return str(w3.provider.endpoint_uri)


def redacted(url: str) -> str:
    """Host only: provider URLs carry the API key in the path and must not reach the logs."""
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return "rpc"


def rotate(w3: AsyncWeb3) -> str:
    """Point the live client at the next endpoint (web3's HTTP provider reads endpoint_uri per request)."""
    global _index
    eps = endpoints()
    if len(eps) < 2:
        return current(w3)
    _index = (eps.index(current(w3)) + 1) % len(eps) if current(w3) in eps else 0
    w3.provider.endpoint_uri = eps[_index]
    log.warning("rpc endpoint rotated to %s", redacted(eps[_index]))
    return eps[_index]


THROTTLE_CODES = {-32001, -32005, -32029, -32090, 429}
THROTTLE_WORDS = ("rate limit", "rate-limit", "usage limit", "too many requests", "quota", "exceeded", "throttl", "forbidden", "429")


def is_throttle(e: BaseException | None) -> bool:
    """HTTP 403/429/5xx, or a JSON-RPC error whose code/message says the endpoint is rationing us."""
    if e is None:
        return False
    status = getattr(e, "status", None) or getattr(e, "status_code", None)
    if status in (403, 429, 502, 503):
        return True
    rpc_code = getattr(e, "rpc_response", None)
    if isinstance(rpc_code, dict):
        err = rpc_code.get("error") or {}
        if isinstance(err, dict) and err.get("code") in THROTTLE_CODES:
            return True
    text = str(e).lower()
    return any(w in text for w in THROTTLE_WORDS)
