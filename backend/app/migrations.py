"""SQLite 轻量、幂等的结构迁移。

历史库只有 harvest_sales 基础列；本模块在启动时：
1. 补全新增事实/状态列（ADD COLUMN，已存在则跳过）；
2. create_all 建立 sale_corrections / batch_settlements 新表；
3. 回填历史行：status=active、is_settled=0、price_scale=2，
   并把计价版本标记为 legacy-v0，等待分批识别/自动修复。
"""

from sqlalchemy import inspect, text

from .pricing import DEFAULT_PRICE_SCALE, LEGACY_VERSION

_SALE_NEW_COLUMNS = [
    ("weight_raw", "VARCHAR(64)"),
    ("unit_price_raw", "VARCHAR(64)"),
    ("price_scale", "INTEGER"),
    ("pricing_version", "VARCHAR(20)"),
    ("status", "VARCHAR(20) NOT NULL DEFAULT 'active'"),
    ("is_settled", "INTEGER NOT NULL DEFAULT 0"),
    ("supersedes_id", "INTEGER REFERENCES harvest_sales(id)"),
    ("corrected_by_id", "INTEGER"),
    ("created_by_correction_id", "INTEGER"),
]


def ensure_schema(Base, engine):
    # 先建新库/新表（含全部新列），再检测旧表缺列，避免两种库形态分歧。
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    sale_columns = {col["name"] for col in inspector.get_columns("harvest_sales")} \
        if "harvest_sales" in table_names else set()

    missing = [(name, ddl) for name, ddl in _SALE_NEW_COLUMNS if name not in sale_columns]

    with engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE harvest_sales ADD COLUMN {name} {ddl}"))
        if missing:
            # 旧数据回填：标记为待修复的遗留版本，事实原文取现存数值的精确字符串。
            conn.execute(text(
                "UPDATE harvest_sales SET "
                "status = 'active' WHERE status IS NULL"
            ))
            conn.execute(text(
                "UPDATE harvest_sales SET is_settled = 0 WHERE is_settled IS NULL"
            ))
            conn.execute(text(
                f"UPDATE harvest_sales SET price_scale = {DEFAULT_PRICE_SCALE} "
                "WHERE price_scale IS NULL"
            ))
            conn.execute(text(
                "UPDATE harvest_sales SET pricing_version = :ver "
                "WHERE pricing_version IS NULL OR pricing_version = ''"
            ), {"ver": LEGACY_VERSION})
            conn.execute(text(
                "UPDATE harvest_sales SET weight_raw = CAST(weight AS TEXT) "
                "WHERE weight_raw IS NULL"
            ))
            conn.execute(text(
                "UPDATE harvest_sales SET unit_price_raw = CAST(unit_price AS TEXT) "
                "WHERE unit_price_raw IS NULL"
            ))
