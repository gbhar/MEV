"""Minimal ABIs: only the view functions the indexer needs."""
ERC20 = [
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
]

UNISWAP_V3_POOL = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
                 {"type": "uint16"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "uint8"}, {"type": "bool"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "tickBitmap", "type": "function", "stateMutability": "view", "inputs": [{"name": "wordPosition", "type": "int16"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "ticks", "type": "function", "stateMutability": "view", "inputs": [{"name": "tick", "type": "int24"}],
     "outputs": [{"name": "liquidityGross", "type": "uint128"}, {"name": "liquidityNet", "type": "int128"},
                 {"name": "feeGrowthOutside0X128", "type": "uint256"}, {"name": "feeGrowthOutside1X128", "type": "uint256"},
                 {"name": "tickCumulativeOutside", "type": "int56"}, {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
                 {"name": "secondsOutside", "type": "uint32"}, {"name": "initialized", "type": "bool"}]},
    {"name": "Swap", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "sender", "type": "address"}, {"indexed": True, "name": "recipient", "type": "address"},
        {"indexed": False, "name": "amount0", "type": "int256"}, {"indexed": False, "name": "amount1", "type": "int256"},
        {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"}, {"indexed": False, "name": "liquidity", "type": "uint128"},
        {"indexed": False, "name": "tick", "type": "int24"}]},
]

CONSTANT_PRODUCT_POOL = [
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
]

UNISWAP_V3_QUOTER_V2 = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                 {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}]},
]

# Multicall3 (same address on every chain): many view calls in one eth_call, all at the same block.
MULTICALL3 = [
    {"name": "aggregate3", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "calls", "type": "tuple[]", "components": [
         {"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"}, {"name": "callData", "type": "bytes"}]}],
     "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
         {"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}]}]},
]
