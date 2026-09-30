from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..database import get_db
from ..models import Batch, Pond, StockingRecord, FeedingRecord, CostRecord, HarvestSale, WaterQualityRecord, MedicationRecord
from ..schemas import CultureCycleAnalysis, BatchTraceability, BatchInfo, PondInfo, HarvestSaleTrace
from ..pricing import PRICING_VERSION
from ..services import sales as sales_service

router = APIRouter(
    prefix="/api/analysis",
    tags=["养殖周期分析"]
)


def _settlement_context(db: Session, batch: Batch, version: str):
    """返回（是否已在该版本结算, 结算单号）；版本不匹配时不视为同截止版本。"""
    settlement = sales_service.get_settlement(db, batch.id)
    if settlement and settlement.cutoff_version == version:
        return True, settlement.settlement_no
    return False, (settlement.settlement_no if settlement else None)


def _require_known_version(version: str) -> str:
    """只允许在已发布的计价版本下做截止版本分析，拒绝未知版本口径。"""
    if version != PRICING_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的计价截止版本 {version!r}，当前生效版本为 {PRICING_VERSION}",
        )
    return version


def _sale_trace(r: HarvestSale) -> HarvestSaleTrace:
    return HarvestSaleTrace(
        id=r.id,
        sale_date=r.sale_date,
        weight=r.weight,
        unit_price=r.unit_price,
        total_amount=r.total_amount,
        buyer=r.buyer,
        status=r.status,
        price_scale=r.price_scale,
        pricing_version=r.pricing_version,
        is_settled=bool(r.is_settled),
        supersedes_id=r.supersedes_id,
    )


@router.get("/cycle/{batch_id}/", response_model=CultureCycleAnalysis)
def analyze_cycle(
    batch_id: int,
    version: str = PRICING_VERSION,
    db: Session = Depends(get_db),
):
    _require_known_version(version)
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()

    # 收入与出塘重量只取当前生效（active）销售行，冲正行不计入。
    active_sales = sales_service.effective_sales(db, batch.id)
    total_revenue_dec = sales_service.revenue_decimal(db, batch.id, version)
    total_revenue = float(total_revenue_dec)
    harvest_weight = sum(float(s.weight) for s in active_sales)

    initial_quantity = db.query(func.sum(StockingRecord.quantity)).filter(
        StockingRecord.batch_id == batch.id
    ).scalar() or 0

    feed_total = db.query(func.sum(FeedingRecord.feed_quantity)).filter(
        FeedingRecord.batch_id == batch.id
    ).scalar() or 0

    total_cost = db.query(func.sum(CostRecord.amount)).filter(
        CostRecord.batch_id == batch.id
    ).scalar() or 0

    harvest_date = batch.actual_harvest_date
    days_cultured = None
    if harvest_date:
        days_cultured = (harvest_date - batch.stocking_date).days

    survival_rate = 0
    if initial_quantity > 0 and harvest_weight > 0:
        avg_weight_per_fish = 0.5
        estimated_survival = harvest_weight / avg_weight_per_fish
        survival_rate = (estimated_survival / initial_quantity) * 100

    feed_conversion_ratio = 0
    if harvest_weight > 0 and feed_total > 0:
        feed_conversion_ratio = feed_total / harvest_weight

    yield_per_mu = 0
    if pond and pond.area > 0:
        yield_per_mu = harvest_weight / pond.area

    profit = total_revenue - total_cost
    settled, settlement_no = _settlement_context(db, batch, version)

    costs = db.query(
        CostRecord.cost_type,
        func.sum(CostRecord.amount).label('total')
    ).filter(
        CostRecord.batch_id == batch.id
    ).group_by(CostRecord.cost_type).all()

    cost_breakdown = {c.cost_type: c.total for c in costs}

    known_types = ['feed', 'medicine', 'labor', 'electricity']
    other_cost = sum(
        amount for cost_type, amount in cost_breakdown.items()
        if cost_type not in known_types
    )

    cost_summary_dict = {
        "feed_cost": cost_breakdown.get('feed', 0),
        "medicine_cost": cost_breakdown.get('medicine', 0),
        "labor_cost": cost_breakdown.get('labor', 0),
        "electricity_cost": cost_breakdown.get('electricity', 0),
        "other_cost": other_cost,
        "total_cost": total_cost
    }

    feeding_summary_dict = db.query(
        FeedingRecord.feed_type,
        func.sum(FeedingRecord.feed_quantity).label('total_quantity'),
        func.count(FeedingRecord.id).label('feeding_count')
    ).filter(
        FeedingRecord.batch_id == batch.id
    ).group_by(FeedingRecord.feed_type).all()

    total_feed_weight = feed_total
    feeding_count = sum(f.feeding_count for f in feeding_summary_dict)
    avg_daily_feed = 0
    if days_cultured and days_cultured > 0:
        avg_daily_feed = total_feed_weight / days_cultured

    feeding_summary_result = {
        "total_feed_weight": total_feed_weight,
        "feeding_count": feeding_count,
        "avg_daily_feed": avg_daily_feed
    }

    return CultureCycleAnalysis(
        batch_number=batch.batch_number,
        pond_name=pond.name if pond else "未知",
        species=batch.species,
        stocking_date=batch.stocking_date,
        harvest_date=harvest_date,
        days_cultured=days_cultured,
        initial_quantity=initial_quantity,
        harvest_weight=harvest_weight,
        survival_rate=round(survival_rate, 2),
        feed_total=feed_total,
        feed_conversion_ratio=round(feed_conversion_ratio, 2),
        area=pond.area if pond else 0,
        yield_per_mu=round(yield_per_mu, 2),
        total_cost=total_cost,
        total_revenue=total_revenue,
        profit=round(profit, 2),
        revenue_version=version,
        sale_count=len(active_sales),
        settled=settled,
        settlement_no=settlement_no,
        cost_summary=cost_summary_dict,
        feeding_summary=feeding_summary_result
    )

