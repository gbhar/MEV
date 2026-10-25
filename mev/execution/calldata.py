"""FlashArb.execute() parameters and their encoding. The contract takes the two legs exactly as the scanner sees
them (pool address, fork, token in/out) plus the loan size, the minimum profit and a deadline; see
contracts/src/FlashArb.sol for the struct layout this mirrors."""
from dataclasses import dataclass

from eth_utils import function_abi_to_4byte_selector
from web3 import Web3

from mev.chain import abi
from mev.pools.registry import Pool, Token

LENDER = {"aave_v3": 0, "morpho_blue": 1}
KIND_CL_UNISWAP, KIND_CL_PANCAKE, KIND_PAIR_V2, KIND_PAIR_AERODROME = 0, 1, 2, 3
_ERRORS = {function_abi_to_4byte_selector(e): e for e in abi.FLASH_ARB if e["type"] == "error"}
_ERROR_STRING = bytes.fromhex("08c379a0")   # Error(string)
ROUTE_SELECTOR = function_abi_to_4byte_selector(next(e for e in abi.FLASH_ARB if e["type"] == "function" and e["name"] == "executeRoute"))


def leg_kind(pool: Pool) -> int:
    if pool.kind == "uniswap_v3":
        return KIND_CL_PANCAKE if pool.variant == "pancake" else KIND_CL_UNISWAP
    return KIND_PAIR_AERODROME if pool.variant == "aerodrome" else KIND_PAIR_V2


def leg(pool: Pool, token_in: Token, token_out: Token) -> tuple:
    kind = leg_kind(pool)
    return (Web3.to_checksum_address(pool.address), kind, Web3.to_checksum_address(token_in.address),
            Web3.to_checksum_address(token_out.address), pool.fee_ppm if kind == KIND_PAIR_V2 else 0)


@dataclass(frozen=True)
class Params:
    lender: int
    quote: str
    amount: int
    buy: tuple
    sell: tuple
    min_profit: int
    deadline_block: int

    def as_tuple(self) -> tuple:
        return (self.lender, self.quote, self.amount, self.buy, self.sell, self.min_profit, self.deadline_block)


def build_params(buy_pool: Pool, sell_pool: Pool, amount_in: int, min_profit: int, deadline_block: int, lender: str) -> Params:
    """Borrow the pair's quote token, buy the base on buy_pool, sell it on sell_pool (the simulator's orientation)."""
    quote, base = buy_pool.quote, buy_pool.base
    if sell_pool.quote.key != quote.key or sell_pool.base.key != base.key:
        raise ValueError(f"{buy_pool.pool_id} and {sell_pool.pool_id} are not the same pair")
    return Params(LENDER[lender], Web3.to_checksum_address(quote.address), amount_in,
                  leg(buy_pool, quote, base), leg(sell_pool, base, quote), min_profit, deadline_block)


@dataclass(frozen=True)
class RouteParams:
    lender: int
    quote: str
    amount: int
    legs: tuple
    min_profit: int
    deadline_block: int

    def as_tuple(self) -> tuple:
        return (self.lender, self.quote, self.amount, list(self.legs), self.min_profit, self.deadline_block)


def build_route_params(pools: dict[str, Pool], route: tuple[str, ...], path: tuple[str, ...], amount_in: int, min_profit: int,
                       deadline_block: int, lender: str) -> RouteParams:
    """Legs follow the candidate's path through its pools; the first token of the path is borrowed."""
    if len(path) != len(route) + 1 or path[0] != path[-1]:
        raise ValueError("a route must start and end in the borrowed token")
    legs = []
    for pid, t_in, t_out in zip(route, path, path[1:]):
        pool = pools[pid]
        tokens = {pool.token0.key: pool.token0, pool.token1.key: pool.token1}
        if t_in not in tokens or t_out not in tokens:
            raise ValueError(f"{pid} does not hold {t_in} -> {t_out}")
        legs.append(leg(pool, tokens[t_in], tokens[t_out]))
    first = pools[route[0]]
    quote = first.token0 if first.token0.key == path[0] else first.token1
    return RouteParams(LENDER[lender], Web3.to_checksum_address(quote.address), amount_in, tuple(legs), min_profit, deadline_block)


def encode_execute_route(params: RouteParams) -> bytes:
    c = Web3().eth.contract(abi=abi.FLASH_ARB)
    return bytes.fromhex(c.encode_abi("executeRoute", args=[params.as_tuple()])[2:])


def encode_execute(params: Params) -> bytes:
    c = Web3().eth.contract(abi=abi.FLASH_ARB)
    return bytes.fromhex(c.encode_abi("execute", args=[params.as_tuple()])[2:])


def decode_profit(data: bytes | str) -> int:
    raw = bytes.fromhex(data[2:]) if isinstance(data, str) else bytes(data)
    return int.from_bytes(raw[:32], "big") if len(raw) >= 32 else 0


def decode_revert(data: bytes | str | None) -> str:
    """Human-readable revert: a FlashArb custom error with its arguments, Error(string), or the raw hex."""
    if not data:
        return "reverted without data"
    raw = bytes.fromhex(data[2:]) if isinstance(data, str) else bytes(data)
    if len(raw) < 4:
        return raw.hex() or "reverted without data"
    sel, body = raw[:4], raw[4:]
    codec = Web3().codec
    if sel in _ERRORS:
        e = _ERRORS[sel]
        try:
            args = codec.decode([i["type"] for i in e["inputs"]], body) if e["inputs"] else ()
        except Exception:
            args = ()
        return f"{e['name']}({', '.join(str(a) for a in args)})"
    if sel == _ERROR_STRING:
        try:
            return f"Error({codec.decode(['string'], body)[0]})"
        except Exception:
            pass
    return "0x" + raw.hex()
