"""旧库结构迁移与可追溯字段回填。

项目不引入完整迁移框架：启动时（及测试夹具）调用 :func:`ensure_schema`，
对既有 SQLite 库幂等地补齐新列、新表并回填版本链/体检标记。
"""

from sqlalchemy import inspect, text

from .database import Base, engine as default_engine
from .services import pricing

# harvest_sales 新增列：列名 -> (DDL 列定义, 是否需要回填)
_NEW_COLUMNS = {
    "price_scale": ("INTEGER NOT NULL DEFAULT 2", False),
    "amount_version": ("INTEGER NOT NULL DEFAULT 1", False),
    "amount_status": ("VARCHAR(20) NOT NULL DEFAULT 'active'", False),
    "root_sale_id": ("INTEGER", True),
    "replaced_by_id": ("INTEGER", False),
    "correction_id": ("VARCHAR(64)", False),
    "locked_version": ("INTEGER", False),
    "effective_from": ("INTEGER NOT NULL DEFAULT 0", False),
    "amount_consistent": ("BOOLEAN NOT NULL DEFAULT 1", False),
}


def _existing_columns(conn, table: str):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def ensure_schema(engine=None) -> None:
    """幂等迁移：建新表、补新列、回填版本链根与历史金额体检标记。"""
    engine = engine or default_engine

    # 新表（batch_settlements / sale_corrections / amount_fix_* / settlement_sale_items）
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if "harvest_sales" not in tables:
            return  # 全新库，create_all 已建好，无需迁移

        existing = _existing_columns(conn, "harvest_sales")
        for name, (ddl, _backfill) in _NEW_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE harvest_sales ADD COLUMN {name} {ddl}"))

        # 回填版本链：根条目指向自身
        conn.execute(
            text("UPDATE harvest_sales SET root_sale_id = id WHERE root_sale_id IS NULL")
        )

        # 旧状态字段缺失时补 active
        conn.execute(
            text("UPDATE harvest_sales SET amount_status = 'active' "
                 "WHERE amount_status IS NULL OR amount_status = ''")
        )

    # 历史金额体检：按统一规则标记不一致/缺失/超精度（分批修复的识别依据）
    _backfill_consistency(engine)

    # 幂等键索引（历史库可能缺失）
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_harvest_sales_correction_id "
            "ON harvest_sales (correction_id)"
        ))


def _backfill_consistency(engine) -> int:
    """按统一计价规则重算每条历史记录的一致性标记，返回异常条数。"""
    bad = 0
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, weight, unit_price, total_amount, price_scale, amount_status "
            "FROM harvest_sales"
        )).fetchall()
        for sale_id, weight, unit_price, stored, scale, status in rows:
            # 冲正(void)/被冲正(reversed)条目为系统按复式记账生成，金额自洽，
            # 不参与历史体检（其负重量会被常规校验误判）。
            if status != "active":
                conn.execute(
                    text("UPDATE harvest_sales SET amount_consistent = 1 WHERE id = :sid"),
                    {"sid": sale_id},
                )
                continue
            try:
                consistent, missing, over, expected = pricing.diagnose(
                    stored, weight, unit_price, scale or 2
                )
            except pricing.PriceValidationError:
                consistent, missing, over = False, stored is None, False
            flag = 1 if consistent else 0
            if not consistent:
                bad += 1
            conn.execute(
                text("UPDATE harvest_sales SET amount_consistent = :flag WHERE id = :sid"),
                {"flag": flag, "sid": sale_id},
            )
    return bad
