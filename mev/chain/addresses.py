"""Base mainnet addresses and the bootstrap pool set (every pool here was found by probing the factories on
2026-10-24 and has >= $100k of depth at the current price). mev.pools.discovery keeps the live set in the DB."""
from mev.pools.registry import Pool, Token

CHAIN_ID = 8453

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
USDbC = Token("0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "USDbC", 6)
USDT = Token("0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "USDT", 6)
DAI = Token("0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "DAI", 18)
cbBTC = Token("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "cbBTC", 8)
cbETH = Token("0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "cbETH", 18)
wstETH = Token("0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "wstETH", 18)
AERO = Token("0x940181a94A35A4569E4529A3CDfB74e38FD98631", "AERO", 18)
VIRTUAL = Token("0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "VIRTUAL", 18)
# 2026-10-28: tokens with >= two deep venues, found by enumerating every Aerodrome pool and probing the other factories
VVV = Token("0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf", "VVV", 18)
BRETT = Token("0x532f27101965dd16442E59d40670FaF5eBB142E4", "BRETT", 18)
MORPHO = Token("0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842", "MORPHO", 18)
DEGEN = Token("0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "DEGEN", 18)
TOKENS = {t.key: t for t in (WETH, USDC, USDbC, USDT, DAI, cbBTC, cbETH, wstETH, AERO, VIRTUAL, VVV, BRETT, MORPHO, DEGEN)}

UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
PANCAKE_V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"
PANCAKE_V3_QUOTER_V2 = "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"
SUSHI_V3_FACTORY = "0xc35DADB65012eC5796536bD9864eD8773aBc74C4"
AERODROME_CL_FACTORY = "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
AERODROME_CL_QUOTER_V2 = "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"
AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
UNISWAP_V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
BASESWAP_V2_FACTORY = "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB"
SUSHI_V2_FACTORY = "0x71524B4f93c58fcbF659783284E38825f0622859"
AAVE_V3_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"   # fee-free flash loans; placeholder lender in FlashArb
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

QUOTERS = {"uniswap": UNISWAP_V3_QUOTER_V2, "pancake": PANCAKE_V3_QUOTER_V2, "slipstream": AERODROME_CL_QUOTER_V2}

# token0/token1 is the on-chain order (ascending address); Pool.base/quote decide the trade orientation.
def _v3(dex, variant, pid, addr, t0, t1, fee, spacing):
    return Pool(pid, dex, "uniswap_v3", addr, t0, t1, fee_ppm=fee, tick_spacing=spacing, variant=variant)


def _cp(dex, variant, pid, addr, t0, t1, fee):
    return Pool(pid, dex, "constant_product", addr, t0, t1, fee_ppm=fee, variant=variant)


POOLS: list[Pool] = [
    # WETH/USDC
    _v3("uniswap_v3", "uniswap", "univ3_weth_usdc_1", "0xb4CB800910B228ED3d0834cF79D697127BBB00e5", WETH, USDC, 100, 1),
    _v3("uniswap_v3", "uniswap", "univ3_weth_usdc_5", "0xd0b53D9277642d899DF5C87A3966A349A798F224", WETH, USDC, 500, 10),
    _v3("uniswap_v3", "uniswap", "univ3_weth_usdc_30", "0x6c561B446416E1A00E8E93E221854d6eA4171372", WETH, USDC, 3000, 60),
    _v3("pancake_v3", "pancake", "pcs3_weth_usdc_1", "0x72AB388E2E2F6FaceF59E3C3FA2C4E29011c2D38", WETH, USDC, 100, 1),
    _v3("sushi_v3", "uniswap", "sushi3_weth_usdc_5", "0x57713F7716e0b0F65ec116912F834E49805480d2", WETH, USDC, 500, 10),
    _v3("aerodrome_cl", "slipstream", "aerocl_weth_usdc_100", "0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59", WETH, USDC, 150, 100),
    _cp("aerodrome", "aerodrome", "aero_weth_usdc_v", "0xcDAC0d6c6C59727a65F871236188350531885C43", WETH, USDC, 3000),
    _cp("uniswap_v2", "v2", "univ2_weth_usdc", "0x88A43bbDF9D098eEC7bCEda4e2494615dfD9bB9C", WETH, USDC, 3000),
    # WETH/USDT, WETH/USDbC
    _v3("uniswap_v3", "uniswap", "univ3_weth_usdt_5", "0xd92E0767473D1E3FF11Ac036f2b1DB90aD0aE55F", WETH, USDT, 500, 10),
    _v3("uniswap_v3", "uniswap", "univ3_weth_usdt_30", "0xcE1d8c90A5F0ef28fe0F457e5Ad615215899319a", WETH, USDT, 3000, 60),
    _v3("aerodrome_cl", "slipstream", "aerocl_weth_usdt_100", "0x9785eF59E2b499fB741674ecf6fAF912Df7b3C1b", WETH, USDT, 424, 100),
    _v3("uniswap_v3", "uniswap", "univ3_weth_usdbc_5", "0x4C36388bE6F416A29C8d8Eee81C771cE6bE14B18", WETH, USDbC, 500, 10),
    # cbBTC/USDC (USDC is token0)
    _v3("uniswap_v3", "uniswap", "univ3_usdc_cbbtc_5", "0xfBB6Eed8e7aa03B138556eeDaF5D271A5E1e43ef", USDC, cbBTC, 500, 10),
    _v3("pancake_v3", "pancake", "pcs3_usdc_cbbtc_5", "0x26e263efdc91f0d3279E2Ec2Bd58A7Ca5C2fCE62", USDC, cbBTC, 500, 10),
    _v3("aerodrome_cl", "slipstream", "aerocl_usdc_cbbtc_100", "0x4e962BB3889Bf030368F56810A9c96B83CB3E778", USDC, cbBTC, 448, 100),
    _v3("aerodrome_cl", "slipstream", "aerocl_usdc_cbbtc_2000", "0x3e66e55e97ce60096f74b7C475e8249f2D31a9fb", USDC, cbBTC, 338, 2000),
    _cp("aerodrome", "aerodrome", "aero_usdc_cbbtc_v", "0x9c38b55f9A9Aba91BbCEDEb12bf4428f47A6a0B8", USDC, cbBTC, 3000),
    # WETH/cbBTC (quote = WETH)
    _v3("uniswap_v3", "uniswap", "univ3_weth_cbbtc_5", "0x7AeA2E8A3843516afa07293a10Ac8E49906dabD1", WETH, cbBTC, 500, 10),
    _v3("uniswap_v3", "uniswap", "univ3_weth_cbbtc_30", "0x8c7080564B5A792A33Ef2FD473fbA6364d5495e5", WETH, cbBTC, 3000, 60),
    _v3("pancake_v3", "pancake", "pcs3_weth_cbbtc_1", "0xC211e1f853A898Bd1302385CCdE55f33a8C4B3f3", WETH, cbBTC, 100, 1),
    _v3("pancake_v3", "pancake", "pcs3_weth_cbbtc_5", "0xd974D59e30054cf1aBedeD0C9947B0D8Baf90029", WETH, cbBTC, 500, 10),
    _v3("aerodrome_cl", "slipstream", "aerocl_weth_cbbtc_100", "0x70aCDF2Ad0bf2402C957154f944c19Ef4e1cbAE1", WETH, cbBTC, 2510, 100),
    _cp("aerodrome", "aerodrome", "aero_weth_cbbtc_v", "0x2578365B3dfA7FfE60108e181EFb79FeDdec2319", WETH, cbBTC, 3000),
    # cbETH/WETH, wstETH/WETH (quote = WETH)
    _v3("uniswap_v3", "uniswap", "univ3_cbeth_weth_1", "0xA9DaFa443a02FBc907Cb0093276B3E6F4ef02A46", cbETH, WETH, 100, 1),
    _v3("uniswap_v3", "uniswap", "univ3_cbeth_weth_5", "0x10648BA41B8565907Cfa1496765fA4D95390aa0d", cbETH, WETH, 500, 10),
    _v3("pancake_v3", "pancake", "pcs3_cbeth_weth_1", "0x257FCbAE4Ac6B26A02E4FC5e1a11e4174B5ce395", cbETH, WETH, 100, 1),
    _cp("aerodrome", "aerodrome", "aero_cbeth_weth_v", "0x44Ecc644449fC3a9858d2007CaA8CFAa4C561f91", cbETH, WETH, 3000),
    _v3("uniswap_v3", "uniswap", "univ3_weth_wsteth_1", "0x20E068D76f9E90b90604500B84c7e19dCB923e7e", WETH, wstETH, 100, 1),
    _cp("aerodrome", "aerodrome", "aero_weth_wsteth_v", "0xA6385c73961dd9C58db2EF0c4EB98cE4B60651e8", WETH, wstETH, 3000),
    # stables
    _v3("uniswap_v3", "uniswap", "univ3_usdc_usdbc_1", "0x06959273E9A65433De71F5A452D529544E07dDD0", USDC, USDbC, 100, 1),
    _v3("pancake_v3", "pancake", "pcs3_usdc_usdbc_1", "0x29Ed55B18Af0Add137952CB3E29FB77B32fCE426", USDC, USDbC, 100, 1),
    _v3("sushi_v3", "uniswap", "sushi3_usdc_usdbc_1", "0xD3f749adA01aF29a713545Fb6b8E782B49A75a20", USDC, USDbC, 100, 1),
    _v3("aerodrome_cl", "slipstream", "aerocl_usdc_usdbc_1", "0x98c7A2338336d2d354663246F64676009c7bDa97", USDC, USDbC, 100, 1),
    _v3("uniswap_v3", "uniswap", "univ3_usdc_usdt_1", "0xD56da2B74bA826f19015E6B7Dd9Dae1903E85DA1", USDC, USDT, 100, 1),
    _v3("pancake_v3", "pancake", "pcs3_usdc_usdt_1", "0x5f07bb9fEE6062e9D09A52E6d587c64bAD6bA706", USDC, USDT, 100, 1),
    _v3("aerodrome_cl", "slipstream", "aerocl_usdc_usdt_1", "0xa41Bc0AFfbA7Fd420d186b84899d7ab2aC57fcD1", USDC, USDT, 11, 1),
    _v3("uniswap_v3", "uniswap", "univ3_dai_usdc_1", "0xC18F50d6A832f12F6DcAaeEe8D0c87A65B96787E", DAI, USDC, 100, 1),
    _v3("pancake_v3", "pancake", "pcs3_dai_usdc_1", "0x345825A980BD94e1480bC4f20FE4e3DAE2F23Dd3", DAI, USDC, 100, 1),
    # AERO and VIRTUAL
    _v3("aerodrome_cl", "slipstream", "aerocl_usdc_aero_2000", "0xBE00fF35AF70E8415D0eB605a286D8A45466A4c1", USDC, AERO, 10000, 2000),
    _v3("uniswap_v3", "uniswap", "univ3_usdc_aero_5", "0xE5B5f522E98B5a2baAe212d4dA66b865B781DB97", USDC, AERO, 500, 10),
    _v3("aerodrome_cl", "slipstream", "aerocl_weth_aero_200", "0x82321f3BEB69f503380D6B233857d5C43562e2D0", WETH, AERO, 3076, 200),
    _cp("aerodrome", "aerodrome", "aero_weth_aero_v", "0x7f670f78B17dEC44d5Ef68a48740b6f8849cc2e6", WETH, AERO, 10000),   # factory getFee = 100 bp,
    _v3("pancake_v3", "pancake", "pcs3_weth_aero_5", "0x20CB8f872ae894F7c9e32e621C186e5AFCe82Fd0", WETH, AERO, 500, 10),
    _v3("uniswap_v3", "uniswap", "univ3_virtual_weth_5", "0x9c087Eb773291e50CF6c6a90ef0F4500e349B903", VIRTUAL, WETH, 500, 10),
    _v3("aerodrome_cl", "slipstream", "aerocl_virtual_weth_200", "0xC200F21EfE67c7F41B81A854c26F9cdA80593065", VIRTUAL, WETH, 600, 200),
    _cp("aerodrome", "aerodrome", "aero_virtual_weth_v", "0x21594b992F68495dD28d605834b58889d0a727c7", VIRTUAL, WETH, 10000),   # factory getFee = 100 bp,
    _cp("uniswap_v2", "v2", "univ2_virtual_weth", "0xE31c372a7Af875b3B5E0F3713B17ef51556da667", VIRTUAL, WETH, 3000),
]
