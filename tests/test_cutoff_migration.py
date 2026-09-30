"""周期分析/追溯/销售详情在同一截止版本下的收入一致性，及旧库迁移。"""

import os
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text

import tests.support  # noqa: F401  # 先装配 backend 导入路径
import app.database as database
from app.models import Base
from app.migrations import ensure_schema
from tests.support import configure_temp_db, make_client, attach_sqlite_pragmas


def _make_batch(client, number="B-VIEW"):
    pond = client.post("/api/ponds/", json={
        "name": f"塘-{number}", "area": 8, "water_depth": 2
    }).json()
    return client.post("/api/batches/", json={
        "batch_number": number, "pond_id": pond["id"],
        "species": "鲈鱼", "stocking_date": "2026-01-01",
    }).json()["id"]


class CutoffRevenueConsistencyTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        self.batch_id = _make_batch(self.client)

    def _add_sale(self, w, p):
        return self.client.post("/api/harvest-sales/", json={
            "batch_id": self.batch_id, "sale_date": "2026-03-01",
            "weight": w, "unit_price": p,
        }).json()["id"]

    def _revenue_three_views(self, cutoff):
        params = {} if cutoff is None else {"cutoff_version": cutoff}
        cycle = self.client.get(
            f"/api/analysis/cycle/{self.batch_id}/", params=params
        ).json()["total_revenue"]
        trace = self.client.get(
            f"/api/analysis/traceability/{self.batch_id}/", params=params
        ).json()
        listing = self.client.get(
            "/api/harvest-sales/", params={"batch_id": self.batch_id, **params}
        ).json()
        list_rev = round(sum(s["total_amount"] for s in listing), 6)
        return cycle, trace["total_revenue"], list_rev

    def test_same_revenue_across_views_at_each_cutoff(self):
        s1 = self._add_sale(3.5, 12.33)  # 43.16
        s2 = self._add_sale(2.0, 10.0)   # 20.00

        # 结算版本 1：收入 63.16
        self.client.post(f"/api/batches/{self.batch_id}/settle/",
                         json={"idempotency_key": "v1"})
        c, t, l = self._revenue_three_views(1)
        self.assertAlmostEqual(c, 63.16, places=6)
        self.assertEqual((round(c, 6), round(t, 6), round(l, 6)),
                         (63.16, 63.16, 63.16))

        # 结算后更正 s1 为 13.0 单价 -> 冲正+替代
        self.client.post(f"/api/harvest-sales/corrections/{s1}/",
                         json={"correction_id": "fix-s1", "weight": 3.5, "unit_price": 13.0})

        # 当前（无截止）：-43.16 + 45.5 + 20 = 22.34? 不，=45.5+20=65.5（冲正抵消原条目）
        c, t, l = self._revenue_three_views(None)
        self.assertEqual((round(c, 6), round(t, 6), round(l, 6)),
                         (65.5, 65.5, 65.5))

        # 历史截止版本 1 仍是签署时的 63.16（快照复现）
        c, t, l = self._revenue_three_views(1)
        self.assertEqual((round(c, 6), round(t, 6), round(l, 6)),
                         (63.16, 63.16, 63.16))

        # 第二版本结算后，cutoff=2 与当前均为 65.5
        self.client.post(f"/api/batches/{self.batch_id}/settle/",
                         json={"idempotency_key": "v2"})
        c, t, l = self._revenue_three_views(2)
        self.assertEqual((round(c, 6), round(t, 6), round(l, 6)),
                         (65.5, 65.5, 65.5))
        c, t, l = self._revenue_three_views(None)
        self.assertEqual((round(c, 6), round(t, 6), round(l, 6)),
                         (65.5, 65.5, 65.5))

    def test_detail_cutoff_visibility(self):
        s1 = self._add_sale(1.0, 10.0)
        self.client.post(f"/api/batches/{self.batch_id}/settle/",
                         json={"idempotency_key": "v1"})
        r = self.client.post(f"/api/harvest-sales/corrections/{s1}/",
                             json={"correction_id": "cx", "weight": 1.0, "unit_price": 12.0})
        rep = r.json()["result_sale_id"]
        rev_row = r.json()["reversal_sale_id"]

        # 替代/冲正条目在 cutoff=1 时尚未生效
        self.assertEqual(self.client.get(f"/api/harvest-sales/{rep}/",
                                         params={"cutoff_version": 1}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/harvest-sales/{rev_row}/",
                                         params={"cutoff_version": 1}).status_code, 404)
        # 当前可查；原条目作为审计事实在 cutoff=1 仍可查
        self.assertEqual(self.client.get(f"/api/harvest-sales/{rep}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/harvest-sales/{s1}/",
                                         params={"cutoff_version": 1}).status_code, 200)

    def test_profit_uses_consistent_revenue(self):
        self._add_sale(3.5, 12.33)  # 43.16
        self.client.post("/api/cost-records/", json={
            "batch_id": self.batch_id, "cost_date": "2026-02-01",
            "cost_type": "feed", "amount": 13.16,
        })
        body = self.client.get(f"/api/analysis/cycle/{self.batch_id}/").json()
        self.assertAlmostEqual(body["total_revenue"], 43.16, places=6)
        self.assertAlmostEqual(body["profit"], 30.0, places=6)


