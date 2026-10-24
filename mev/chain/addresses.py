"""Base mainnet addresses. All verified live via eth_getCode / factory lookups on 2026-10-24."""
from mev.pools.registry import Pool, Token

CHAIN_ID = 8453

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)

UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
UNISWAP_V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
AAVE_V3_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# V1 pool set. token0/token1 ordering is identical across these pools (WETH is token0),
# which the scanner relies on. Fees are parts-per-million (500 ppm = 5 bps).
POOLS: list[Pool] = [
    Pool("univ3_weth_usdc_5", "uniswap_v3", "uniswap_v3",
         "0xd0b53D9277642d899DF5C87A3966A349A798F224", WETH, USDC, fee_ppm=500, tick_spacing=10),
    Pool("univ3_weth_usdc_30", "uniswap_v3", "uniswap_v3",
         "0x6c561B446416E1A00E8E93E221854d6eA4171372", WETH, USDC, fee_ppm=3000, tick_spacing=60),
    Pool("aero_weth_usdc_v", "aerodrome", "constant_product",
         "0xcDAC0d6c6C59727a65F871236188350531885C43", WETH, USDC, fee_ppm=3000),
    Pool("univ2_weth_usdc", "uniswap_v2", "constant_product",
         "0x88A43bbDF9D098eEC7bCEda4e2494615dfD9bB9C", WETH, USDC, fee_ppm=3000),
]
