from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from ..database import get_db
from ..models import HarvestSale, SaleCorrection
from ..schemas import (
    HarvestSaleCreate, HarvestSaleUpdate, HarvestSaleResponse,
    SaleCorrectionCreate, SaleCorrectionResponse,
    DataIssue, DataRepairReport,
)
from ..services import sales as sales_service
from ..services.sales import ServiceError

router = APIRouter(
    prefix="/api/harvest-sales",
    tags=["出塘销售"]
)


def _raise(exc: ServiceError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _present(sale: HarvestSale) -> HarvestSaleResponse:
    """按当前计价版本重算有效行金额后呈现，使销售详情与分析/追溯同口径。"""
    resp = HarvestSaleResponse.model_validate(sale)
    if sale.status == "active":
        try:
            resp.total_amount = float(sales_service.expected_amount(sale))
        except Exception:
            pass
    return resp


def _correction_response(corr: SaleCorrection, replayed: bool) -> SaleCorrectionResponse:
    return SaleCorrectionResponse.model_validate(corr).model_copy(update={"replayed": replayed})


@router.post("/", response_model=HarvestSaleResponse, status_code=201)
def create_harvest_sale(sale: HarvestSaleCreate, db: Session = Depends(get_db)):
    # total_amount 由服务端按 weight × unit_price × price_scale 统一计算，忽略客户端值。
    try:
        return sales_service.create_sale(db, sale)
    except ServiceError as exc:
        _raise(exc)


@router.get("/", response_model=List[HarvestSaleResponse])
def get_harvest_sales(
    skip: int = 0,
    limit: int = 100,
    batch_id: Optional[int] = None,
    include_reversed: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(HarvestSale)
    if batch_id is not None:
        query = query.filter(HarvestSale.batch_id == batch_id)
    if not include_reversed:
        query = query.filter(HarvestSale.status == "active")
    sales = query.order_by(HarvestSale.id).offset(skip).limit(limit).all()
    return [_present(s) for s in sales]


@router.get("/data-issues/", response_model=List[DataIssue])
def list_data_issues(
    batch_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """分批识别历史条目：金额不一致 / 缺失金额 / 超出计价精度（只读，不修改）。"""
    return sales_service.scan_issues(db, batch_id=batch_id, limit=limit, offset=offset)


@router.post("/data-repair/", response_model=DataRepairReport)
def repair_data(
    batch_id: Optional[int] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """分批自动修复未签署记录；已进入结算的记录只报告不覆盖。"""
    return sales_service.repair_unsigned(db, batch_id=batch_id, limit=limit)


@router.get("/corrections/{correction_id}/", response_model=SaleCorrectionResponse)
def get_correction_result(correction_id: str, db: Session = Depends(get_db)):
    """按更正标识查询首次处理结果（响应丢失后的幂等查询）。"""
    corr = sales_service.get_replayed_correction(db, correction_id)
    if corr is None:
        raise HTTPException(status_code=404, detail="更正标识不存在，尚未处理")
    return _correction_response(corr, replayed=True)


@router.post("/{sale_id}/corrections/", response_model=SaleCorrectionResponse)
def correct_harvest_sale(
    sale_id: int,
    payload: SaleCorrectionCreate,
    correction_id: str,
    db: Session = Depends(get_db),
):
    """提交销售更正。correction_id 为调用方幂等键，重复提交只生效一次。"""
    try:
        corr, replayed = sales_service.correct_sale(db, sale_id, correction_id, payload)
    except ServiceError as exc:
        _raise(exc)
    return _correction_response(corr, replayed)


@router.get("/{sale_id}/", response_model=HarvestSaleResponse)
def get_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")
    return _present(sale)


@router.put("/{sale_id}/", response_model=HarvestSaleResponse)
def update_harvest_sale(sale_id: int, sale: HarvestSaleUpdate, db: Session = Depends(get_db)):
    # 金额与计价事实禁止经普通 PUT 修改；统一引导至带幂等标识的更正接口。
    try:
        return sales_service.update_sale_metadata(db, sale_id, sale)
    except ServiceError as exc:
        _raise(exc)


@router.delete("/{sale_id}/")
def delete_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    try:
        sales_service.delete_sale(db, sale_id)
    except ServiceError as exc:
        _raise(exc)
    return {"message": "出塘销售记录删除成功"}
