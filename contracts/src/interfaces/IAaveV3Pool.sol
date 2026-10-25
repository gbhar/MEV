// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// Aave V3 Pool, only what flashLoanSimple needs. Base mainnet: 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5.
interface IAaveV3Pool {
    function flashLoanSimple(address receiverAddress, address asset, uint256 amount, bytes calldata params, uint16 referralCode) external;
    function FLASHLOAN_PREMIUM_TOTAL() external view returns (uint128);
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata params) external returns (bool);
}
