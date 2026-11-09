from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List
import os, uuid, time
from services import firebase as fb

router = APIRouter()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

def require_admin(x_admin_secret: str = Header(...)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(403, "Invalid admin secret")

# ───────── Payment Methods Config ─────────
class PaymentMethodConfig(BaseModel):
    enabled: bool
    address: Optional[str] = None
    minAmount: Optional[float] = 1.0
    maxAmount: Optional[float] = 10000.0

@router.put("/payment-methods/{network}")
async def set_payment_method(network: str, cfg: PaymentMethodConfig, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    if network not in ["TRC20", "BEP20"]:
        raise HTTPException(400, "Invalid network")
    fb.ref(f"payment_methods/{network}").set(cfg.dict())
    fb.log_audit("payment_method_updated", "admin", network, cfg.dict())
    return {"success": True}

@router.get("/payment-methods")
async def get_payment_methods(x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    return fb.ref("payment_methods").get() or {}

# ───────── Package Management ─────────
class PackageData(BaseModel):
    name: str
    description: str
    image: Optional[str] = ""
    price: float
    duration: int
    contentIds: List[str] = []
    isActive: bool = True
    purchaseLimit: Optional[int] = None

@router.post("/packages")
async def create_package(pkg: PackageData, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    new_ref = fb.ref("packages").push()
    data = pkg.dict()
    data["createdAt"] = int(time.time() * 1000)
    new_ref.set(data)
    fb.log_audit("package_created", "admin", new_ref.key, data)
    return {"success": True, "id": new_ref.key}

@router.put("/packages/{pkg_id}")
async def update_package(pkg_id: str, pkg: PackageData, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    existing = fb.ref(f"packages/{pkg_id}").get()
    if not existing:
        raise HTTPException(404, "Package not found")
    data = pkg.dict()
    data["updatedAt"] = int(time.time() * 1000)
    fb.ref(f"packages/{pkg_id}").update(data)
    return {"success": True}

@router.delete("/packages/{pkg_id}")
async def delete_package(pkg_id: str, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    fb.ref(f"packages/{pkg_id}").update({"isActive": False, "archivedAt": int(time.time() * 1000)})
    return {"success": True}

@router.get("/packages")
async def get_all_packages(x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    pkgs = fb.ref("packages").get() or {}
    result = []
    for k, v in pkgs.items():
        v["id"] = k
        result.append(v)
    return {"packages": result}

# ───────── Premium Pricing ─────────
class PricingData(BaseModel):
    price: float
    currency: str = "USDT"
    duration: int = 30
    purchaseEnabled: bool = True

@router.put("/pricing/{prompt_id}")
async def set_pricing(prompt_id: str, pricing: PricingData, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    fb.ref(f"premium_pricing/{prompt_id}").set(pricing.dict())
    return {"success": True}

@router.get("/pricing")
async def get_all_pricing(x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    return fb.ref("premium_pricing").get() or {}

# ───────── Payment Orders ─────────
@router.get("/orders")
async def get_orders(status: Optional[str] = None, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    orders = fb.ref("payment_orders").get() or {}
    result = []
    for k, v in orders.items():
        if status and v.get("status") != status:
            continue
        v["id"] = k
        result.append(v)
    result.sort(key=lambda x: x.get("createdAt", 0), reverse=True)
    return {"orders": result[:200]}

# ───────── Wallet Management ─────────
@router.get("/wallets")
async def get_wallets(x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    wallets = fb.ref("wallets").get() or {}
    users = fb.ref("users").get() or {}
    result = []
    for uid, w in wallets.items():
        u = users.get(uid, {})
        result.append({
            "userId": uid,
            "name": u.get("name", "Unknown"),
            "balance": w.get("balance", 0),
            "updatedAt": w.get("updatedAt")
        })
    return {"wallets": result}

class AdminAdjustment(BaseModel):
    userId: str
    amount: float
    type: str  # credit | debit
    reason: str
    adminNote: str

@router.post("/wallet/adjust")
async def adjust_wallet(adj: AdminAdjustment, x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    if not adj.reason or not adj.adminNote:
        raise HTTPException(400, "Reason and admin note required for audit")

    tx_ref = f"ADMIN-ADJ-{uuid.uuid4().hex[:12]}"
    if adj.type == "credit":
        result = fb.credit_wallet(adj.userId, adj.amount, tx_ref, f"Admin: {adj.reason}")
    elif adj.type == "debit":
        try:
            result = fb.debit_wallet(adj.userId, adj.amount, tx_ref, f"Admin: {adj.reason}")
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        raise HTTPException(400, "type must be credit or debit")

    fb.log_audit("admin_wallet_adjust", "admin", adj.userId, {
        "type": adj.type, "amount": adj.amount,
        "reason": adj.reason, "note": adj.adminNote
    })
    return {"success": True, "newBalance": result["balance"]}

# ───────── Analytics ─────────
@router.get("/analytics")
async def get_analytics(x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    orders = fb.ref("payment_orders").get() or {}
    total_revenue = 0
    total_deposits = 0
    total_purchases = 0
    pending = 0
    failed = 0
    by_network = {"TRC20": 0, "BEP20": 0, "wallet": 0}

    for k, v in orders.items():
        if v.get("status") == "paid":
            amt = v.get("amount", 0)
            total_revenue += amt
            if v.get("type") == "deposit":
                total_deposits += amt
            elif v.get("type") == "purchase":
                total_purchases += amt
                pm = v.get("paymentMethod", "wallet")
                by_network[pm] = by_network.get(pm, 0) + amt
        elif v.get("status") == "pending":
            pending += 1
        elif v.get("status") == "failed":
            failed += 1

    users = fb.ref("users").get() or {}
    return {
        "totalUsers": len(users),
        "totalRevenue": round(total_revenue, 2),
        "totalDeposits": round(total_deposits, 2),
        "totalPurchases": round(total_purchases, 2),
        "pendingOrders": pending,
        "failedOrders": failed,
        "byNetwork": by_network
    }

# ───────── Audit Logs ─────────
@router.get("/audit-logs")
async def get_audit_logs(x_admin_secret: str = Header(...)):
    require_admin(x_admin_secret)
    logs = fb.ref("audit_logs").order_by_child("timestamp").limit_to_last(200).get() or {}
    result = []
    for k, v in logs.items():
        v["id"] = k
        result.append(v)
    return {"logs": sorted(result, key=lambda x: x.get("timestamp", 0), reverse=True)}
