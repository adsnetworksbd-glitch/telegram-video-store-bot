from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.telegram_auth import verify_telegram_init_data

router = APIRouter()

class VerifyRequest(BaseModel):
    initData: str

@router.post("/verify")
async def verify_user(req: VerifyRequest):
    try:
        user = verify_telegram_init_data(req.initData)
        return {"valid": True, "user": user}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
