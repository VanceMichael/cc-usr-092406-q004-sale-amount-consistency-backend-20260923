"""历史金额分批修复（不一致/缺失/超精度；未签署自动校正、已签署冲正替代）。"""

import unittest
from datetime import date

from tests.support import configure_temp_db, make_client


def _make_batch(client, number="B-FIX"):
    pond = client.post("/api/ponds/", json={
        "name": f"塘-{number}", "area": 8, "water_depth": 2
    }).json()
    return client.post("/api/batches/", json={
        "batch_number": number, "pond_id": pond["id"],
        "species": "鲈鱼", "stocking_date": "2026-01-01",
    }).json()["id"]


def _seed_raw_sale(db, batch_id, weight, price, amount, scale=2, locked=None,
                   status="active"):
    """直接写入一条带历史问题的原始记录（绕过服务端计价）。"""
    from app.models import HarvestSale
    s = HarvestSale(
        batch_id=batch_id, sale_date=date(2026, 2, 1),
        weight=weight, unit_price=price, total_amount=amount,
        price_scale=scale, amount_version=1, amount_status=status,
        locked_version=locked, effective_from=0, amount_consistent=False,
    )
    db.add(s); db.commit(); db.refresh(s)
    s.root_sale_id = s.id
    db.commit()
    return s.id


class DataFixBatchTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        self.batch_id = _make_batch(self.client)

    def test_identifies_three_issue_types_in_batches(self):
        from app.database import SessionLocal
        db = SessionLocal()
        id_bad = _seed_raw_sale(db, self.batch_id, 3.5, 12.33, 99.0)    # 不一致
        id_missing = _seed_raw_sale(db, self.batch_id, 2.0, 10.0, None)  # 缺失
        id_over = _seed_raw_sale(db, self.batch_id, 12.345, 1, 12.345, scale=2)  # 超精度
        db.close()

        # 每批 2 条，第一页不应完成
        first = self.client.post("/api/amount-fixes/", json={
            "batch_ref": "fix-A", "batch_id": self.batch_id, "batch_size": 2,
        }).json()
        self.assertFalse(first["finished"])
        self.assertEqual(first["total_scanned"], 2)

        second = self.client.post("/api/amount-fixes/fix-A/chunks/",
                                  json={"batch_size": 2}).json()
        self.assertTrue(second["finished"])
        self.assertEqual(second["total_scanned"], 3)
        self.assertEqual(second["total_inconsistent"], 1)
        self.assertEqual(second["total_missing"], 1)
        self.assertEqual(second["total_over_precision"], 1)
        # 全部未签署 -> 自动校正
        self.assertEqual(second["total_auto_fixed"], 3)
        self.assertEqual(second["total_reversed"], 0)

        # 金额已按统一规则修正
        for sid, expected in [(id_bad, 43.16), (id_missing, 20.0), (id_over, 12.35)]:
            got = self.client.get(f"/api/harvest-sales/{sid}/").json()["total_amount"]
            self.assertAlmostEqual(got, expected, places=6)

    def test_rerun_fix_batch_is_idempotent_and_finishes(self):
        from app.database import SessionLocal
        db = SessionLocal()
        _seed_raw_sale(db, self.batch_id, 3.5, 12.33, 99.0)
        db.close()

        for _ in range(3):
            r = self.client.post("/api/amount-fixes/", json={
                "batch_ref": "fix-R", "batch_id": self.batch_id, "batch_size": 10,
            })
            self.assertEqual(r.status_code, 200)
        final = self.client.get("/api/amount-fixes/fix-R/").json()
        self.assertTrue(final["finished"])
        self.assertEqual(final["total_inconsistent"], 1)
        self.assertEqual(final["total_auto_fixed"], 1)

    def test_signed_records_fixed_by_reversal_not_overwrite(self):
        from app.database import SessionLocal
        db = SessionLocal()
        sid = _seed_raw_sale(db, self.batch_id, 3.5, 12.33, 99.0, locked=1)
        db.close()

        r = self.client.post("/api/amount-fixes/", json={
            "batch_ref": "fix-S", "batch_id": self.batch_id, "batch_size": 10,
        }).json()
        self.assertTrue(r["finished"])
        self.assertEqual(r["total_reversed"], 1)
        self.assertEqual(r["total_auto_fixed"], 0)

        # 原条目保留错误历史值但标记 reversed，未被原地覆盖
        old = self.client.get(f"/api/harvest-sales/{sid}/").json()
        self.assertEqual(old["amount_status"], "reversed")
        self.assertEqual(old["total_amount"], 99.0)
        rep_id = old["replaced_by_id"]
        rep = self.client.get(f"/api/harvest-sales/{rep_id}/").json()
        self.assertEqual(rep["amount_status"], "active")
        self.assertEqual(rep["total_amount"], 43.16)
        # 当前收入 = 原错误值被冲正行抵消 + 替代值
        rev = self.client.get("/api/analysis/cycle/1/").json() if False else \
            self.client.get(f"/api/analysis/cycle/{self.batch_id}/").json()["total_revenue"]
        self.assertAlmostEqual(rev, 43.16, places=6)

    def test_dry_run_only_identifies_without_applying(self):
        from app.database import SessionLocal
        db = SessionLocal()
        sid = _seed_raw_sale(db, self.batch_id, 3.5, 12.33, 99.0)
        db.close()

        r = self.client.post("/api/amount-fixes/", json={
            "batch_ref": "fix-D", "batch_id": self.batch_id,
            "batch_size": 10, "auto_apply": False,
        }).json()
        self.assertEqual(r["total_blocked"], 1)
        self.assertEqual(r["total_auto_fixed"], 0)
        # 金额未被修改
        self.assertEqual(
            self.client.get(f"/api/harvest-sales/{sid}/").json()["total_amount"], 99.0
        )


if __name__ == "__main__":
    unittest.main()
