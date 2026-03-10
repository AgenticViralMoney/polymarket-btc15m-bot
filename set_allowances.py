#!/usr/bin/env python3
"""
One-time script to set on-chain allowances for Polymarket trading.

This approves the Exchange contracts to spend:
  - USDC (for BUY orders)
  - Conditional Tokens / CTF (for SELL orders)

Run once per wallet. Requires a small amount of POL (formerly MATIC)
on Polygon for gas fees (~0.01 POL total for all transactions).

Usage:
  cd ~/Downloads/polymarket_5m_bot
  source .venv/bin/activate
  pip install web3
  python3 set_allowances.py
"""

import os
import ssl
import sys

import certifi
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "").strip()

if not PRIVATE_KEY or not FUNDER_ADDRESS:
    print("ERROR: PRIVATE_KEY and FUNDER_ADDRESS must be set in .env")
    sys.exit(1)

try:
    from web3 import Web3
    from web3.constants import MAX_INT
except ImportError:
    print("ERROR: web3 not installed. Run: pip install web3")
    sys.exit(1)

# --- Polygon network (multiple RPCs for fallback) ---
RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
]
CHAIN_ID = 137

# --- Contract addresses (Polygon mainnet) ---
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchange contracts that need approval
EXCHANGES = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

# ABIs (minimal)
ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ERC1155_SET_APPROVAL_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "operator", "type": "address"},
            {"internalType": "bool", "name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def send_and_wait(w3, raw_tx, label):
    signed = w3.eth.account.sign_transaction(raw_tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  {label}: tx sent {tx_hash.hex()}, waiting...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = "OK" if receipt["status"] == 1 else "FAILED"
    print(f"  {label}: {status} (block {receipt['blockNumber']})")
    return receipt


def main():
    # Fix macOS SSL certificate issues by pointing to certifi CA bundle
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

    w3 = None
    for rpc_url in RPC_URLS:
        try:
            provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10})
            candidate = Web3(provider)
            # Polygon is a POA chain — inject middleware to handle extraData
            candidate.middleware_onion.inject(
                Web3.middleware.ExtraDataToPOAMiddleware, layer=0
            )
            if candidate.is_connected():
                print(f"Connected to: {rpc_url}")
                w3 = candidate
                break
            else:
                print(f"  {rpc_url} -- not responding, trying next...")
        except Exception as e:
            print(f"  {rpc_url} -- failed: {e}")

    if w3 is None:
        print("ERROR: Cannot connect to any Polygon RPC. Check your internet / SSL.")
        sys.exit(1)

    # Use the funder address (proxy wallet) as the transaction sender
    # But we sign with the private key (EOA) that controls it
    eoa = w3.eth.account.from_key(PRIVATE_KEY).address
    balance = w3.eth.get_balance(eoa)
    pol_balance = w3.from_wei(balance, "ether")
    print(f"EOA address: {eoa}")
    print(f"Funder address: {FUNDER_ADDRESS}")
    print(f"POL balance: {pol_balance} POL")

    if balance < w3.to_wei(0.005, "ether"):
        print("WARNING: Very low POL balance. You need ~0.01 POL for gas fees.")
        print("Send some POL to your EOA address above.")
        sys.exit(1)

    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_SET_APPROVAL_ABI)

    nonce = w3.eth.get_transaction_count(eoa)
    max_uint = int(MAX_INT, 0)

    print(f"\nSetting allowances for {len(EXCHANGES)} exchange contracts...\n")

    for name, exchange_addr in EXCHANGES.items():
        print(f"[{name}] {exchange_addr}")

        # 1) Approve USDC spending
        tx = usdc.functions.approve(exchange_addr, max_uint).build_transaction(
            {"chainId": CHAIN_ID, "from": eoa, "nonce": nonce}
        )
        send_and_wait(w3, tx, "USDC approve")
        nonce += 1

        # 2) Approve CTF (conditional token) spending — THIS IS WHAT ENABLES SELLING
        tx = ctf.functions.setApprovalForAll(exchange_addr, True).build_transaction(
            {"chainId": CHAIN_ID, "from": eoa, "nonce": nonce}
        )
        send_and_wait(w3, tx, "CTF setApprovalForAll")
        nonce += 1

        print()

    print("All allowances set. Sells should now work.")
    print("You only need to run this once per wallet.")


if __name__ == "__main__":
    main()
