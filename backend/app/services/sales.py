"""销售金额的计算、更正、修复与结算服务。

事务边界：
- 所有写事务均以 ``BEGIN IMMEDIATE`` 取得 SQLite 写锁后再读改，
  销售写入 / 批次结算 / 并发更正彼此串行化，不会出现交错覆盖。
- 金额事实（weight_raw / unit_price_raw / price_scale / pricing_version）
  与 total_amount 在同一事务内落库，保证“事实与结论一致”。

幂等：
- 更正必须带 correction_id（sale_corrections.correction_id 唯一），
  同一标识重复提交只生效一次；丢失响应后可凭标识查询首次结果。
"""

import json
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from ..models import Batch, BatchSettlement, HarvestSale, SaleCorrection
from ..pricing import (
    DEFAULT_PRICE_SCALE,
    PRICING_VERSION,
    compute_amount,
    decimal_places,
    stored_total_to_decimal,
)

# 允许金额比较的最大误差（Decimal 精确比较时兜底，仅吸收历史 float 落库噪声）。
_AMOUNT_EPS = Decimal("0.0000001")


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def begin_write_tx(db) -> None:
    """显式开启写事务并立即取得库级写锁（SQLite BEGIN IMMEDIATE）。

    拿锁超时（仍有别的写事务未结束）转为 409，调用方可安全重试。
    """
    try:
        db.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as exc:
        db.rollback()
        if "locked" in str(exc.orig).lower():
            raise ServiceError(409, "数据库写锁繁忙，请稍后重试") from exc
        raise


# ---------------------------------------------------------------- 事实落库

def _raw_str(value) -> str:
    """保留调用方提交的原始文本（可追溯事实）。"""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def apply_sale_facts(sale: HarvestSale, weight, unit_price, price_scale) -> Decimal:
    """把重量/单价/精度事实写入销售行，并由服务端统一计算总金额。

    返回计算后的 Decimal 金额。
    """
    amount = compute_amount(weight, unit_price, price_scale)
    w_dec = Decimal(_raw_str(weight))
    p_dec = Decimal(_raw_str(unit_price))
    scale = DEFAULT_PRICE_SCALE if price_scale is None else int(price_scale)
    sale.weight_raw = format(w_dec, "f")
    sale.unit_price_raw = format(p_dec, "f")
    sale.weight = float(w_dec)
    sale.unit_price = float(p_dec)
    sale.price_scale = scale
    sale.total_amount = float(amount)
    sale.pricing_version = PRICING_VERSION
    return amount


def expected_amount(sale: HarvestSale, version: str = PRICING_VERSION) -> Decimal:
    """按指定版本与行上事实计算应有金额。"""
    if version != PRICING_VERSION:
        # 当前只有 v1；历史遗留版本统一按 v1 重算（旧规则本身就是错误来源）。
        version = PRICING_VERSION
    return compute_amount(
        sale.weight_raw if sale.weight_raw is not None else sale.weight,
        sale.unit_price_raw if sale.unit_price_raw is not None else sale.unit_price,
        sale.price_scale if sale.price_scale is not None else DEFAULT_PRICE_SCALE,
    )


# ---------------------------------------------------------------- 历史问题识别

def classify_sale(sale: HarvestSale) -> str | None:
    """识别单条历史记录的金额问题：missing / mismatch / over_precision / None。"""
    if sale.status != "active":
        return None
    try:
        expected = expected_amount(sale)
    except Exception:
        return "missing"
    stored = stored_total_to_decimal(sale.total_amount)
    if stored is None:
        return "missing"
    # 值与按事实重算结果一致即视为正常（吸收 float 落库产生的尾零，如 35.0）。
    if abs(stored - expected) <= _AMOUNT_EPS:
        return None
    scale = sale.price_scale if sale.price_scale is not None else DEFAULT_PRICE_SCALE
    if decimal_places(stored) > scale:
        return "over_precision"
    return "mismatch"


