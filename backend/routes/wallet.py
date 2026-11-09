from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services import firebase as fb
from services.telegram_auth import verify_telegram_init_data

router = APIRouter()

def auth_user(init_data: str) -> dict:
    try:
        return verify_telegram_init_data(init_data)
    except Exception as e:
        raise HTTPException(401, str(e))

class InitDataRequest(BaseModel):
    initData: str

@router.post("/balance")
async def get_balance(req: InitDataRequest):
    user = auth_user(req.initData)
    uid = str(user["id"])
    balance = fb.get_wallet(uid)
    return {"balance": balance, "userId": uid}

@router.post("/transactions")
async def get_transactions(req: InitDataRequest):
    user = auth_user(req.initData)
    uid = str(user["id"])
    txs = fb.ref("wallet_transactions").order_by_child("userId").equal_to(uid).get()
    result = []
    if txs:
        for k, v in txs.items():
            if v.get("userId") == uid:
                v["id"] = k
                result.append(v)
    result.sort(key=lambda x: x.get("createdAt", 0), reverse=True)
    return {"transactions": result[:50]}

@router.post("/entitlement/check")
async def check_entitlement(req: InitDataRequest, item_id: str):
    user = auth_user(req.initData)
    uid = str(user["id"])
    has_access = fb.check_entitlement(uid, item_id)
    return {"hasAccess": has_access}

@router.post("/entitlements")
async def get_entitlements(req: InitDataRequest):
    user = auth_user(req.initData)
    uid = str(user["id"])
    ents = fb.ref(f"user_entitlements/{uid}").get() or {}
    import time
    now = int(time.time() * 1000)
    result = {}
    for item_id, ent in ents.items():
        expires = ent.get("expiresAt", -1)
        is_active = ent.get("isActive") and (expires == -1 or now < expires)
        result[item_id] = {**ent, "currentlyActive": is_active}
    return {"entitlements": result}
