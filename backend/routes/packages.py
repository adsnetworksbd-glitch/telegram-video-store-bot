from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from services import firebase as fb
from services.telegram_auth import verify_telegram_init_data

router = APIRouter()

@router.get("/list")
async def list_packages():
    pkgs = fb.ref("packages").get() or {}
    result = []
    for k, v in pkgs.items():
        if v.get("isActive"):
            v["id"] = k
            result.append(v)
    return {"packages": result}

@router.get("/premium-pricing")
async def list_pricing():
    pricing = fb.ref("premium_pricing").get() or {}
    return {"pricing": pricing}
