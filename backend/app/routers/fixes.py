"""历史金额分批修复接口。"""

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AmountFixBatch
from ..schemas import FixBatchCreate, FixBatchResponse
from ..services import data_fix

router = APIRouter(
    prefix="/api/amount-fixes",
    tags=["历史金额修复"]
)


class FixChunkRequest(BaseModel):
    batch_size: int = Field(100, ge=1, le=1000)
    auto_apply: bool = True


@router.post("/", response_model=FixBatchResponse)
def start_fix_batch(payload: FixBatchCreate):
    """开启一个修复批次并立即处理第一批。

    重复使用相同 batch_ref 调用不会重建，而是继续推进下一批（天然支持重试）。
    """
    return data_fix.start_fix_batch(
        batch_ref=payload.batch_ref,
        batch_id=payload.batch_id,
        batch_size=payload.batch_size,
        auto_apply=payload.auto_apply,
    )


@router.post("/{batch_ref}/chunks/", response_model=FixBatchResponse)
def run_next_chunk(batch_ref: str, payload: FixChunkRequest = FixChunkRequest(),
                   batch_id: Optional[int] = None):
    """推进下一批；全部识别修复完成后 finished=true。"""
    return data_fix.run_fix_chunk(
        batch_ref=batch_ref,
        batch_size=payload.batch_size,
        auto_apply=payload.auto_apply,
        batch_id=batch_id,
    )


@router.get("/{batch_ref}/", response_model=FixBatchResponse)
def get_fix_batch(batch_ref: str):
    return data_fix.get_fix_batch(batch_ref)


@router.get("/", response_model=List[FixBatchResponse])
def list_fix_batches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    rows = (
        db.query(AmountFixBatch)
        .order_by(AmountFixBatch.id.desc())
        .offset(skip).limit(limit).all()
    )
    return [
        {
            "batch_ref": f.batch_ref,
            "status": f.status,
            "last_sale_id": f.last_sale_id,
            "total_scanned": f.total_scanned,
            "total_inconsistent": f.total_inconsistent,
            "total_missing": f.total_missing,
            "total_over_precision": f.total_over_precision,
            "total_auto_fixed": f.total_auto_fixed,
            "total_reversed": f.total_reversed,
            "total_blocked": f.total_blocked,
            "finished": f.status == "completed",
            "issues": [],
        }
        for f in rows
    ]
