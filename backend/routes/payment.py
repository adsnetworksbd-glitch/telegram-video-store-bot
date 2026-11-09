from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import uuid, time, os
from services import firebase as fb
from services.blockchain import verify_payment
from services.telegram_auth import verify_telegram_init_data

router = APIRouter()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

def get_payment_config(network: str) -> dict:
    cfg = fb.ref(f"payment_methods/{network}").get()
    if not cfg or not cfg.get("enabled"):
        raise HTTPException(400, f"{network} payment not enabled")
    return cfg

def auth_user(init_data: str) -> dict:
    try:
        return verify_telegram_init_data(init_data)
    except Exception as e:
        raise HTTPException(401, str(e))

# ───────── Create deposit order ─────────
class DepositRequest(BaseModel):
    initData: str
    amount: float
    network: str  # TRC20 | BEP20

@router.post("/deposit/create")
async def create_deposit(req: DepositRequest):
    user = auth_user(req.initData)
    user_id = str(user["id"])

    if req.amount < 1:
        raise HTTPException(400, "Minimum deposit is 1 USDT")

    cfg = get_payment_config(req.network)
    address = cfg.get("address")
    if not address:
        raise HTTPException(400, "Wallet address not configured")

    order_id = f"DEP-{uuid.uuid4().hex[:12].upper()}"
    expires_at = int(time.time() * 1000) + (30 * 60 * 1000)  # 30 min

    fb.ref(f"payment_orders/{order_id}").set({
        "orderId": order_id,
        "userId": user_id,
        "type": "deposit",
        "amount": req.amount,
        "currency": "USDT",
        "network": req.network,
        "toAddress": address,
        "status": "pending",
        "createdAt": int(time.time() * 1000),
        "expiresAt": expires_at
    })

    return {
        "orderId": order_id,
        "address": address,
        "amount": req.amount,
        "network": req.network,
        "expiresAt": expires_at
    }

# ───────── Verify deposit ─────────
class VerifyDepositRequest(BaseModel):
    initData: str
    orderId: str
    txHash: str

@router.post("/deposit/verify")
async def verify_deposit(req: VerifyDepositRequest):
    user = auth_user(req.initData)
    user_id = str(user["id"])

    order = fb.ref(f"payment_orders/{req.orderId}").get()
    if not order:
        raise HTTPException(404, "Order not found")
    if order["userId"] != user_id:
        raise HTTPException(403, "Forbidden")
    if order["status"] != "pending":
        raise HTTPException(400, f"Order is already {order['status']}")
    if int(time.time() * 1000) > order["expiresAt"]:
        fb.ref(f"payment_orders/{req.orderId}/status").set("expired")
        raise HTTPException(400, "Order expired")

    # Check tx hash not reused
    existing = fb.ref("wallet_transactions").order_by_child("txHash").equal_to(req.txHash).get()
    if existing:
        raise HTTPException(400, "Transaction hash already used")

    # Update status to verifying
    fb.ref(f"payment_orders/{req.orderId}").update({
        "status": "verifying",
        "txHash": req.txHash
    })

    result = await verify_payment(
        network=order["network"],
        tx_hash=req.txHash,
        expected_to=order["toAddress"],
        expected_amount=order["amount"],
        order_id=req.orderId
    )

    if not result["verified"]:
        fb.ref(f"payment_orders/{req.orderId}").update({
            "status": "failed",
            "failReason": result.get("error")
        })
        raise HTTPException(400, f"Verification failed: {result.get('error')}")

    # Credit wallet
    tx_ref = f"DEP-TX-{req.txHash[:20]}"
    credit_result = fb.credit_wallet(
        user_id=user_id,
        amount=order["amount"],
        tx_ref=tx_ref,
        reason=f"Deposit via {order['network']}"
    )

    fb.ref(f"payment_orders/{req.orderId}").update({
        "status": "paid",
        "verifiedAt": int(time.time() * 1000),
        "txHash": req.txHash
    })

    fb.log_audit("deposit_verified", user_id, req.orderId, {
        "amount": order["amount"], "network": order["network"], "txHash": req.txHash
    })

    return {
        "success": True,
        "amount": order["amount"],
        "newBalance": credit_result["balance"]
    }

# ───────── Create purchase order ─────────
class PurchaseRequest(BaseModel):
    initData: str
    itemId: str
    itemType: str   # content | package
    paymentMethod: str  # wallet | TRC20 | BEP20

