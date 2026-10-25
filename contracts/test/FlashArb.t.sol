// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {FlashArb} from "../src/FlashArb.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {IPairPool} from "../src/interfaces/IPools.sol";
import {MockPair} from "./MockPair.sol";

interface IQuoterV2 {
    struct QuoteExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint24 fee;
        uint160 sqrtPriceLimitX96;
    }

    function quoteExactInputSingle(QuoteExactInputSingleParams memory p)
        external
        returns (uint256 amountOut, uint160 sqrtPriceX96After, uint32 initializedTicksCrossed, uint256 gasEstimate);
}

/// Fork tests run against the real Base state at the blocks of paper trades the platform booked on
/// 2026-10-24. Run: MEV_RPC_HTTP=<archive rpc> forge test  (every test skips when the variable is unset).
contract FlashArbTest is Test {
    address constant AAVE = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address constant QUOTER_V2 = 0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a;
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant VIRTUAL = 0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b;
    address constant AERO_VIRTUAL_WETH = 0x21594b992F68495dD28d605834b58889d0a727c7; // Aerodrome volatile, 100 bp fee
    address constant UNIV3_VIRTUAL_WETH_5 = 0x9c087Eb773291e50CF6c6a90ef0F4500e349B903; // Uniswap V3, 5 bp

    // Paper trade 4 (block 53038943, $1,000 of WETH). The platform booked it as +$1.68 with the Aerodrome fee
    // wrongly registered at 30 bp; with the real 100 bp fee the simulator and the chain agree it loses
    // 1966879470454673 wei (~$4.58) before the premium. These constants come from mev/simulator at that block.
    uint256 constant T4_BLOCK = 53038943;
    uint256 constant T4_AMOUNT = 429501687933749293;
    uint256 constant T4_BUY_OUT = 1606635843626486163868; // VIRTUAL from Aerodrome
    uint256 constant T4_SELL_OUT = 427534808463294621; // WETH from Uniswap V3

    FlashArb arb;
    address owner = address(this);
    address stranger = address(0xBEEF);

    function _fork(uint256 blockNumber) internal returns (bool) {
        string memory url = vm.envOr("MEV_RPC_HTTP", string(""));
        if (bytes(url).length == 0) {
            emit log("MEV_RPC_HTTP unset: fork test skipped");
            return false;
        }
        vm.createSelectFork(url, blockNumber);
        arb = new FlashArb(AAVE, MORPHO);
        return true;
    }

    function _trade4(FlashArb.Lender lender, uint256 minProfit) internal view returns (FlashArb.Params memory p) {
        p.lender = lender;
        p.quote = WETH;
        p.amount = T4_AMOUNT;
        p.buy = FlashArb.Leg(AERO_VIRTUAL_WETH, FlashArb.Kind.PAIR_AERODROME, WETH, VIRTUAL, 0);
        p.sell = FlashArb.Leg(UNIV3_VIRTUAL_WETH_5, FlashArb.Kind.CL_UNISWAP, VIRTUAL, WETH, 0);
        p.minProfit = minProfit;
        p.deadlineBlock = block.number;
    }

    /// Two mock pairs around the real flash loan: VIRTUAL is 2% cheaper on A than on B (0.1 WETH moves each 0.1%).
    function _mockRoute(FlashArb.Lender lender, uint256 amount, uint256 minProfit)
        internal
        returns (FlashArb.Params memory p, MockPair a, MockPair b)
    {
        a = new MockPair(WETH, VIRTUAL, 30);
        b = new MockPair(WETH, VIRTUAL, 30);
        deal(WETH, address(a), 100 ether);
        deal(VIRTUAL, address(a), 100 ether * 2500 * 102 / 100); // 2550 VIRTUAL per WETH on A
        deal(WETH, address(b), 100 ether);
        deal(VIRTUAL, address(b), 100 ether * 2500); // 2500 VIRTUAL per WETH on B
        p.lender = lender;
        p.quote = WETH;
        p.amount = amount;
        p.buy = FlashArb.Leg(address(a), FlashArb.Kind.PAIR_V2, WETH, VIRTUAL, 3000);
        p.sell = FlashArb.Leg(address(b), FlashArb.Kind.PAIR_AERODROME, VIRTUAL, WETH, 0);
        p.minProfit = minProfit;
        p.deadlineBlock = block.number;
    }

    // ---------------------------------------------------------------- real pools

    function test_trade4_legs_match_python_simulator_to_the_wei() public {
        if (!_fork(T4_BLOCK)) return;
        assertEq(IPairPool(AERO_VIRTUAL_WETH).getAmountOut(T4_AMOUNT, WETH), T4_BUY_OUT, "Aerodrome leg");
        (uint256 out,,,) = IQuoterV2(QUOTER_V2).quoteExactInputSingle(
            IQuoterV2.QuoteExactInputSingleParams(VIRTUAL, WETH, T4_BUY_OUT, 500, 0)
        );
        assertEq(out, T4_SELL_OUT, "Uniswap V3 leg");
        assertLt(T4_SELL_OUT, T4_AMOUNT, "the round trip loses money at the real 100 bp fee");
    }

    function test_trade4_reverts_instead_of_losing() public {
        if (!_fork(T4_BLOCK)) return;
        uint256 before = IERC20(WETH).balanceOf(owner);
        vm.expectRevert(); // Aave re-wraps the receiver's InsufficientProfit; only the gas is lost
        arb.execute(_trade4(FlashArb.Lender.AAVE_V3, 1));
        assertEq(IERC20(WETH).balanceOf(owner), before);
        assertEq(IERC20(WETH).balanceOf(address(arb)), 0);
        assertEq(IERC20(VIRTUAL).balanceOf(address(arb)), 0);
    }

    // ---------------------------------------------------------------- happy path with the real lenders

    function test_profit_is_swept_to_owner_aave() public {
        if (!_fork(T4_BLOCK)) return;
        (FlashArb.Params memory p,,) = _mockRoute(FlashArb.Lender.AAVE_V3, 0.1 ether, 0.0005 ether);
        uint256 before = IERC20(WETH).balanceOf(owner);
        uint256 profit = arb.execute(p);
        assertGt(profit, 0.0005 ether);
        assertEq(IERC20(WETH).balanceOf(owner) - before, profit, "profit swept to owner");
        assertEq(IERC20(WETH).balanceOf(address(arb)), 0, "nothing rests in the contract");
        assertEq(IERC20(VIRTUAL).balanceOf(address(arb)), 0, "no base token left behind");
    }

    function test_profit_is_swept_to_owner_morpho_fee_free() public {
        if (!_fork(T4_BLOCK)) return;
        (FlashArb.Params memory pa,,) = _mockRoute(FlashArb.Lender.AAVE_V3, 0.1 ether, 0);
        uint256 withAave = arb.execute(pa);
        (FlashArb.Params memory pm,,) = _mockRoute(FlashArb.Lender.MORPHO_BLUE, 0.1 ether, 0);
        uint256 withMorpho = arb.execute(pm);
        assertEq(withMorpho - withAave, 0.1 ether * 5 / 10_000, "Morpho saves exactly the 5 bp premium");
    }

    function test_reverts_when_profit_below_minimum() public {
        if (!_fork(T4_BLOCK)) return;
        (FlashArb.Params memory p,,) = _mockRoute(FlashArb.Lender.AAVE_V3, 0.1 ether, 1 ether);
        vm.expectRevert();
        arb.execute(p);
        assertEq(IERC20(WETH).balanceOf(address(arb)), 0);
    }

    // ---------------------------------------------------------------- controls

    function test_only_owner_can_execute() public {
        if (!_fork(T4_BLOCK)) return;
        FlashArb.Params memory p = _trade4(FlashArb.Lender.AAVE_V3, 1);
        vm.prank(stranger);
        vm.expectRevert(FlashArb.NotOwner.selector);
        arb.execute(p);
    }

    function test_paused_blocks_execution() public {
        if (!_fork(T4_BLOCK)) return;
        (FlashArb.Params memory p,,) = _mockRoute(FlashArb.Lender.AAVE_V3, 0.1 ether, 0);
        arb.setPaused(true);
        vm.expectRevert(FlashArb.IsPaused.selector);
        arb.execute(p);
        arb.setPaused(false);
        assertGt(arb.execute(p), 0);
    }

    function test_expired_deadline_reverts() public {
        if (!_fork(T4_BLOCK)) return;
        FlashArb.Params memory p = _trade4(FlashArb.Lender.AAVE_V3, 1);
        p.deadlineBlock = block.number - 1;
        vm.expectRevert(abi.encodeWithSelector(FlashArb.Expired.selector, block.number - 1, block.number));
        arb.execute(p);
    }

    function test_mismatched_legs_revert() public {
        if (!_fork(T4_BLOCK)) return;
        FlashArb.Params memory p = _trade4(FlashArb.Lender.AAVE_V3, 1);
        p.sell.tokenOut = VIRTUAL; // the sell leg must return the borrowed token
        vm.expectRevert(FlashArb.BadCallback.selector);
        arb.execute(p);
    }

    function test_callbacks_reject_strangers() public {
        if (!_fork(T4_BLOCK)) return;
        vm.expectRevert(FlashArb.BadCallback.selector);
        arb.uniswapV3SwapCallback(1, -1, "");
        vm.expectRevert(FlashArb.BadCallback.selector);
        arb.pancakeV3SwapCallback(1, -1, "");
        vm.expectRevert(FlashArb.BadCallback.selector);
        arb.executeOperation(WETH, 1, 0, address(arb), "");
        vm.expectRevert(FlashArb.BadCallback.selector);
        arb.onMorphoFlashLoan(1, "");
    }

    function test_rescue_and_owner_transfer() public {
        if (!_fork(T4_BLOCK)) return;
        deal(WETH, address(arb), 1 ether);
        uint256 before = IERC20(WETH).balanceOf(stranger);
        arb.rescue(WETH, stranger, 1 ether);
        assertEq(IERC20(WETH).balanceOf(stranger) - before, 1 ether);
        arb.setOwner(stranger);
        vm.expectRevert(FlashArb.NotOwner.selector);
        arb.setPaused(true);
    }
}
