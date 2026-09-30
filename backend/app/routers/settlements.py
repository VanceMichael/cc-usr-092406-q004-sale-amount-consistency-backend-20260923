from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import BatchSettlementResponse, BatchSettlementSummary
from ..services import sales as sales_service
from ..services.sales import ServiceError

router = APIRouter(
    prefix="/api/batches",
    tags=["批次结算"]
)


@router.post("/{batch_id}/settlements/", response_model=BatchSettlementResponse, status_code=201)
def settle_batch(batch_id: int, db: Session = Depends(get_db)):
    """签署结算：一个批次仅允许一次成功结算；事务内固化截止版本收入快照。"""
    try:
        return sales_service.settle_batch(db, batch_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/{batch_id}/settlements/", response_model=BatchSettlementSummary)
def get_batch_settlement(batch_id: int, db: Session = Depends(get_db)):
    settlement = sales_service.get_settlement(db, batch_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="批次尚未结算")
    return settlement
