// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// Concentrated-liquidity pool swap: Uniswap V3, SushiSwap V3, PancakeSwap V3 and Aerodrome Slipstream share it.
/// The pool pays `recipient` first and then calls back for the input; the callback name differs per fork.
interface IConcentratedPool {
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1);
    function token0() external view returns (address);
    function token1() external view returns (address);
}

/// x*y=k pair: Uniswap V2 and Aerodrome volatile pools share it. Input is transferred to the pair first.
interface IPairPool {
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
    function getReserves() external view returns (uint256 reserve0, uint256 reserve1, uint256 blockTimestampLast);
    function token0() external view returns (address);
    function token1() external view returns (address);
    /// Aerodrome only (Uniswap V2 pairs lack it): the pool's own fee-aware quote.
    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256);
}
