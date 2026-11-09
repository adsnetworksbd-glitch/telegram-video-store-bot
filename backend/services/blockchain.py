import httpx
import os
from typing import Optional

TRONSCAN_API = "https://apilist.tronscanapi.com/api"
BSCSCAN_API = "https://api.bscscan.com/api"

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"


async def verify_trc20_payment(
    tx_hash: str,
    expected_to: str,
    expected_amount_usdt: float,
    order_id: str
) -> dict:
    """Verify USDT TRC20 transaction via TronScan public API"""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{TRONSCAN_API}/transaction-info",
                params={"hash": tx_hash}
            )
            data = resp.json()
        except Exception as e:
            return {"verified": False, "error": str(e)}

    if not data or data.get("contractRet") != "SUCCESS":
        return {"verified": False, "error": "Transaction not successful or not found"}

    trc20_transfers = data.get("trc20TransferInfo", [])
    for transfer in trc20_transfers:
        contract = transfer.get("contract_address", "")
        to_addr = transfer.get("to_address", "")
        amount_raw = int(transfer.get("amount_str", "0"))
        amount_usdt = amount_raw / 1_000_000  # USDT has 6 decimals

        if (contract.lower() == USDT_TRC20_CONTRACT.lower()
                and to_addr.lower() == expected_to.lower()
                and abs(amount_usdt - expected_amount_usdt) < 0.01):
            return {
                "verified": True,
                "network": "TRC20",
                "txHash": tx_hash,
                "amount": amount_usdt,
                "toAddress": to_addr,
                "blockTime": data.get("timestamp", 0)
            }

    return {"verified": False, "error": "Transfer details do not match"}


async def verify_bep20_payment(
    tx_hash: str,
    expected_to: str,
    expected_amount_usdt: float,
    order_id: str
) -> dict:
    """Verify USDT BEP20 transaction via BscScan API"""
    api_key = os.environ.get("BSCSCAN_API_KEY", "")
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                BSCSCAN_API,
                params={
                    "module": "account",
                    "action": "tokentx",
                    "contractaddress": USDT_BEP20_CONTRACT,
                    "txhash": tx_hash,
                    "apikey": api_key
                }
            )
            data = resp.json()
        except Exception as e:
            return {"verified": False, "error": str(e)}

    if data.get("status") != "1":
        # Try transaction receipt fallback
        try:
            resp2 = await client.get(
                BSCSCAN_API,
                params={
                    "module": "proxy",
                    "action": "eth_getTransactionReceipt",
                    "txhash": tx_hash,
                    "apikey": api_key
                }
            )
            receipt = resp2.json().get("result", {})
            if receipt.get("status") != "0x1":
                return {"verified": False, "error": "TX failed or not found on BSC"}
        except:
            pass
        return {"verified": False, "error": "BEP20 TX not found"}

    for tx in data.get("result", []):
        if tx.get("hash", "").lower() != tx_hash.lower():
            continue
        to_addr = tx.get("to", "")
        decimals = int(tx.get("tokenDecimal", 18))
        amount_raw = int(tx.get("value", "0"))
        amount_usdt = amount_raw / (10 ** decimals)

        if (to_addr.lower() == expected_to.lower()
                and abs(amount_usdt - expected_amount_usdt) < 0.01):
            return {
                "verified": True,
                "network": "BEP20",
                "txHash": tx_hash,
                "amount": amount_usdt,
                "toAddress": to_addr,
                "blockTime": int(tx.get("timeStamp", 0)) * 1000
            }

    return {"verified": False, "error": "BEP20 transfer details do not match"}


async def verify_payment(
    network: str,
    tx_hash: str,
    expected_to: str,
    expected_amount: float,
    order_id: str
) -> dict:
    """Route to correct network verifier"""
    if network == "TRC20":
        return await verify_trc20_payment(tx_hash, expected_to, expected_amount, order_id)
    elif network == "BEP20":
        return await verify_bep20_payment(tx_hash, expected_to, expected_amount, order_id)
    else:
        return {"verified": False, "error": f"Unsupported network: {network}"}
