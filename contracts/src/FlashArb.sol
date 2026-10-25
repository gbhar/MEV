// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";
import {IAaveV3Pool, IFlashLoanSimpleReceiver} from "./interfaces/IAaveV3Pool.sol";
import {IMorpho, IMorphoFlashLoanCallback} from "./interfaces/IMorpho.sol";
import {IConcentratedPool, IPairPool} from "./interfaces/IPools.sol";

/// @title FlashArb — the spec's intentionally simple execution contract (section 16).
/// @notice One owner-only entry point. Borrow the quote token with a flash loan, buy the base token on
/// pool A, sell it on pool B, and revert unless the balance covers the repayment plus the caller's minimum
/// profit. The surplus goes to the owner in the same transaction. There is no pricing, routing or AI logic
/// here: the off-chain engine decides everything and the contract only enforces the outcome.
///
/// Lenders: Aave V3 flashLoanSimple (5 bp premium, the platform's assumption) and Morpho Blue (fee-free,
/// selectable per call once an address is configured).
/// Pools: Uniswap V3 / SushiSwap V3 (uniswapV3SwapCallback), PancakeSwap V3 (pancakeV3SwapCallback),
/// Aerodrome Slipstream (uniswapV3SwapCallback), Uniswap V2 pairs and Aerodrome volatile pairs.
contract FlashArb is IFlashLoanSimpleReceiver, IMorphoFlashLoanCallback {
    enum Lender {
        AAVE_V3,
        MORPHO_BLUE
    }

    /// How a leg is executed. CL_* legs call swap() and pay inside the fork's callback; PAIR_* legs transfer the input first.
    enum Kind {
        CL_UNISWAP, // Uniswap V3, SushiSwap V3, Aerodrome Slipstream: uniswapV3SwapCallback
        CL_PANCAKE, // PancakeSwap V3: pancakeV3SwapCallback
        PAIR_V2, // Uniswap V2-style pair, output computed on-chain from reserves and feePpm
        PAIR_AERODROME // Aerodrome volatile pair, output from the pair's own getAmountOut
    }

    struct Leg {
        address pool;
        Kind kind;
        address tokenIn;
        address tokenOut;
        uint24 feePpm; // PAIR_V2 only (parts per million, 3000 = 30 bp)
    }

    struct Params {
        Lender lender;
        address quote; // the borrowed token
        uint256 amount; // loan size in quote units
        Leg buy; // quote -> base
        Leg sell; // base -> quote
        uint256 minProfit; // quote units the owner must receive, or the whole transaction reverts
        uint256 deadlineBlock; // revert when included later than this block
    }

    address public owner;
    bool public paused;
    IAaveV3Pool public immutable aave;
    IMorpho public immutable morpho; // address(0) until a Morpho deployment is configured

    // Set only for the duration of one execute(); every callback checks them.
    address private _callbackPool;
    address private _callbackToken;
    bytes32 private _inFlight;
    uint256 private _lastProfit;

    uint160 private constant MIN_SQRT_RATIO = 4295128739;
    uint160 private constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;

    event Executed(address indexed quote, uint256 amount, uint256 repay, uint256 profit, Lender lender);
    event OwnerChanged(address indexed previousOwner, address indexed newOwner);
    event PausedSet(bool paused);

    error NotOwner();
    error IsPaused();
    error Expired(uint256 deadline, uint256 current);
    error InsufficientProfit(uint256 got, uint256 want);
    error LenderUnavailable();
    error BadCallback();
    error ZeroAddress();
    error TransferFailed();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address aavePool, address morphoBlue) {
        if (aavePool == address(0)) revert ZeroAddress();
        owner = msg.sender;
        aave = IAaveV3Pool(aavePool);
        morpho = IMorpho(morphoBlue);
        emit OwnerChanged(address(0), msg.sender);
    }

    // ------------------------------------------------------------------ owner controls

    function setOwner(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    /// Emergency kill switch (spec: "manual emergency shutdown").
    function setPaused(bool p) external onlyOwner {
        paused = p;
        emit PausedSet(p);
    }

    /// Nothing should ever rest here; this recovers tokens that do (airdrops, a lender refund, mistakes).
    function rescue(address token, address to, uint256 amount) external onlyOwner {
        _transfer(token, to, amount);
    }

    // ------------------------------------------------------------------ entry point

    /// @return profit quote units transferred to the owner.
    function execute(Params calldata p) external onlyOwner returns (uint256 profit) {
        if (paused) revert IsPaused();
        if (block.number > p.deadlineBlock) revert Expired(p.deadlineBlock, block.number);
        if (p.buy.tokenIn != p.quote || p.sell.tokenOut != p.quote || p.buy.tokenOut != p.sell.tokenIn) revert BadCallback();
        bytes memory data = abi.encode(p);
        _inFlight = keccak256(data);
        if (p.lender == Lender.AAVE_V3) {
            aave.flashLoanSimple(address(this), p.quote, p.amount, data, 0);
        } else {
            if (address(morpho) == address(0)) revert LenderUnavailable();
            morpho.flashLoan(p.quote, p.amount, data);
        }
        _inFlight = bytes32(0);
        profit = _lastProfit;
        _lastProfit = 0;
    }

    // ------------------------------------------------------------------ flash-loan callbacks

    function executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata params)
        external
        override
        returns (bool)
    {
        if (msg.sender != address(aave) || initiator != address(this) || keccak256(params) != _inFlight) revert BadCallback();
        Params memory p = abi.decode(params, (Params));
        if (asset != p.quote || amount != p.amount) revert BadCallback();
        _arbitrage(p, amount + premium);
        if (!IERC20(asset).approve(address(aave), amount + premium)) revert TransferFailed();
        return true;
    }

    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external override {
        if (msg.sender != address(morpho) || keccak256(data) != _inFlight) revert BadCallback();
        Params memory p = abi.decode(data, (Params));
        if (assets != p.amount) revert BadCallback();
        _arbitrage(p, assets);
        if (!IERC20(p.quote).approve(address(morpho), assets)) revert TransferFailed();
    }

    // ------------------------------------------------------------------ swap callbacks

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        _payCallback(amount0Delta, amount1Delta);
    }

    function pancakeV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        _payCallback(amount0Delta, amount1Delta);
    }

    function _payCallback(int256 amount0Delta, int256 amount1Delta) private {
        if (msg.sender != _callbackPool || _callbackPool == address(0)) revert BadCallback();
        int256 owed = amount0Delta > 0 ? amount0Delta : amount1Delta;
        if (owed <= 0) revert BadCallback();
        _transfer(_callbackToken, msg.sender, uint256(owed));
    }

    // ------------------------------------------------------------------ the round trip

    function _arbitrage(Params memory p, uint256 repay) private {
        uint256 baseOut = _swap(p.buy, p.amount);
        _swap(p.sell, baseOut);
        uint256 balance = IERC20(p.quote).balanceOf(address(this));
        if (balance < repay + p.minProfit) revert InsufficientProfit(balance > repay ? balance - repay : 0, p.minProfit);
        uint256 profit = balance - repay;
        _lastProfit = profit;
        if (profit > 0) _transfer(p.quote, owner, profit);
        emit Executed(p.quote, p.amount, repay, profit, p.lender);
    }

    function _swap(Leg memory leg, uint256 amountIn) private returns (uint256 amountOut) {
        if (leg.kind == Kind.CL_UNISWAP || leg.kind == Kind.CL_PANCAKE) {
            IConcentratedPool pool = IConcentratedPool(leg.pool);
            bool zeroForOne = leg.tokenIn < leg.tokenOut; // token0 is always the lower address
            _callbackPool = leg.pool;
            _callbackToken = leg.tokenIn;
            (int256 a0, int256 a1) = pool.swap(
                address(this), zeroForOne, int256(amountIn), zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1, ""
            );
            _callbackPool = address(0);
            _callbackToken = address(0);
            int256 out = zeroForOne ? -a1 : -a0;
            if (out <= 0) revert BadCallback();
            return uint256(out);
        }
        IPairPool pair = IPairPool(leg.pool);
        bool inIsToken0 = leg.tokenIn < leg.tokenOut;
        if (leg.kind == Kind.PAIR_AERODROME) {
            amountOut = pair.getAmountOut(amountIn, leg.tokenIn);
        } else {
            (uint256 r0, uint256 r1,) = pair.getReserves();
            (uint256 rIn, uint256 rOut) = inIsToken0 ? (r0, r1) : (r1, r0);
            uint256 inWithFee = amountIn * (1_000_000 - leg.feePpm);
            amountOut = inWithFee * rOut / (rIn * 1_000_000 + inWithFee);
        }
        _transfer(leg.tokenIn, leg.pool, amountIn);
        if (inIsToken0) pair.swap(0, amountOut, address(this), "");
        else pair.swap(amountOut, 0, address(this), "");
    }

    function _transfer(address token, address to, uint256 amount) private {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFailed();
    }
}