def scan_issues(db, batch_id: int | None = None, limit: int = 100, offset: int = 0):
    """分批扫描金额不一致 / 缺失 / 超精度的有效销售记录（只读）。"""
    query = db.query(HarvestSale).filter(HarvestSale.status == "active")
    if batch_id is not None:
        query = query.filter(HarvestSale.batch_id == batch_id)
    query = query.order_by(HarvestSale.id)
    rows = query.offset(offset).limit(limit).all()
    issues = []
    for sale in rows:
        issue_type = classify_sale(sale)
        if issue_type:
            exp = expected_amount(sale)
            issues.append({
                "sale_id": sale.id,
                "batch_id": sale.batch_id,
                "issue_type": issue_type,
                "stored_amount": sale.total_amount,
                "expected_amount": float(exp),
                "price_scale": sale.price_scale,
                "pricing_version": sale.pricing_version,
            })
    return issues


def repair_unsigned(db, batch_id: int | None = None, limit: int = 100):
    """在一个写事务内分批自动修复未签署记录；已签署行只能冲正，计入 skipped。"""
    begin_write_tx(db)
    try:
        query = db.query(HarvestSale).filter(HarvestSale.status == "active")
        if batch_id is not None:
            query = query.filter(HarvestSale.batch_id == batch_id)
        rows = query.order_by(HarvestSale.id).limit(limit).all()

        repaired_ids, settled_ids = [], []
        issues_payload = []
        for sale in rows:
            issue_type = classify_sale(sale)
            if not issue_type and sale.pricing_version == PRICING_VERSION:
                continue
            exp = expected_amount(sale)
            issues_payload.append({
                "sale_id": sale.id,
                "batch_id": sale.batch_id,
                "issue_type": issue_type or "legacy_version",
                "stored_amount": sale.total_amount,
                "expected_amount": float(exp),
                "price_scale": sale.price_scale,
                "pricing_version": sale.pricing_version,
            })
            if sale.is_settled:
                settled_ids.append(sale.id)
                continue
            apply_sale_facts(sale, sale.weight_raw, sale.unit_price_raw, sale.price_scale)
            repaired_ids.append(sale.id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "scanned": len(rows),
        "issues": issues_payload,
        "repaired": len(repaired_ids),
        "skipped_settled": len(settled_ids),
        "repaired_sale_ids": repaired_ids,
        "settled_sale_ids": settled_ids,
    }


# ---------------------------------------------------------------- 写入 / 更正

def create_sale(db, payload) -> HarvestSale:
    data = payload.model_dump()
    data.pop("total_amount", None)  # 客户端金额永不采信
    weight = data.pop("weight")
    unit_price = data.pop("unit_price")
    price_scale = data.pop("price_scale", None)

    begin_write_tx(db)
    try:
        batch = db.query(Batch).filter(Batch.id == data["batch_id"]).first()
        if not batch:
            db.rollback()
            raise ServiceError(404, "批次不存在")
        sale = HarvestSale(**data)
        apply_sale_facts(sale, weight, unit_price, price_scale)
        db.add(sale)
        db.commit()
        db.refresh(sale)
    except ServiceError:
        raise
    except Exception:
        db.rollback()
        raise
    return sale


_FORBIDDEN_PUT_FIELDS = {"weight", "unit_price", "price_scale", "pricing_version"}


def update_sale_metadata(db, sale_id: int, payload) -> HarvestSale:
    begin_write_tx(db)
    try:
        sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
        if not sale:
            db.rollback()
            raise ServiceError(404, "出塘销售记录不存在")
        if sale.status == "reversed":
            db.rollback()
            raise ServiceError(409, "记录已冲正，不能修改；请以替代行为准")
        if sale.is_settled:
            db.rollback()
            raise ServiceError(409, "记录已随批次结算签署，不能原地修改；"
                                   "请通过更正接口冲正并生成替代行")

        data = payload.model_dump(exclude_unset=True)
        if "batch_id" in data and data["batch_id"] != sale.batch_id:
            db.rollback()
            raise ServiceError(400, "销售记录不能跨批次移动，请在目标批次新建并冲正本行")
        if "total_amount" in data:
            db.rollback()
            raise ServiceError(
                400, "总金额只能由服务端按重量×单价与计价精度统一计算，"
                     "更正金额请提交 /api/harvest-sales/{id}/corrections/"
            )
        touched_facts = _FORBIDDEN_PUT_FIELDS & data.keys()
        if touched_facts:
            db.rollback()
            raise ServiceError(
                400, "重量/单价/计价精度属于计价事实，不能直接修改，"
                     "请通过 /api/harvest-sales/{id}/corrections/ 提交更正"
            )
        for key, value in data.items():
            setattr(sale, key, value)
        db.commit()
        db.refresh(sale)
    except ServiceError:
        raise
    except Exception:
        db.rollback()
        raise
    return sale


