import firebase_admin
from firebase_admin import credentials, db
import os, json

_initialized = False

def get_db():
    global _initialized
    if not _initialized:
        sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        db_url = os.environ.get("FIREBASE_DATABASE_URL")
        if not sa or not db_url:
            raise RuntimeError("Firebase env vars not set")
        cred = credentials.Certificate(json.loads(sa))
        firebase_admin.initialize_app(cred, {"databaseURL": db_url})
        _initialized = True
    return db

def ref(path: str):
    return get_db().reference(path)

# --- Wallet atomic operations ---
def credit_wallet(user_id: str, amount: float, tx_ref: str, reason: str) -> dict:
    """Idempotent wallet credit — skips if tx_ref already processed"""
    tx_check = ref(f"wallet_transactions/{tx_ref}").get()
    if tx_check:
        return {"status": "already_credited", "balance": get_wallet(user_id)}
    
    wallet_ref = ref(f"wallets/{user_id}/balance")
    
    def do_credit(current):
        return (current or 0) + amount
    
    wallet_ref.transaction(do_credit)
    
    import time
    ref(f"wallet_transactions/{tx_ref}").set({
        "userId": user_id,
        "type": "credit",
        "amount": amount,
        "reason": reason,
        "createdAt": {".sv": "timestamp"}
    })
    
    new_bal = get_wallet(user_id)
    ref(f"wallets/{user_id}/updatedAt").set({".sv": "timestamp"})
    return {"status": "credited", "balance": new_bal}

def debit_wallet(user_id: str, amount: float, tx_ref: str, reason: str) -> dict:
    """Atomic wallet debit with balance check"""
    tx_check = ref(f"wallet_transactions/{tx_ref}").get()
    if tx_check:
        return {"status": "already_debited"}
    
    bal = get_wallet(user_id)
    if bal < amount:
        raise ValueError(f"Insufficient balance: {bal} < {amount}")
    
    wallet_ref = ref(f"wallets/{user_id}/balance")
    
    def do_debit(current):
        c = current or 0
        if c < amount:
            raise Exception("Insufficient")
        return c - amount
    
    wallet_ref.transaction(do_debit)
    
    ref(f"wallet_transactions/{tx_ref}").set({
        "userId": user_id,
        "type": "debit",
        "amount": amount,
        "reason": reason,
        "createdAt": {".sv": "timestamp"}
    })
    
    ref(f"wallets/{user_id}/updatedAt").set({".sv": "timestamp"})
    return {"status": "debited", "balance": get_wallet(user_id)}

def get_wallet(user_id: str) -> float:
    val = ref(f"wallets/{user_id}/balance").get()
    return float(val or 0)

def grant_entitlement(user_id: str, item_id: str, item_type: str, 
                       duration_days: int, order_id: str):
    """Grant access to content or package"""
    import time
    now_ms = int(time.time() * 1000)
    expires_ms = now_ms + (duration_days * 86400 * 1000) if duration_days > 0 else -1
    
    ref(f"user_entitlements/{user_id}/{item_id}").set({
        "type": item_type,
        "grantedAt": now_ms,
        "expiresAt": expires_ms,
        "orderId": order_id,
        "isActive": True
    })

def check_entitlement(user_id: str, item_id: str) -> bool:
    import time
    ent = ref(f"user_entitlements/{user_id}/{item_id}").get()
    if not ent:
        return False
    if not ent.get("isActive"):
        return False
    expires = ent.get("expiresAt", -1)
    if expires == -1:
        return True
    return int(time.time() * 1000) < expires

def log_audit(action: str, actor_id: str, target_id: str, details: dict):
    import time
    ref("audit_logs").push({
        "action": action,
        "actorId": actor_id,
        "targetId": target_id,
        "details": details,
        "timestamp": int(time.time() * 1000)
    })
