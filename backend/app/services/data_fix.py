"""历史金额分批识别与修复。

按主键游标分批扫描 harvest_sales，把三类问题条目识别出来：
- inconsistent   总金额与 重量×单价（按计价精度）不一致
- missing        总金额缺失
- over_precision 总金额超出计价精度

处理策略：
- 未签署（locked_version 为空）：在原条目上自动校正（仅重算金额字段）；
- 已进入结算：通过冲正+替代修复，绝不在签署快照上原地覆盖；
- auto_apply=False：只识别不处理，已签署/未签署均标记 pending。

每批在一个 BEGIN IMMEDIATE 事务内完成"扫描-修复-推进游标"，可重复调用直到
finished=True；重复调用不会重复修复（已有 fix item 跳过）。
"""

import hashlib
import uuid
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import or_

from ..database import transactional_session
from ..models import AmountFixBatch, AmountFixItem, HarvestSale
from ..services import pricing
from .sales_service import FIX_CORRECTION_PREFIX, recompute_in_tx

_OPEN = "open"
_SCANNING = "scanning"
_COMPLETED = "completed"


def _fix_correction_id(batch_ref: str, sale_id: int) -> str:
    digest = hashlib.md5(batch_ref.encode("utf-8")).hexdigest()[:10]
    return f"{FIX_CORRECTION_PREFIX}{sale_id}:{digest}"


def _classify(sale: HarvestSale):
    """返回 (issue, old_amount, expected) 或 None。"""
    try:
        consistent, missing, over, expected = pricing.diagnose(
            sale.total_amount, sale.weight, sale.unit_price, sale.price_scale
        )
    except pricing.PriceValidationError:
        return None
    if consistent:
        return None
    if missing:
        issue = "missing"
    elif over:
        # 金额小数位超出计价精度（无论四舍五入后是否同值）
        issue = "over_precision"
    else:
        issue = "inconsistent"
    return issue, sale.total_amount, expected


def _summary(fix: AmountFixBatch, issues: List[Dict]) -> Dict:
    return {
        "batch_ref": fix.batch_ref,
        "status": fix.status,
        "last_sale_id": fix.last_sale_id,
        "total_scanned": fix.total_scanned,
        "total_inconsistent": fix.total_inconsistent,
        "total_missing": fix.total_missing,
        "total_over_precision": fix.total_over_precision,
        "total_auto_fixed": fix.total_auto_fixed,
        "total_reversed": fix.total_reversed,
        "total_blocked": fix.total_blocked,
        "finished": fix.status == _COMPLETED,
        "issues": issues,
    }


def start_fix_batch(
    batch_ref: Optional[str] = None,
    batch_id: Optional[int] = None,
    batch_size: int = 100,
    auto_apply: bool = True,
) -> Dict:
    batch_ref = batch_ref or f"fix-{uuid.uuid4().hex[:16]}"
    with transactional_session() as db:
        exists = db.query(AmountFixBatch).filter(
            AmountFixBatch.batch_ref == batch_ref
        ).first()
        if exists is not None:
            # 同一修复批次重复开启：继续推进下一批，而不是重建
            return _run_chunk(db, exists, batch_id, batch_size, auto_apply)

        fix = AmountFixBatch(batch_ref=batch_ref, status=_SCANNING)
        db.add(fix)
        db.flush()
        return _run_chunk(db, fix, batch_id, batch_size, auto_apply)


def run_fix_chunk(batch_ref: str, batch_size: int = 100, auto_apply: bool = True,
                  batch_id: Optional[int] = None) -> Dict:
    with transactional_session() as db:
        fix = db.query(AmountFixBatch).filter(
            AmountFixBatch.batch_ref == batch_ref
        ).first()
        if fix is None:
            raise HTTPException(status_code=404, detail="修复批次不存在")
        if fix.status == _COMPLETED:
            return _summary(fix, [])
        return _run_chunk(db, fix, batch_id, batch_size, auto_apply)


def get_fix_batch(batch_ref: str) -> Dict:
    with transactional_session() as db:
        fix = db.query(AmountFixBatch).filter(
            AmountFixBatch.batch_ref == batch_ref
        ).first()
        if fix is None:
            raise HTTPException(status_code=404, detail="修复批次不存在")
        return _summary(fix, [])


def _run_chunk(db, fix: AmountFixBatch, batch_id: Optional[int],
               batch_size: int, auto_apply: bool) -> Dict:
    query = db.query(HarvestSale).filter(
        HarvestSale.id > fix.last_sale_id,
        HarvestSale.amount_status == "active",
        # 修复自身产生的替代条目不再重复扫描
        or_(
            HarvestSale.correction_id.is_(None),
            ~HarvestSale.correction_id.like(f"{FIX_CORRECTION_PREFIX}%"),
        ),
    )
    if batch_id is not None:
        query = query.filter(HarvestSale.batch_id == batch_id)
    rows = query.order_by(HarvestSale.id.asc()).limit(batch_size).all()

    issue_payloads: List[Dict] = []
    already_item_ids = {
        row[0] for row in db.query(AmountFixItem.sale_id)
        .filter(AmountFixItem.fix_batch_id == fix.id).all()
    }

    for sale in rows:
        fix.total_scanned += 1
        classified = _classify(sale)
        if classified is None:
            continue
        issue, old_amount, expected = classified
        if sale.id in already_item_ids:
            continue

        if issue == "missing":
            fix.total_missing += 1
        elif issue == "inconsistent":
            fix.total_inconsistent += 1
        else:
            fix.total_over_precision += 1

        signed = sale.locked_version is not None
        action = "pending"
        result_sale_id = None
        if auto_apply:
            correction_id = _fix_correction_id(fix.batch_ref, sale.id)
            action = recompute_in_tx(db, sale, correction_id)
            if action == "auto_fixed":
                fix.total_auto_fixed += 1
                result_sale_id = sale.id
            elif action == "reversal":
                fix.total_reversed += 1
                result_sale_id = sale.replaced_by_id
        if action == "pending":
            fix.total_blocked += 1

        db.add(AmountFixItem(
            fix_batch_id=fix.id,
            sale_id=sale.id,
            issue=issue,
            old_amount=old_amount,
            expected_amount=expected,
            signed=signed,
            action=action,
            result_sale_id=result_sale_id,
        ))
        issue_payloads.append({
            "sale_id": sale.id,
            "batch_id": sale.batch_id,
            "issue": issue,
            "old_amount": old_amount,
            "expected_amount": expected,
            "signed": signed,
            "action": action,
            "result_sale_id": result_sale_id,
        })

    if rows:
        fix.last_sale_id = rows[-1].id
    if len(rows) < batch_size:
        fix.status = _COMPLETED
    else:
        fix.status = _SCANNING

    db.flush()
    result = _summary(fix, issue_payloads)
    return result