def get_replayed_correction(db, correction_id: str) -> SaleCorrection | None:
    return db.query(SaleCorrection).filter(
        SaleCorrection.correction_id == correction_id
    ).first()


def correct_sale(db, sale_id: int, correction_id: str, payload) -> tuple[SaleCorrection, bool]:
    """幂等更正。返回（更正审计记录, 是否为重放）。"""
    if not correction_id or not correction_id.strip():
        raise ServiceError(400, "correction_id 不能为空")

    # 先在事务外校验新事实，非法输入直接拒绝且不产生幂等记录。
    new_weight_raw = _raw_str(payload.weight)
    new_price_raw = _raw_str(payload.unit_price)
    new_scale = payload.price_scale if payload.price_scale is not None else DEFAULT_PRICE_SCALE
    new_amount = compute_amount(new_weight_raw, new_price_raw, new_scale)

    metadata_fields = ("sale_date", "buyer", "batch_number", "quality_grade", "notes")
    metadata_overrides = {
        f: getattr(payload, f) for f in metadata_fields
        if getattr(payload, f, None) is not None
    }

    begin_write_tx(db)
    try:
        existing = get_replayed_correction(db, correction_id)
        if existing is not None:
            db.commit()
            return existing, True

        sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
        if not sale:
            db.rollback()
            raise ServiceError(404, "出塘销售记录不存在")
        if sale.status == "reversed":
            db.rollback()
            raise ServiceError(409, "该记录已被冲正，请对当前有效替代行发起更正")

        settled = bool(sale.is_settled)
        old_amount = sale.total_amount
        mode = "reversal" if settled else "inplace"

        correction = SaleCorrection(
            correction_id=correction_id.strip(),
            sale_id=sale.id,
            batch_id=sale.batch_id,
            mode=mode,
            reason=payload.reason,
            old_weight=sale.weight_raw,
            old_unit_price=sale.unit_price_raw,
            old_price_scale=sale.price_scale,
            old_total_amount=old_amount,
            old_pricing_version=sale.pricing_version,
            new_weight=new_weight_raw,
            new_unit_price=new_price_raw,
            new_price_scale=int(new_scale),
            new_total_amount=float(new_amount),
            new_pricing_version=PRICING_VERSION,
        )
        db.add(correction)
        db.flush()  # 取 correction.id；唯一索引在此拦截并发同标识提交

        if settled:
            # 已签署：原行冲正留痕（不原地覆盖），追加有效替代行。
            sale.status = "reversed"
            sale.corrected_by_id = correction.id

            replacement = HarvestSale(
                batch_id=sale.batch_id,
                sale_date=metadata_overrides.get("sale_date", sale.sale_date),
                buyer=metadata_overrides.get("buyer", sale.buyer),
                batch_number=metadata_overrides.get("batch_number", sale.batch_number),
                quality_grade=metadata_overrides.get("quality_grade", sale.quality_grade),
                notes=metadata_overrides.get("notes", sale.notes),
                status="active",
                is_settled=0,
                supersedes_id=sale.id,
                created_by_correction_id=correction.id,
            )
            apply_sale_facts(replacement, new_weight_raw, new_price_raw, new_scale)
            db.add(replacement)
            db.flush()
            correction.reversed_sale_id = sale.id
            correction.replacement_sale_id = replacement.id
        else:
            # 未签署：同一事务内原地校正（事实与结论一起更新，留审计记录）。
            apply_sale_facts(sale, new_weight_raw, new_price_raw, new_scale)
            for field, value in metadata_overrides.items():
                setattr(sale, field, value)
            sale.corrected_by_id = correction.id
            correction.reversed_sale_id = None
            correction.replacement_sale_id = None

        db.commit()
        db.refresh(correction)
        return correction, False
    except IntegrityError as exc:
        db.rollback()
        # 唯一索引冲突 = 并发的相同 correction_id 已先行提交，按重放处理。
        existing = get_replayed_correction(db, correction_id)
        if existing is not None:
            return existing, True
        raise ServiceError(409, f"更正提交冲突，请重试: {exc.orig}")
    except ServiceError:
        raise
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------- 收入口径

