import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from ..database import get_db
from ..models import HarvestSale, IdempotentResult
from ..schemas import (
    HarvestSaleCreate,
    HarvestSaleResponse,
    HarvestSaleUpdate,
    SaleCorrectionCreate,
    SaleCorrectionResponse,
)
from ..services import sales_service
from ..services.versioning import visible_sales

router = APIRouter(
    prefix="/api/harvest-sales",
    tags=["出塘销售"]
)

# PUT 请求中禁止出现的计价字段：原始事实不可原地修改，金额只能服务端产生
_FORBIDDEN_PRICE_FIELDS = ("weight", "unit_price", "total_amount")


@router.post("/", response_model=HarvestSaleResponse)
def create_harvest_sale(sale: HarvestSaleCreate):
    # total_amount 即便出现在请求体中也被丢弃，统一由服务端计算
    payload = sale.model_dump(exclude={"total_amount"})
    result = sales_service.create_sale(payload)
    result.pop("replayed", None)
    return result


@router.get("/", response_model=List[HarvestSaleResponse])
def get_harvest_sales(
    skip: int = 0,
    limit: int = 100,
    batch_id: int = None,
    cutoff_version: Optional[int] = None,
    db: Session = Depends(get_db),
):
    if batch_id is not None:
        # 批次维度：走统一截止版本口径，与周期分析/追溯收入一致
        rows = visible_sales(db, batch_id, cutoff_version)
        return rows[skip: skip + limit]

    query = db.query(HarvestSale)
    if cutoff_version is not None:
        query = query.filter(HarvestSale.effective_from <= cutoff_version)
    return query.order_by(HarvestSale.id.asc()).offset(skip).limit(limit).all()


@router.get("/by-request/{request_id}/", response_model=HarvestSaleResponse)
def get_sale_by_request(request_id: str, db: Session = Depends(get_db)):
    """响应丢失后凭写入幂等键查询原结果。"""
    record = (
        db.query(IdempotentResult)
        .filter(
            IdempotentResult.scope == "harvest_sale_create",
            IdempotentResult.request_key == request_id,
        )
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="请求标识不存在")
    return json.loads(record.response_json)


@router.get("/corrections/{correction_id}/", response_model=SaleCorrectionResponse)
def get_correction_result(correction_id: str):
    """响应丢失后按更正标识查询原结果（不产生任何副作用）。"""
    return sales_service.get_correction(correction_id)


@router.post("/corrections/{sale_id}/", response_model=SaleCorrectionResponse)
def correct_harvest_sale(sale_id: int, correction: SaleCorrectionCreate):
    return sales_service.apply_correction(
        sale_id=sale_id,
        correction_id=correction.correction_id,
        weight=correction.weight,
        unit_price=correction.unit_price,
        price_scale=correction.price_scale,
        sale_date=correction.sale_date,
        reason=correction.reason,
    )


@router.get("/{sale_id}/", response_model=HarvestSaleResponse)
def get_harvest_sale(
    sale_id: int,
    cutoff_version: Optional[int] = None,
    db: Session = Depends(get_db),
):
    sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")

    if cutoff_version is not None:
        # 截止版本语义：在条目生效版本之前查询返回 404。
        # 被冲正的原条目作为审计事实仍可查询（amount_status=reversed，
        # 由冲正行抵消），与追溯/列表的复式记账口径一致。
        if sale.effective_from > cutoff_version:
            raise HTTPException(status_code=404, detail="该记录在指定截止版本下尚未生效")
    return sale


@router.put("/{sale_id}/", response_model=HarvestSaleResponse)
def update_harvest_sale(sale_id: int, sale: HarvestSaleUpdate, db: Session = Depends(get_db)):
    update_data = sale.model_dump(exclude_unset=True)

    forbidden = [f for f in _FORBIDDEN_PRICE_FIELDS if f in update_data]
    if forbidden:
        raise HTTPException(
            status_code=409,
            detail="重量/单价/总金额为可追溯原始事实，不允许原地修改；"
                   "请使用 /api/harvest-sales/corrections/{sale_id}/ 更正",
        )

    db_sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not db_sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")
    if db_sale.amount_status != "active":
        raise HTTPException(status_code=409, detail="冲正/被冲正条目为不可变审计记录")

    # 非计价元数据（买家、等级、备注等）允许更新；版本链条目同步元数据不影响金额
    for key, value in update_data.items():
        setattr(db_sale, key, value)

    db.commit()
    db.refresh(db_sale)
    return db_sale


@router.delete("/{sale_id}/")
def delete_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    db_sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not db_sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")
    if db_sale.locked_version is not None:
        raise HTTPException(
            status_code=409,
            detail="记录已进入结算，不允许删除；请通过更正（冲正+替代）处理",
        )
    if db_sale.amount_status != "active":
        raise HTTPException(status_code=409, detail="冲正/替代条目不允许删除")

    db.delete(db_sale)
    db.commit()
    return {"message": "出塘销售记录删除成功"}