@router.get("/traceability/{batch_id}/", response_model=BatchTraceability)
def batch_traceability(
    batch_id: int,
    version: str = PRICING_VERSION,
    db: Session = Depends(get_db),
):
    _require_known_version(version)
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()

    stocking_records = db.query(StockingRecord).filter(
        StockingRecord.batch_id == batch.id
    ).all()

    feeding_records = db.query(FeedingRecord).filter(
        FeedingRecord.batch_id == batch.id
    ).all()

    water_quality_records = db.query(WaterQualityRecord).filter(
        WaterQualityRecord.batch_id == batch.id
    ).all()

    medication_records = db.query(MedicationRecord).filter(
        MedicationRecord.batch_id == batch.id
    ).all()

    cost_records = db.query(CostRecord).filter(
        CostRecord.batch_id == batch.id
    ).all()

    # 与周期分析同一截止版本、同一生效链口径。
    active_sales = sales_service.effective_sales(db, batch.id)
    reversed_sales = db.query(HarvestSale).filter(
        HarvestSale.batch_id == batch.id,
        HarvestSale.status == "reversed",
    ).order_by(HarvestSale.id).all()
    total_revenue = float(sales_service.revenue_decimal(db, batch.id, version))
    settled, settlement_no = _settlement_context(db, batch, version)

    return BatchTraceability(
        batch=BatchInfo(
            batch_number=batch.batch_number,
            species=batch.species,
            stocking_date=batch.stocking_date,
            harvest_date=batch.actual_harvest_date,
            status=batch.status,
            pond_id=batch.pond_id
        ),
        pond_info=PondInfo(
            name=pond.name if pond else None,
            area=pond.area if pond else None,
            water_depth=pond.water_depth if pond else None
        ),
        stocking_records=[
            {
                "species": r.species,
                "quantity": r.quantity,
                "source": r.source,
                "batch_number": r.batch_number,
                "stocking_date": r.created_at.date() if hasattr(r, 'created_at') else None
            } for r in stocking_records
        ],
        feeding_records=[
            {
                "feeding_date": r.feeding_date,
                "feed_type": r.feed_type,
                "quantity": r.feed_quantity,
                "unit": "kg"
            } for r in feeding_records
        ],
        water_quality_records=[
            {
                "record_date": r.record_date,
                "water_temperature": r.water_temperature,
                "ph_value": r.ph_value,
                "dissolved_oxygen": r.dissolved_oxygen
            } for r in water_quality_records
        ],
        medication_records=[
            {
                "medication_date": r.medication_date,
                "medication_name": r.drug_name,
                "dosage": r.dosage,
                "unit": r.dosage_unit
            } for r in medication_records
        ],
        cost_records=[
            {
                "cost_date": r.cost_date,
                "cost_type": r.cost_type,
                "amount": r.amount,
                "description": r.description
            } for r in cost_records
        ],
        harvest_sales=[_sale_trace(r) for r in active_sales],
        reversed_sales=[_sale_trace(r) for r in reversed_sales],
        total_revenue=total_revenue,
        revenue_version=version,
        sale_count=len(active_sales),
        settled=settled,
        settlement_no=settlement_no,
    )

@router.get("/trace-by-number/{batch_number}/", response_model=BatchTraceability)
def trace_by_batch_number(
    batch_number: str,
    version: str = PRICING_VERSION,
    db: Session = Depends(get_db),
):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise HTTPException(status_code=404, detail=f"批次号 {batch_number} 不存在")
    return batch_traceability(batch.id, version, db)
