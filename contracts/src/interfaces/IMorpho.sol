// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// Morpho Blue flash loans (fee-free). Base mainnet: 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb.
/// The borrower must approve Morpho for `assets` before the callback returns; Morpho pulls the repayment itself.
interface IMorpho {
    function flashLoan(address token, uint256 assets, bytes calldata data) external;
}

interface IMorphoFlashLoanCallback {
    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external;
}
