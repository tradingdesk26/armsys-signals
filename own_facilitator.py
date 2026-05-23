"""
RegimeShift own x402 facilitator — bypasses CDP entirely.

WHY: Coinbase's CDP facilitator (api.cdp.coinbase.com/platform/v2/x402) has
deployed schema validators that reject paymentPayloads from the canonical
x402 SDK (we tried SDK 2.0–2.11, all fail with various JSON Schema errors —
the deployed CDP spec has diverged from the published SDK in mid-migration).

WHAT: This is a local facilitator that uses the SAME x402 SDK as the
arms-signals server, so payloads round-trip without format ambiguity. It
settles payments by submitting USDC.transferWithAuthorization (EIP-3009)
transactions on Base mainnet using Wallet A as the gas-paying relayer.

WIRE: speaks the standard x402 facilitator HTTP API:
  POST /verify    — checks EIP-712 sig + balance + nonce
  POST /settle    — submits the on-chain transferWithAuthorization tx
  GET  /supported — declares we serve scheme=exact on eip155:8453

DEPLOYMENT: runs on port 8091; arms-signals' FACILITATOR_URL points here.

Blueprint adapted from x402-foundation/x402:examples/python/facilitator/basic
"""

import os
import sys

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from x402 import x402Facilitator
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.exact import register_exact_evm_facilitator


# ─── Config ──────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "8091"))

# Wallet A is the oracle signer AND the facilitator relayer.
# It pays gas for transferWithAuthorization. Address: 0x3d6EF3B451Abaf79eb0a5c08089518fB3f4de8b5
EVM_PRIVATE_KEY = os.environ.get("EVM_PRIVATE_KEY")
EVM_RPC_URL     = os.environ.get("EVM_RPC_URL", "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy")

if not EVM_PRIVATE_KEY:
    print("❌ EVM_PRIVATE_KEY environment variable is required (relayer wallet)")
    sys.exit(1)


# ─── Initialize signer + facilitator ────────────────────────────────────────

evm_signer = FacilitatorWeb3Signer(
    private_key=EVM_PRIVATE_KEY,
    rpc_url=EVM_RPC_URL,
)
print(f"Relayer (Wallet A): {evm_signer.get_addresses()[0]}")


# Hooks — useful for debugging during integration
async def before_verify_hook(ctx):
    print(f"[verify] start  payer={getattr(ctx.payment_payload, 'payload', {})}")


async def after_verify_hook(ctx):
    res = getattr(ctx, "result", None)
    print(f"[verify] result {res}")


async def verify_failure_hook(ctx):
    print(f"[verify] FAIL   {getattr(ctx, 'error', '?')}")


async def before_settle_hook(ctx):
    print(f"[settle] start")


async def after_settle_hook(ctx):
    res = getattr(ctx, "result", None)
    tx  = getattr(res, "transaction", "?")
    print(f"[settle] OK     tx={tx}")


async def settle_failure_hook(ctx):
    print(f"[settle] FAIL   {getattr(ctx, 'error', '?')}")


facilitator = (
    x402Facilitator()
    .on_before_verify(before_verify_hook)
    .on_after_verify(after_verify_hook)
    .on_verify_failure(verify_failure_hook)
    .on_before_settle(before_settle_hook)
    .on_after_settle(after_settle_hook)
    .on_settle_failure(settle_failure_hook)
)

# Register the EVM "exact" scheme for Base mainnet (eip155:8453)
register_exact_evm_facilitator(
    facilitator,
    evm_signer,
    networks="eip155:8453",   # Base mainnet
    # No erc4337 deployment — paying agents already have funded EOAs
)


# ─── Pydantic request bodies ────────────────────────────────────────────────

class VerifyRequest(BaseModel):
    paymentPayload: dict
    paymentRequirements: dict


class SettleRequest(BaseModel):
    paymentPayload: dict
    paymentRequirements: dict


# ─── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(
    title="RegimeShift x402 Facilitator (own)",
    description="Bypasses CDP — settles paid x402 calls directly on Base mainnet.",
    version="1.0.0",
)


@app.post("/verify")
async def verify(request: VerifyRequest):
    try:
        from x402.schemas import PaymentRequirements, parse_payment_payload

        payload      = parse_payment_payload(request.paymentPayload)
        requirements = PaymentRequirements.model_validate(request.paymentRequirements)
        response     = await facilitator.verify(payload, requirements)
        return response.model_dump(by_alias=True, exclude_none=True)
    except Exception as e:
        print(f"verify error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/settle")
async def settle(request: SettleRequest):
    try:
        from x402.schemas import PaymentRequirements, parse_payment_payload

        payload      = parse_payment_payload(request.paymentPayload)
        requirements = PaymentRequirements.model_validate(request.paymentRequirements)
        response     = await facilitator.settle(payload, requirements)
        return response.model_dump(by_alias=True, exclude_none=True)
    except Exception as e:
        print(f"settle error: {e}")
        if "aborted" in str(e).lower():
            from x402.schemas import SettleResponse
            abort = SettleResponse(
                success=False,
                error_reason=str(e),
                network=request.paymentPayload.get("accepted", {}).get("network", "unknown"),
                transaction="",
            )
            return abort.model_dump(by_alias=True, exclude_none=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/supported")
async def supported():
    try:
        response = facilitator.get_supported()
        return {
            "kinds":      [k.model_dump(by_alias=True, exclude_none=True) for k in response.kinds],
            "extensions": response.extensions,
            "signers":    response.signers,
        }
    except Exception as e:
        print(f"supported error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/health")
async def health():
    return {"status": "ok", "relayer": evm_signer.get_addresses()[0]}


if __name__ == "__main__":
    import uvicorn
    print(f"RegimeShift facilitator listening on port {PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
