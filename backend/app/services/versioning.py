"""金额版本链与截止版本可见性。

同一条销售事实在"已签署后更正"形成版本链（root_sale_id 相同），采用复式记账：

    原条目   amount_status=reversed，金额为旧值（保留账上，不删除、不原地覆盖）
    冲正条目 amount_status=void，     金额为旧值的相反数，抵消原条目
    替代条目 amount_status=active，   承载更正后的重量/单价/金额

三者之和恰为更正后的事实；在冲正生效版本之前的截止版本只能看到原条目，
因此任意历史截止版本的收入都可精确复现。未签署记录的更正直接自动校正，
不产生冲正行。

周期分析、追溯接口、销售列表/详情必须经由 :func:`visible_sales` 取数，
保证同一 ``cutoff_version`` 下三处收入完全相同：

- cutoff 命中已签署结算版本：以结算快照为准（复现签署时账面层）；
- 否则取 effective_from <= cutoff 的全部账目行；cutoff 缺省表示当前（+∞）。
"""

from decimal import Decimal
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models import BatchSettlement, HarvestSale, SettlementSaleItem
from .pricing import decimal_sum

# 当前（未指定截止版本）的逻辑版本上界
CURRENT = 10 ** 9


def latest_settlement_version(db: Session, batch_id: int) -> int:
    row = (
        db.query(BatchSettlement.version)
        .filter(BatchSettlement.batch_id == batch_id)
        .order_by(BatchSettlement.version.desc())
        .first()
    )
    return row[0] if row else 0


def get_settlement(db: Session, batch_id: int, version: int) -> Optional[BatchSettlement]:
    return (
        db.query(BatchSettlement)
        .filter(BatchSettlement.batch_id == batch_id, BatchSettlement.version == version)
        .first()
    )


class _SnapshotRow:
    """快照条目对外暴露的字段（与 HarvestSale 取数面一致）。"""

    __slots__ = (
        "id", "batch_id", "sale_date", "weight", "unit_price", "total_amount",
        "buyer", "batch_number", "quality_grade", "notes", "created_at",
        "price_scale", "amount_version", "amount_status", "root_sale_id",
        "replaced_by_id", "correction_id", "locked_version", "effective_from",
        "amount_consistent", "from_snapshot",
    )

    def __init__(self, item: SettlementSaleItem):
        self.id = item.sale_id
        self.batch_id = item.settlement.batch_id
        self.sale_date = item.sale_date
        self.weight = item.weight
        self.unit_price = item.unit_price
        self.total_amount = item.total_amount
        self.buyer = None
        self.batch_number = None
        self.quality_grade = None
        self.notes = None
        self.created_at = item.created_at
        self.price_scale = item.price_scale
        self.amount_version = item.amount_version
        self.amount_status = item.amount_status
        self.root_sale_id = item.root_sale_id
        self.replaced_by_id = None
        self.correction_id = None
        self.locked_version = item.settlement.version
        self.effective_from = 0
        self.amount_consistent = True
        self.from_snapshot = True


def visible_sales(
    db: Session, batch_id: int, cutoff_version: Optional[int] = None
) -> List:
    """返回某批次在指定截止版本下计入收入的销售条目。

    三个读接口共用本函数，收入口径唯一。
    """
    if cutoff_version is not None:
        settlement = get_settlement(db, batch_id, cutoff_version)
        if settlement is not None:
            return [_SnapshotRow(item) for item in settlement.items]

    bound = CURRENT if cutoff_version is None else cutoff_version
    rows = (
        db.query(HarvestSale)
        .filter(
            HarvestSale.batch_id == batch_id,
            HarvestSale.effective_from <= bound,
        )
        .order_by(HarvestSale.id.asc())
        .all()
    )
    for r in rows:
        r.from_snapshot = False
    return rows


def revenue_decimal(db: Session, batch_id: int, cutoff_version: Optional[int] = None) -> Decimal:
    """统一收入口径：对可见条目金额做 Decimal 精确求和。"""
    return decimal_sum(s.total_amount for s in visible_sales(db, batch_id, cutoff_version))
