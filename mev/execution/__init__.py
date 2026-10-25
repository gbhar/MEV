"""Execution layer (spec Phases 5-7): FlashArb calldata, the local signer, the kill switch and the engine that
runs in shadow (build + eth_call, never send) or live (sign + send under hard caps) mode. Nothing in this package
is imported unless MEV_EXECUTION_MODE is shadow or live."""
