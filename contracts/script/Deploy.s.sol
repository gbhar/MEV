// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script} from "forge-std/Script.sol";
import {FlashArb} from "../src/FlashArb.sol";

/// forge script script/Deploy.s.sol --rpc-url base --broadcast  (signer from MEV_SIGNER_KEY via `--private-key`;
/// scripts/deploy_contract.py is the Python equivalent that uses the platform's signer module).
contract Deploy is Script {
    address constant AAVE_V3_POOL_BASE = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
    address constant MORPHO_BLUE_BASE = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;

    function run() external returns (FlashArb arb) {
        vm.startBroadcast();
        arb = new FlashArb(AAVE_V3_POOL_BASE, MORPHO_BLUE_BASE);
        vm.stopBroadcast();
    }
}
