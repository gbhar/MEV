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
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"name": "tickSpacing", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int24"}]},
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

# Aerodrome Slipstream QuoterV2: same shape, but the tier field is int24 tickSpacing (changes the selector).
SLIPSTREAM_QUOTER_V2 = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "amountIn", "type": "uint256"}, {"name": "tickSpacing", "type": "int24"},
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

# FlashArb (contracts/src/FlashArb.sol): the execution contract. Generated from the forge artifact; regenerate with
# `make contract-abi` after changing the contract.
FLASH_ARB = [
 {
  "type": "function",
  "name": "aave",
  "inputs": [],
  "outputs": [
   {
    "name": "",
    "type": "address",
    "internalType": "contract IAaveV3Pool"
   }
  ],
  "stateMutability": "view"
 },
 {
  "type": "function",
  "name": "execute",
  "inputs": [
   {
    "name": "p",
    "type": "tuple",
    "internalType": "struct FlashArb.Params",
    "components": [
     {
      "name": "lender",
      "type": "uint8",
      "internalType": "enum FlashArb.Lender"
     },
     {
      "name": "quote",
      "type": "address",
      "internalType": "address"
     },
     {
      "name": "amount",
      "type": "uint256",
      "internalType": "uint256"
     },
     {
      "name": "buy",
      "type": "tuple",
      "internalType": "struct FlashArb.Leg",
      "components": [
       {
        "name": "pool",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "kind",
        "type": "uint8",
        "internalType": "enum FlashArb.Kind"
       },
       {
        "name": "tokenIn",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "tokenOut",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "feePpm",
        "type": "uint24",
        "internalType": "uint24"
       }
      ]
     },
     {
      "name": "sell",
      "type": "tuple",
      "internalType": "struct FlashArb.Leg",
      "components": [
       {
        "name": "pool",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "kind",
        "type": "uint8",
        "internalType": "enum FlashArb.Kind"
       },
       {
        "name": "tokenIn",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "tokenOut",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "feePpm",
        "type": "uint24",
        "internalType": "uint24"
       }
      ]
     },
     {
      "name": "minProfit",
      "type": "uint256",
      "internalType": "uint256"
     },
     {
      "name": "deadlineBlock",
      "type": "uint256",
      "internalType": "uint256"
     }
    ]
   }
  ],
  "outputs": [
   {
    "name": "profit",
    "type": "uint256",
    "internalType": "uint256"
   }
  ],
  "stateMutability": "nonpayable"
 },
 {
  "type": "function",
  "name": "executeRoute",
  "inputs": [
   {
    "name": "p",
    "type": "tuple",
    "internalType": "struct FlashArb.RouteParams",
    "components": [
     {
      "name": "lender",
      "type": "uint8",
      "internalType": "enum FlashArb.Lender"
     },
     {
      "name": "quote",
      "type": "address",
      "internalType": "address"
     },
     {
      "name": "amount",
      "type": "uint256",
      "internalType": "uint256"
     },
     {
      "name": "legs",
      "type": "tuple[]",
      "internalType": "struct FlashArb.Leg[]",
      "components": [
       {
        "name": "pool",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "kind",
        "type": "uint8",
        "internalType": "enum FlashArb.Kind"
       },
       {
        "name": "tokenIn",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "tokenOut",
        "type": "address",
        "internalType": "address"
       },
       {
        "name": "feePpm",
        "type": "uint24",
        "internalType": "uint24"
       }
      ]
     },
     {
      "name": "minProfit",
      "type": "uint256",
      "internalType": "uint256"
     },
     {
      "name": "deadlineBlock",
      "type": "uint256",
      "internalType": "uint256"
     }
    ]
   }
  ],
  "outputs": [
   {
    "name": "profit",
    "type": "uint256",
    "internalType": "uint256"
   }
  ],
  "stateMutability": "nonpayable"
 },
 {
  "type": "function",
  "name": "morpho",
  "inputs": [],
  "outputs": [
   {
    "name": "",
    "type": "address",
    "internalType": "contract IMorpho"
   }
  ],
  "stateMutability": "view"
 },
 {
  "type": "function",
  "name": "owner",
  "inputs": [],
  "outputs": [
   {
    "name": "",
    "type": "address",
    "internalType": "address"
   }
  ],
  "stateMutability": "view"
 },
 {
  "type": "function",
  "name": "paused",
  "inputs": [],
  "outputs": [
   {
    "name": "",
    "type": "bool",
    "internalType": "bool"
   }
  ],
  "stateMutability": "view"
 },
 {
  "type": "function",
  "name": "rescue",
  "inputs": [
   {
    "name": "token",
    "type": "address",
    "internalType": "address"
   },
   {
    "name": "to",
    "type": "address",
    "internalType": "address"
   },
   {
    "name": "amount",
    "type": "uint256",
    "internalType": "uint256"
   }
  ],
  "outputs": [],
  "stateMutability": "nonpayable"
 },
 {
  "type": "function",
  "name": "setOwner",
  "inputs": [
   {
    "name": "newOwner",
    "type": "address",
    "internalType": "address"
   }
  ],
  "outputs": [],
  "stateMutability": "nonpayable"
 },
 {
  "type": "function",
  "name": "setPaused",
  "inputs": [
   {
    "name": "p",
    "type": "bool",
    "internalType": "bool"
   }
  ],
  "outputs": [],
  "stateMutability": "nonpayable"
 },
 {
  "type": "event",
  "name": "Executed",
  "inputs": [
   {
    "name": "quote",
    "type": "address",
    "indexed": True,
    "internalType": "address"
   },
   {
    "name": "amount",
    "type": "uint256",
    "indexed": False,
    "internalType": "uint256"
   },
   {
    "name": "repay",
    "type": "uint256",
    "indexed": False,
    "internalType": "uint256"
   },
   {
    "name": "profit",
    "type": "uint256",
    "indexed": False,
    "internalType": "uint256"
   },
   {
    "name": "lender",
    "type": "uint8",
    "indexed": False,
    "internalType": "enum FlashArb.Lender"
   }
  ],
  "anonymous": False
 },
 {
  "type": "event",
  "name": "OwnerChanged",
  "inputs": [
   {
    "name": "previousOwner",
    "type": "address",
    "indexed": True,
    "internalType": "address"
   },
   {
    "name": "newOwner",
    "type": "address",
    "indexed": True,
    "internalType": "address"
   }
  ],
  "anonymous": False
 },
 {
  "type": "event",
  "name": "PausedSet",
  "inputs": [
   {
    "name": "paused",
    "type": "bool",
    "indexed": False,
    "internalType": "bool"
   }
  ],
  "anonymous": False
 },
 {
  "type": "event",
  "name": "RouteExecuted",
  "inputs": [
   {
    "name": "quote",
    "type": "address",
    "indexed": True,
    "internalType": "address"
   },
   {
    "name": "amount",
    "type": "uint256",
    "indexed": False,
    "internalType": "uint256"
   },
   {
    "name": "repay",
    "type": "uint256",
    "indexed": False,
    "internalType": "uint256"
   },
   {
    "name": "profit",
    "type": "uint256",
    "indexed": False,
    "internalType": "uint256"
   },
   {
    "name": "lender",
    "type": "uint8",
    "indexed": False,
    "internalType": "enum FlashArb.Lender"
   },
   {
    "name": "legs",
    "type": "uint8",
    "indexed": False,
    "internalType": "uint8"
   }
  ],
  "anonymous": False
 },
 {
  "type": "error",
  "name": "BadCallback",
  "inputs": []
 },
 {
  "type": "error",
  "name": "BadRoute",
  "inputs": []
 },
 {
  "type": "error",
  "name": "Expired",
  "inputs": [
   {
    "name": "deadline",
    "type": "uint256",
    "internalType": "uint256"
   },
   {
    "name": "current",
    "type": "uint256",
    "internalType": "uint256"
   }
  ]
 },
 {
  "type": "error",
  "name": "InsufficientProfit",
  "inputs": [
   {
    "name": "got",
    "type": "uint256",
    "internalType": "uint256"
   },
   {
    "name": "want",
    "type": "uint256",
    "internalType": "uint256"
   }
  ]
 },
 {
  "type": "error",
  "name": "IsPaused",
  "inputs": []
 },
 {
  "type": "error",
  "name": "LenderUnavailable",
  "inputs": []
 },
 {
  "type": "error",
  "name": "NotOwner",
  "inputs": []
 },
 {
  "type": "error",
  "name": "TransferFailed",
  "inputs": []
 },
 {
  "type": "error",
  "name": "ZeroAddress",
  "inputs": []
 }
]
