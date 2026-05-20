"""
End-to-end x402 paid call against ARMS Signals.

Self-pays $0.001 USDC on Base mainnet from EVM_PRIVATE_KEY's wallet to
the endpoint's payTo (which happens to be the same address — fine, just
a self-transfer that still triggers the CDP facilitator verify+settle).

A successful run prints:
  - 402 then 200
  - the VRP payload
  - the on-chain settlement tx hash (proof for Agora submission)

Once CDP processes the settle, the endpoint should appear in the Bazaar
discovery catalog within a few minutes.

Run:
  EVM_PRIVATE_KEY=0x... python demo_client.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from eth_account import Account

from x402 import x402Client
from x402.http import x402HTTPClient
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client


URL_DEFAULT = "https://regimeshift.xyz/api/v1/asset/eth/vrp"


async def main() -> None:
    load_dotenv()
    pk = os.getenv("EVM_PRIVATE_KEY")
    url = os.getenv("X402_URL", URL_DEFAULT)
    if not pk:
        print("EVM_PRIVATE_KEY not set. Export your Base mainnet private key.")
        sys.exit(1)

    client = x402Client()
    account = Account.from_key(pk)
    register_exact_evm_client(client, EthAccountSigner(account))
    print(f"Buyer wallet: {account.address}")
    print(f"Calling:      {url}\n")

    http_helper = x402HTTPClient(client)

    async with x402HttpxClient(client) as http:
        response = await http.get(url)
        await response.aread()

        print(f"Final status: {response.status_code}")
        print(f"Body:\n{response.text}\n")

        try:
            settle = http_helper.get_payment_settle_response(
                lambda name: response.headers.get(name)
            )
            print("=== Settlement ===")
            print(settle.model_dump_json(indent=2))
        except ValueError:
            print("No settlement header — payment may not have settled.")


if __name__ == "__main__":
    asyncio.run(main())
