// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../src/interfaces/IERC20.sol";

/// A tiny x*y=k pair test double: Uniswap V2 swap() semantics (output first, caller already paid), an
/// Aerodrome-style getAmountOut, and a settable fee. Used to exercise FlashArb's happy path with the real
/// Aave/Morpho flash loans on a fork, since no real pool pair in the dataset is in profitable spread.
contract MockPair {
    address public immutable token0;
    address public immutable token1;
    uint256 public feeBps;

    constructor(address a, address b, uint256 fee) {
        (token0, token1) = a < b ? (a, b) : (b, a);
        feeBps = fee;
    }

    function getReserves() public view returns (uint256, uint256, uint256) {
        return (IERC20(token0).balanceOf(address(this)), IERC20(token1).balanceOf(address(this)), block.timestamp);
    }

    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256) {
        (uint256 r0, uint256 r1,) = getReserves();
        (uint256 rIn, uint256 rOut) = tokenIn == token0 ? (r0, r1) : (r1, r0);
        amountIn -= amountIn * feeBps / 10_000;
        return amountIn * rOut / (rIn + amountIn);
    }

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata) external {
        if (amount0Out > 0) IERC20(token0).transfer(to, amount0Out);
        if (amount1Out > 0) IERC20(token1).transfer(to, amount1Out);
    }
}