@router.post("/purchase/create")
async def create_purchase(req: PurchaseRequest):
    user = auth_user(req.initData)
    user_id = str(user["id"])

    # Check already has active entitlement
    if fb.check_entitlement(user_id, req.itemId):
        raise HTTPException(400, "Already have active access")

    # Get price & duration
    if req.itemType == "content":
        pricing = fb.ref(f"premium_pricing/{req.itemId}").get()
        if not pricing or not pricing.get("purchaseEnabled"):
            raise HTTPException(400, "Purchase not enabled for this content")
        price = float(pricing["price"])
        duration = int(pricing.get("duration", 30))
    elif req.itemType == "package":
        pkg = fb.ref(f"packages/{req.itemId}").get()
        if not pkg or not pkg.get("isActive"):
            raise HTTPException(400, "Package not available")
        price = float(pkg["price"])
        duration = int(pkg.get("duration", 30))
    else:
        raise HTTPException(400, "Invalid itemType")

    order_id = f"PUR-{uuid.uuid4().hex[:12].upper()}"
    expires_at = int(time.time() * 1000) + (30 * 60 * 1000)

    order_data = {
        "orderId": order_id,
        "userId": user_id,
        "type": "purchase",
        "itemId": req.itemId,
        "itemType": req.itemType,
        "paymentMethod": req.paymentMethod,
        "amount": price,
        "currency": "USDT",
        "duration": duration,
        "status": "pending",
        "createdAt": int(time.time() * 1000),
        "expiresAt": expires_at
    }

    if req.paymentMethod == "wallet":
        # Immediate wallet debit
        try:
            tx_ref = f"PUR-{order_id}"
            fb.debit_wallet(user_id, price, tx_ref, f"Purchase {req.itemId}")
        except ValueError as e:
            raise HTTPException(400, str(e))

        fb.grant_entitlement(user_id, req.itemId, req.itemType, duration, order_id)
        order_data["status"] = "paid"
        order_data["paidAt"] = int(time.time() * 1000)

        # Package: grant all included contents
        if req.itemType == "package":
            pkg = fb.ref(f"packages/{req.itemId}").get()
            for cid in (pkg.get("contentIds") or []):
                fb.grant_entitlement(user_id, cid, "content", duration, order_id)

        fb.ref(f"purchase_history/{user_id}/{order_id}").set(order_data)
        fb.ref(f"payment_orders/{order_id}").set(order_data)

        fb.log_audit("purchase_wallet", user_id, req.itemId, {
            "amount": price, "orderId": order_id
        })

        return {"success": True, "orderId": order_id, "status": "paid", "method": "wallet"}

    else:
        # Blockchain payment — return payment instructions
        cfg = get_payment_config(req.paymentMethod)
        address = cfg.get("address")
        order_data["toAddress"] = address
        order_data["network"] = req.paymentMethod
        fb.ref(f"payment_orders/{order_id}").set(order_data)

        return {
            "success": True,
            "orderId": order_id,
            "address": address,
            "amount": price,
            "network": req.paymentMethod,
            "expiresAt": expires_at
        }

# ───────── Verify purchase payment ─────────
class VerifyPurchaseRequest(BaseModel):
    initData: str
    orderId: str
    txHash: str

@router.post("/purchase/verify")
async def verify_purchase(req: VerifyPurchaseRequest):
    user = auth_user(req.initData)
    user_id = str(user["id"])

    order = fb.ref(f"payment_orders/{req.orderId}").get()
    if not order:
        raise HTTPException(404, "Order not found")
    if order["userId"] != user_id:
        raise HTTPException(403, "Forbidden")
    if order["status"] != "pending":
        raise HTTPException(400, f"Order already {order['status']}")
    if int(time.time() * 1000) > order["expiresAt"]:
        fb.ref(f"payment_orders/{req.orderId}/status").set("expired")
        raise HTTPException(400, "Order expired")

    # Check tx hash reuse
    existing = fb.ref("wallet_transactions").order_by_child("txHash").equal_to(req.txHash).get()
    if existing:
        raise HTTPException(400, "Transaction already used")

    fb.ref(f"payment_orders/{req.orderId}").update({
        "status": "verifying", "txHash": req.txHash
    })

    result = await verify_payment(
        network=order["network"],
        tx_hash=req.txHash,
        expected_to=order["toAddress"],
        expected_amount=order["amount"],
        order_id=req.orderId
    )

    if not result["verified"]:
        fb.ref(f"payment_orders/{req.orderId}").update({
            "status": "failed",
            "failReason": result.get("error")
        })
        raise HTTPException(400, f"Verification failed: {result.get('error')}")

    # Grant entitlement
    fb.grant_entitlement(user_id, order["itemId"], order["itemType"], order["duration"], req.orderId)

    # Package: grant all included contents
    if order["itemType"] == "package":
        pkg = fb.ref(f"packages/{order['itemId']}").get()
        for cid in (pkg.get("contentIds") or []):
            fb.grant_entitlement(user_id, cid, "content", order["duration"], req.orderId)

    fb.ref(f"payment_orders/{req.orderId}").update({
        "status": "paid",
        "verifiedAt": int(time.time() * 1000)
    })
    fb.ref(f"purchase_history/{user_id}/{req.orderId}").set(order)

    fb.log_audit("purchase_verified", user_id, order["itemId"], {
        "orderId": req.orderId, "txHash": req.txHash
    })

    return {"success": True, "orderId": req.orderId, "status": "paid"}

# ───────── Check order status ─────────
@router.get("/order/{order_id}")
async def get_order_status(order_id: str, init_data: str):
    user = auth_user(init_data)
    order = fb.ref(f"payment_orders/{order_id}").get()
    if not order:
        raise HTTPException(404, "Order not found")
    if order["userId"] != str(user["id"]):
        raise HTTPException(403, "Forbidden")
    return order
