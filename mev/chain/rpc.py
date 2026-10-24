"""RPC client factory. Read-only: this module never holds or uses a signing key."""
from web3 import AsyncHTTPProvider, AsyncWeb3

from mev.config import settings


def make_web3() -> AsyncWeb3:
    provider = AsyncHTTPProvider(settings.rpc_http, request_kwargs={"timeout": settings.rpc_timeout_s})
    return AsyncWeb3(provider)