def effective_sales(db, batch_id: int):
    """当前生效（active）销售行；reversed 行不参与任何收入/重量汇总。"""
    return db.query(HarvestSale).filter(
        HarvestSale.batch_id == batch_id,
        HarvestSale.status == "active",
    ).order_by(HarvestSale.id).all()


def revenue_decimal(db, batch_id: int, version: str = PRICING_VERSION) -> Decimal:
    """统一收入口径：对生效行按指定计价版本重算金额后求和（Decimal）。

    汇总精度取各生效行 price_scale 的最大值，保证“逐行金额精确之和”
    与销售详情逐行累加在同一截止版本下严格相等。
    """
    total = Decimal("0")
    agg_scale = 0
    for sale in effective_sales(db, batch_id):
        total += expected_amount(sale, version)
        scale = sale.price_scale if sale.price_scale is not None else DEFAULT_PRICE_SCALE
        agg_scale = max(agg_scale, scale)
    if agg_scale:
        total = total.quantize(Decimal(1).scaleb(-agg_scale))
    return total


def revenue(db, batch_id: int, version: str = PRICING_VERSION) -> float:
    return float(revenue_decimal(db, batch_id, version))


# ---------------------------------------------------------------- 批次结算

def settle_batch(db, batch_id: int) -> BatchSettlement:
    begin_write_tx(db)
    try:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if not batch:
            db.rollback()
            raise ServiceError(404, "批次不存在")
        existing = db.query(BatchSettlement).filter(
            BatchSettlement.batch_id == batch_id
        ).first()
        if existing:
            db.rollback()
            raise ServiceError(409, f"批次已结算，结算单号 {existing.settlement_no}；"
                                   f"更正已签署销售必须走冲正+替代")

        # 事务内先修复本批全部未签署历史坏行，避免把错误值固化进快照。
        repaired_ids = []
        pending = db.query(HarvestSale).filter(
            HarvestSale.batch_id == batch_id,
            HarvestSale.status == "active",
        ).order_by(HarvestSale.id).all()
        for sale in pending:
            issue = classify_sale(sale)
            if issue or sale.pricing_version != PRICING_VERSION:
                if not sale.is_settled:
                    apply_sale_facts(sale, sale.weight_raw, sale.unit_price_raw, sale.price_scale)
                    repaired_ids.append(sale.id)

        active = db.query(HarvestSale).filter(
            HarvestSale.batch_id == batch_id,
            HarvestSale.status == "active",
        ).order_by(HarvestSale.id).all()
        total = Decimal("0")
        sale_ids = []
        for sale in active:
            total += expected_amount(sale)
            sale_ids.append(sale.id)
            sale.is_settled = 1

        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        settlement_no = f"STL-{batch_id}-{stamp}-{uuid.uuid4().hex[:8]}"
        settlement = BatchSettlement(
            batch_id=batch_id,
            settlement_no=settlement_no,
            cutoff_version=PRICING_VERSION,
            total_revenue=float(total),
            sale_count=len(sale_ids),
            sale_ids_json=json.dumps(sale_ids),
            repaired_count=len(repaired_ids),
            status="settled",
        )
        db.add(settlement)
        db.commit()
        db.refresh(settlement)
        return settlement
    except IntegrityError as exc:
        db.rollback()
        existing = db.query(BatchSettlement).filter(
            BatchSettlement.batch_id == batch_id
        ).first()
        if existing:
            raise ServiceError(409, f"批次已结算，结算单号 {existing.settlement_no}")
        raise ServiceError(409, f"结算冲突，请重试: {exc.orig}")
    except ServiceError:
        raise
    except Exception:
        db.rollback()
        raise


def get_settlement(db, batch_id: int) -> BatchSettlement | None:
    return db.query(BatchSettlement).filter(
        BatchSettlement.batch_id == batch_id
    ).first()


def delete_sale(db, sale_id: int) -> None:
    begin_write_tx(db)
    try:
        sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
        if not sale:
            db.rollback()
            raise ServiceError(404, "出塘销售记录不存在")
        if sale.is_settled or sale.status == "reversed" \
                or sale.created_by_correction_id or sale.corrected_by_id or sale.supersedes_id:
            db.rollback()
            raise ServiceError(409, "已签署或处于冲正/更正链中的记录不能删除，"
                                   "只能通过冲正+替代更正")
        db.delete(sale)
        db.commit()
    except ServiceError:
        raise
    except Exception:
        db.rollback()
        raise
