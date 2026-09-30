"""销售写入与更正领域服务。

事务边界：所有写操作在独立连接上以 ``BEGIN IMMEDIATE`` 开启单一事务，
"校验-判重-写入-提交"整段串行化并原子提交；并发更正/并发结算靠写锁
+ 唯一约束双重保障。调用方（端点）不另开事务。
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from ..database import transactional_session
from ..models import (
    Batch,
    BatchSettlement,
    CostRecord,
    HarvestSale,
    IdempotentResult,
    SaleCorrection,
    SettlementSaleItem,
)
from ..services import pricing
from ..services.versioning import latest_settlement_version, visible_sales

_CREATE_SCOPE = "harvest_sale_create"


def _hash(obj: Dict[str, Any]) -> str:
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sale_dict(s: HarvestSale) -> Dict[str, Any]:
    return {
        "id": s.id,
        "batch_id": s.batch_id,
        "sale_date": s.sale_date,
        "weight": s.weight,
        "unit_price": s.unit_price,
        "total_amount": s.total_amount,
        "buyer": s.buyer,
        "batch_number": s.batch_number,
        "quality_grade": s.quality_grade,
        "notes": s.notes,
        "price_scale": s.price_scale,
        "amount_version": s.amount_version,
        "amount_status": s.amount_status,
        "root_sale_id": s.root_sale_id,
        "replaced_by_id": s.replaced_by_id,
        "correction_id": s.correction_id,
        "locked_version": s.locked_version,
        "effective_from": s.effective_from,
        "amount_consistent": s.amount_consistent,
        "created_at": s.created_at,
    }


def _load_idempotent(scope: str, key: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        return (
            db.query(IdempotentResult)
            .filter(IdempotentResult.scope == scope, IdempotentResult.request_key == key)
            .first()
        )
    finally:
        db.close()


def create_sale(payload: Dict[str, Any]) -> Dict[str, Any]:
    """创建销售记录。total_amount 永远由服务端按统一规则计算。"""
    request_id = payload.get("request_id")
    request_hash = _hash({
        "batch_id": payload.get("batch_id"),
        "sale_date": payload.get("sale_date"),
        "weight": payload.get("weight"),
        "unit_price": payload.get("unit_price"),
        "price_scale": payload.get("price_scale"),
        "buyer": payload.get("buyer"),
        "batch_number": payload.get("batch_number"),
        "quality_grade": payload.get("quality_grade"),
        "notes": payload.get("notes"),
    })

    # 响应丢失后重放：先查既有结果（读即可，无需写锁）
    if request_id:
        existing = _load_idempotent(_CREATE_SCOPE, request_id)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="同一请求标识对应不同请求体")
            return {**json.loads(existing.response_json), "replayed": True}

    try:
        scale = pricing.normalize_scale(payload.get("price_scale"))
        total = pricing.calculate_total_amount(
            payload["weight"], payload["unit_price"], scale
        )
    except pricing.PriceValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with transactional_session() as db:
        batch = db.query(Batch).filter(Batch.id == payload["batch_id"]).first()
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")

        if request_id:
            existing = (
                db.query(IdempotentResult)
                .filter(
                    IdempotentResult.scope == _CREATE_SCOPE,
                    IdempotentResult.request_key == request_id,
                )
                .first()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise HTTPException(status_code=409, detail="同一请求标识对应不同请求体")
                return {**json.loads(existing.response_json), "replayed": True}

        current_version = latest_settlement_version(db, payload["batch_id"])
        sale = HarvestSale(
            batch_id=payload["batch_id"],
            sale_date=payload["sale_date"],
            weight=float(payload["weight"]),
            unit_price=float(payload["unit_price"]),
            total_amount=total,  # 服务端统一产生，忽略客户端 total_amount
            buyer=payload.get("buyer"),
            batch_number=payload.get("batch_number"),
            quality_grade=payload.get("quality_grade"),
            notes=payload.get("notes"),
            price_scale=scale,
            amount_version=1,
            amount_status="active",
            effective_from=current_version,
            amount_consistent=True,
        )
        db.add(sale)
        db.flush()
        sale.root_sale_id = sale.id

        result = _sale_dict(sale)
        if request_id:
            db.add(IdempotentResult(
                scope=_CREATE_SCOPE,
                request_key=request_id,
                request_hash=request_hash,
                result_id=sale.id,
                response_json=json.dumps(_jsonable(result), ensure_ascii=False, default=str),
            ))
        # 提交后返回
        committed = dict(result)

    return {**committed, "replayed": False}


def _jsonable(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, (datetime,)):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _correction_hash(weight, unit_price, scale, sale_date, reason) -> str:
    return _hash({
        "weight": weight,
        "unit_price": unit_price,
        "price_scale": scale,
        "sale_date": sale_date,
        "reason": reason,
    })


def _correction_payload(record: SaleCorrection, replayed: bool) -> Dict[str, Any]:
    detail = json.loads(record.detail) if record.detail else {}
    return {
        "correction_id": record.correction_id,
        "sale_id": record.sale_id,
        "batch_id": record.batch_id,
        "mode": record.mode,
        "status": record.status,
        "result_sale_id": record.result_sale_id,
        "reversal_sale_id": record.reversal_sale_id,
        "old_amount": detail.get("old_amount"),
        "new_amount": detail.get("new_amount"),
        "detail": record.detail,
        "replayed": replayed,
        "created_at": record.created_at,
    }


def get_correction(correction_id: str) -> Dict[str, Any]:
    """按更正标识查询原结果（响应丢失后使用）。"""
    with transactional_session() as db:
        record = (
            db.query(SaleCorrection)
            .filter(SaleCorrection.correction_id == correction_id)
            .first()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="更正标识不存在")
        return _correction_payload(record, replayed=True)


def apply_correction(
    sale_id: int,
    correction_id: str,
    weight: float,
    unit_price: float,
    price_scale: Optional[int] = None,
    sale_date=None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """对销售条目提交更正；同一 correction_id 只生效一次。"""
    try:
        scale = pricing.normalize_scale(price_scale)
        new_amount = pricing.calculate_total_amount(weight, unit_price, scale)
    except pricing.PriceValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    request_hash = _correction_hash(weight, unit_price, scale, sale_date, reason)

    # 快速重放路径
    with transactional_session() as db:
        existing = (
            db.query(SaleCorrection)
            .filter(SaleCorrection.correction_id == correction_id)
            .first()
        )
        if existing is not None:
            if existing.sale_id != sale_id or existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="更正标识已用于不同的更正请求")
            return _correction_payload(existing, replayed=True)

        target = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
        if target is None:
            raise HTTPException(status_code=404, detail="出塘销售记录不存在")

        root_id = target.root_sale_id or target.id
        head = (
            db.query(HarvestSale)
            .filter(
                HarvestSale.root_sale_id == root_id,
                HarvestSale.amount_status == "active",
            )
            .order_by(HarvestSale.amount_version.desc(), HarvestSale.id.desc())
            .first()
        )
        if head is None:
            raise HTTPException(status_code=409, detail="该销售条目无可更正的生效版本")

        old_amount = head.total_amount
        signed = head.locked_version is not None

        # 先落幂等事实行；并发重复提交时唯一约束裁决，只允许一方继续
        ledger = SaleCorrection(
            correction_id=correction_id,
            sale_id=sale_id,
            batch_id=target.batch_id,
            mode="reversal" if signed else "auto",
            request_hash=request_hash,
            status="applied",
        )
        db.add(ledger)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="更正标识正在被处理或已存在")

        if not signed:
            # 未签署：允许自动校正原始事实（重量/单价/精度/金额就地更新）
            head.weight = float(weight)
            head.unit_price = float(unit_price)
            head.price_scale = scale
            head.total_amount = new_amount
            head.amount_consistent = True
            head.correction_id = correction_id
            if sale_date is not None:
                head.sale_date = sale_date
            ledger.result_sale_id = head.id
            ledger.detail = json.dumps(
                {"old_amount": old_amount, "new_amount": new_amount, "action": "auto_fixed"},
                ensure_ascii=False,
            )
            result_id = head.id
            reversal_id = None
        else:
            # 已进入结算：冲正 + 替代，绝不原地覆盖
            locked_version = head.locked_version
            new_version = head.amount_version + 1
            next_effective = locked_version + 1

            reversal = HarvestSale(
                batch_id=head.batch_id,
                sale_date=head.sale_date,
                weight=-head.weight,
                unit_price=head.unit_price,
                total_amount=(-head.total_amount) if head.total_amount is not None else None,
                buyer=head.buyer,
                batch_number=head.batch_number,
                quality_grade=head.quality_grade,
                notes=(head.notes or "") + f" [冲正 correction={correction_id}]",
                price_scale=head.price_scale,
                amount_version=head.amount_version,
                amount_status="void",
                effective_from=next_effective,
                locked_version=None,
                amount_consistent=True,
                correction_id=correction_id,
            )
            db.add(reversal)
            db.flush()
            reversal.root_sale_id = root_id

            replacement = HarvestSale(
                batch_id=head.batch_id,
                sale_date=sale_date or head.sale_date,
                weight=float(weight),
                unit_price=float(unit_price),
                total_amount=new_amount,
                buyer=head.buyer,
                batch_number=head.batch_number,
                quality_grade=head.quality_grade,
                notes=reason or head.notes,
                price_scale=scale,
                amount_version=new_version,
                amount_status="active",
                effective_from=next_effective,
                locked_version=None,
                amount_consistent=True,
                correction_id=correction_id,
            )
            db.add(replacement)
            db.flush()
            replacement.root_sale_id = root_id

            head.amount_status = "reversed"
            head.replaced_by_id = replacement.id

            ledger.result_sale_id = replacement.id
            ledger.reversal_sale_id = reversal.id
            ledger.detail = json.dumps(
                {
                    "old_amount": old_amount,
                    "new_amount": new_amount,
                    "action": "reversal",
                    "locked_version": locked_version,
                },
                ensure_ascii=False,
            )
            result_id = replacement.id
            reversal_id = reversal.id

        payload = _correction_payload(ledger, replayed=False)
        payload["reversal_sale_id"] = reversal_id
        payload["result_sale_id"] = result_id
        payload["old_amount"] = old_amount
        payload["new_amount"] = new_amount

    return payload


def _settlement_dict(s: BatchSettlement, replayed: bool = False) -> Dict[str, Any]:
    return {
        "id": s.id,
        "batch_id": s.batch_id,
        "version": s.version,
        "settled_at": s.settled_at,
        "revenue": s.revenue,
        "cost": s.cost,
        "profit": s.profit,
        "sale_count": s.sale_count,
        "cutoff_version": s.cutoff_version,
        "idempotency_key": s.idempotency_key,
        "replayed": replayed,
    }


def settle_batch(batch_id: int, idempotency_key: Optional[str]) -> Dict[str, Any]:
    """签署批次结算：快照当前收入/成本/利润并锁定生效销售条目。"""
    with transactional_session() as db:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")

        if idempotency_key:
            prior = (
                db.query(BatchSettlement)
                .filter(BatchSettlement.idempotency_key == idempotency_key)
                .first()
            )
            if prior is not None:
                return _settlement_dict(prior, replayed=True)

        last_version = latest_settlement_version(db, batch_id)
        next_version = last_version + 1

        # 当前生效条目（active/void，effective_from <= next_version）
        entries = visible_sales(db, batch_id, None)

        unfixed = [
            e for e in entries
            if e.amount_status == "active"
            and (e.total_amount is None or not e.amount_consistent)
        ]
        if unfixed:
            raise HTTPException(
                status_code=409,
                detail="批次存在金额缺失或与重量×单价不一致的生效销售记录，"
                       "请先运行历史金额修复批次再结算",
            )

        revenue = pricing.decimal_sum(e.total_amount for e in entries)
        cost = pricing.decimal_sum(
            row[0] for row in db.query(CostRecord.amount)
            .filter(CostRecord.batch_id == batch_id).all()
        )

        settlement = BatchSettlement(
            batch_id=batch_id,
            version=next_version,
            revenue=float(revenue),
            cost=float(cost),
            profit=float(revenue - cost),
            sale_count=sum(1 for e in entries if e.amount_status == "active"),
            cutoff_version=next_version,
            idempotency_key=idempotency_key,
        )
        db.add(settlement)
        db.flush()

        for e in entries:
            db.add(SettlementSaleItem(
                settlement_id=settlement.id,
                sale_id=e.id,
                sale_date=e.sale_date,
                weight=e.weight,
                unit_price=e.unit_price,
                total_amount=e.total_amount,
                price_scale=e.price_scale,
                amount_version=e.amount_version,
                amount_status=e.amount_status,
                root_sale_id=e.root_sale_id,
            ))
            if e.amount_status == "active" and e.locked_version is None:
                # 锁定尚未签署的生效条目；本事务内条目行即快照来源
                e.locked_version = next_version

        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="批次结算并发冲突，请重试")

        return _settlement_dict(settlement, replayed=False)


# 历史数据修复时每条坏数据使用的更正标识前缀
FIX_CORRECTION_PREFIX = "fix:"


def recompute_in_tx(db, sale: HarvestSale, correction_id: str) -> str:
    """在已开启写事务的会话上对单条历史坏数据执行重算修复。

    返回动作：'auto_fixed'（未签署，数据内自动校正）| 'reversal'（已签署，
    冲正+替代）| 'pending'（已签署且不允许自动处理，列入待冲正清单）。
    修复产生的版本链与人工更正完全相同，保证账实相符且可追溯。
    """
    expected = pricing.calculate_total_amount(sale.weight, sale.unit_price, sale.price_scale)
    signed = sale.locked_version is not None

    if not signed:
        sale.total_amount = expected
        sale.amount_consistent = True
        sale.correction_id = correction_id
        return "auto_fixed"

    # 已结算签署：同样执行冲正+替代，但修正后的替代条目承载统一规则重算值
    locked_version = sale.locked_version
    next_effective = locked_version + 1

    reversal = HarvestSale(
        batch_id=sale.batch_id,
        sale_date=sale.sale_date,
        weight=-sale.weight,
        unit_price=sale.unit_price,
        total_amount=(-sale.total_amount) if sale.total_amount is not None else None,
        buyer=sale.buyer,
        batch_number=sale.batch_number,
        quality_grade=sale.quality_grade,
        notes=(sale.notes or "") + f" [历史修复冲正 {correction_id}]",
        price_scale=sale.price_scale,
        amount_version=sale.amount_version,
        amount_status="void",
        effective_from=next_effective,
        locked_version=None,
        amount_consistent=True,
        correction_id=correction_id,
    )
    db.add(reversal)
    db.flush()
    reversal.root_sale_id = sale.root_sale_id or sale.id

    replacement = HarvestSale(
        batch_id=sale.batch_id,
        sale_date=sale.sale_date,
        weight=sale.weight,
        unit_price=sale.unit_price,
        total_amount=expected,
        buyer=sale.buyer,
        batch_number=sale.batch_number,
        quality_grade=sale.quality_grade,
        notes=(sale.notes or "") + " [历史修复替代]",
        price_scale=sale.price_scale,
        amount_version=sale.amount_version + 1,
        amount_status="active",
        effective_from=next_effective,
        locked_version=None,
        amount_consistent=True,
        correction_id=correction_id,
    )
    db.add(replacement)
    db.flush()
    replacement.root_sale_id = sale.root_sale_id or sale.id

    sale.amount_status = "reversed"
    sale.replaced_by_id = replacement.id

    return "reversal"