class LegacyDatabaseMigrationTest(unittest.TestCase):
    def test_legacy_schema_gets_trace_columns_and_consistency_backfill(self):
        path = tempfile.mktemp(prefix="aq_legacy_", suffix=".db")
        eng = create_engine(f"sqlite:///{path}")

        # 构造"旧库"：只有最早的 harvest_sales 结构
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE ponds (id INTEGER PRIMARY KEY, name VARCHAR(100), "
                "area FLOAT, water_depth FLOAT, species VARCHAR(100), status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE batches (id INTEGER PRIMARY KEY, batch_number VARCHAR(50), "
                "pond_id INTEGER, species VARCHAR(100), stocking_date DATE, "
                "estimated_harvest_date DATE, actual_harvest_date DATE, status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE harvest_sales (id INTEGER PRIMARY KEY, batch_id INTEGER, "
                "sale_date DATE, weight FLOAT, unit_price FLOAT, total_amount FLOAT, "
                "buyer VARCHAR(200), batch_number VARCHAR(50), quality_grade VARCHAR(50), "
                "notes TEXT, created_at DATETIME)"
            ))
            conn.execute(text(
                "INSERT INTO batches (id, batch_number, pond_id, species, stocking_date, status) "
                "VALUES (1, 'OLD-1', NULL, '鲈', '2026-01-01', 'active')"
            ))
            # 一条金额正确、一条金额错误
            conn.execute(text(
                "INSERT INTO harvest_sales (id, batch_id, sale_date, weight, unit_price, total_amount) "
                "VALUES (1, 1, '2026-03-01', 3.5, 12.33, 43.16)"
            ))
            conn.execute(text(
                "INSERT INTO harvest_sales (id, batch_id, sale_date, weight, unit_price, total_amount) "
                "VALUES (2, 1, '2026-03-02', 2.0, 10.0, 999.0)"
            ))

        # 执行迁移
        attach_sqlite_pragmas(eng)
        ensure_schema(eng)

        with eng.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(harvest_sales)")).fetchall()}
        for required in ("price_scale", "amount_version", "amount_status", "root_sale_id",
                         "replaced_by_id", "correction_id", "locked_version",
                         "effective_from", "amount_consistent"):
            self.assertIn(required, cols)

        with eng.begin() as conn:
            # 版本链根回填为自身
            roots = conn.execute(text(
                "SELECT id, root_sale_id, amount_status, amount_consistent, price_scale "
                "FROM harvest_sales ORDER BY id"
            )).fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in roots],
                         [(1, 1, "active"), (2, 2, "active")])
        self.assertTrue(roots[0][3])   # 正确记录
        self.assertFalse(roots[1][3])  # 错误记录被标记
        self.assertEqual(roots[0][4], 2)

        # 新表已建立
        tables = set(inspect(eng).get_table_names())
        for t in ("batch_settlements", "sale_corrections", "amount_fix_batches",
                  "amount_fix_items", "settlement_sale_items", "idempotent_results"):
            self.assertIn(t, tables)

        eng.dispose()
        os.remove(path)
        for suffix in ("-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    unittest.main()
