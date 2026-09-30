"""批次结算、并发边界与失败重试测试。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.support import configure_temp_db, make_client


def _make_batch(client, number="B-SETTLE"):
    pond = client.post("/api/ponds/", json={
        "name": f"塘-{number}", "area": 8, "water_depth": 2
    }).json()
    return client.post("/api/batches/", json={
        "batch_number": number, "pond_id": pond["id"],
        "species": "鲈鱼", "stocking_date": "2026-01-01",
    }).json()["id"]


class SettlementTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        self.batch_id = _make_batch(self.client)

    def _add_sale(self, weight, price, **kw):
        return self.client.post("/api/harvest-sales/", json={
            "batch_id": self.batch_id, "sale_date": "2026-03-01",
            "weight": weight, "unit_price": price, **kw,
        }).json()["id"]

    def test_settlement_snapshots_revenue_and_locks(self):
        self._add_sale(3.5, 12.33)   # 43.16
        self._add_sale(2.0, 10.0)    # 20.0
        r = self.client.post(f"/api/batches/{self.batch_id}/settle/",
                             json={"idempotency_key": "set-1"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["cutoff_version"], 1)
        self.assertAlmostEqual(body["revenue"], 63.16, places=6)
        self.assertEqual(body["sale_count"], 2)
        # 签署后条目被锁定
        sales = self.client.get("/api/harvest-sales/",
                                params={"batch_id": self.batch_id}).json()
        self.assertTrue(all(s["locked_version"] == 1 for s in sales))

    def test_settlement_blocked_when_unfixed_amount_exists(self):
        sid = self._add_sale(3.5, 12.33)
        from app.database import SessionLocal
        from app.models import HarvestSale
        db = SessionLocal()
        s = db.query(HarvestSale).get(sid)
        s.total_amount = 1.0  # 人为制造不一致
        s.amount_consistent = False
        db.commit(); db.close()

        r = self.client.post(f"/api/batches/{self.batch_id}/settle/", json={})
        self.assertEqual(r.status_code, 409)

    def test_settlement_idempotency_key_replays(self):
        self._add_sale(1.0, 10.0)
        first = self.client.post(f"/api/batches/{self.batch_id}/settle/",
                                 json={"idempotency_key": "same-key"})
        second = self.client.post(f"/api/batches/{self.batch_id}/settle/",
                                  json={"idempotency_key": "same-key"})
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertTrue(second.json()["replayed"])
        rows = self.client.get(f"/api/batches/{self.batch_id}/settlements/").json()
        self.assertEqual(len(rows), 1)

    def test_successive_settlement_versions_are_consecutive(self):
        sid = self._add_sale(1.0, 10.0)  # 10
        self.client.post(f"/api/batches/{self.batch_id}/settle/",
                         json={"idempotency_key": "v1"})
        self.client.post(f"/api/harvest-sales/corrections/{sid}/",
                         json={"correction_id": "c-v2", "weight": 1.0, "unit_price": 12.0})
        v2 = self.client.post(f"/api/batches/{self.batch_id}/settle/",
                              json={"idempotency_key": "v2"})
        self.assertEqual(v2.json()["version"], 2)
        # 第二次结算快照的是替代条目（12.0 + 冲正 -10 抵消原条目）
        self.assertAlmostEqual(v2.json()["revenue"], 12.0, places=6)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        self.batch_id = _make_batch(self.client, "B-CONC")
        for w, p in [(1.0, 10.0), (2.0, 5.0)]:
            self.client.post("/api/harvest-sales/", json={
                "batch_id": self.batch_id, "sale_date": "2026-03-01",
                "weight": w, "unit_price": p,
            })

    def test_concurrent_settlements_serialize_to_consecutive_versions(self):
        from app.services import sales_service

        errors = []
        results = []
        lock = threading.Lock()

        def worker(i):
            try:
                out = sales_service.settle_batch(self.batch_id, f"conc-key-{i}")
                with lock:
                    results.append(out)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))

        self.assertEqual(errors, [])
        versions = sorted(r["version"] for r in results)
        self.assertEqual(versions, list(range(1, 9)))  # 无重复版本、无空洞
        ids = {r["id"] for r in results}
        self.assertEqual(len(ids), 8)

    def test_concurrent_same_correction_applies_once(self):
        from app.services import sales_service

        sid = self.client.get("/api/harvest-sales/",
                              params={"batch_id": self.batch_id}).json()[0]["id"]
        # 先签署使后续走冲正链路
        self.client.post(f"/api/batches/{self.batch_id}/settle/",
                         json={"idempotency_key": "lock-1"})

        outcomes = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                out = sales_service.apply_correction(
                    sid, "race-corr", 1.0, 11.0
                )
                with lock:
                    outcomes.append(out)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: worker(), range(8)))

        self.assertEqual(errors, [])
        # 所有并发请求都得到同一个结果条目
        result_ids = {o["result_sale_id"] for o in outcomes}
        self.assertEqual(len(result_ids), 1)
        # 只产生一笔冲正 + 一笔替代
        all_sales = self.client.get("/api/harvest-sales/",
                                    params={"batch_id": self.batch_id}).json()
        self.assertEqual(sum(1 for s in all_sales if s["amount_status"] == "void"), 1)
        self.assertEqual(sum(1 for s in all_sales if s["amount_status"] == "active"), 2)


class FailureRetryTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        self.batch_id = _make_batch(self.client, "B-RETRY")
        self.sid = self.client.post("/api/harvest-sales/", json={
            "batch_id": self.batch_id, "sale_date": "2026-03-01",
            "weight": 3.5, "unit_price": 12.33,
        }).json()["id"]

    def test_failed_then_retried_correction_succeeds_once(self):
        # 首次请求非法（负重量）-> 400，且不应占用幂等键
        bad = self.client.post(
            f"/api/harvest-sales/corrections/{self.sid}/",
            json={"correction_id": "retry-1", "weight": -1, "unit_price": 13.0},
        )
        self.assertEqual(bad.status_code, 400)

        # 同一更正标识用合法体重试 -> 成功，且只生效一次
        ok = self.client.post(
            f"/api/harvest-sales/corrections/{self.sid}/",
            json={"correction_id": "retry-1", "weight": 3.5, "unit_price": 13.0},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(ok.json()["replayed"])
        again = self.client.post(
            f"/api/harvest-sales/corrections/{self.sid}/",
            json={"correction_id": "retry-1", "weight": 3.5, "unit_price": 13.0},
        )
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(
            self.client.get("/api/harvest-sales/corrections/retry-1/").json()["new_amount"],
            45.5,
        )

    def test_settlement_retry_after_blocking_issue_fixed(self):
        from app.database import SessionLocal
        from app.models import HarvestSale
        db = SessionLocal()
        s = db.get(HarvestSale, self.sid)
        s.total_amount = 1.0
        s.amount_consistent = False
        db.commit(); db.close()

        # 金额不一致时结算被拒
        blocked = self.client.post(f"/api/batches/{self.batch_id}/settle/", json={})
        self.assertEqual(blocked.status_code, 409)

        # 修复后用相同意图重试结算 -> 成功，且之前没有产生任何结算行
        self.client.post("/api/amount-fixes/", json={
            "batch_ref": "fix-retry", "batch_id": self.batch_id, "batch_size": 10,
        })
        ok = self.client.post(f"/api/batches/{self.batch_id}/settle/",
                              json={"idempotency_key": "settle-retry"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["version"], 1)
        self.assertAlmostEqual(ok.json()["revenue"], 43.16, places=6)


class MixedConcurrencyTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        self.batch_id = _make_batch(self.client, "B-MIX")
        self.sid = self.client.post("/api/harvest-sales/", json={
            "batch_id": self.batch_id, "sale_date": "2026-03-01",
            "weight": 1.0, "unit_price": 10.0,
        }).json()["id"]

    def test_concurrent_settlements_and_corrections_keep_consistent_ledger(self):
        from app.services import sales_service

        # 首轮先签署，之后每轮让"再结算"与"一次更正"并发竞速，
        # 无论二者谁先提交，账目恒等式与版本连续性都必须成立。
        sales_service.settle_batch(self.batch_id, "mix-s-0")
        prices = [12.0, 13.0, 14.0]

        for round_idx, price in enumerate(prices, start=1):
            errs = []

            def settle():
                try:
                    sales_service.settle_batch(self.batch_id, f"mix-s-{round_idx}")
                except Exception as exc:  # noqa: BLE001
                    errs.append(exc)

            def correct():
                try:
                    sales_service.apply_correction(
                        self.sid, f"mix-c-{round_idx}", 1.0, price
                    )
                except Exception as exc:  # noqa: BLE001
                    errs.append(exc)

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda f: f(), [settle, correct]))
            self.assertEqual(errs, [])

        # 每个更正标识只生效一次且可查
        for round_idx, price in enumerate(prices, start=1):
            q = self.client.get(f"/api/harvest-sales/corrections/mix-c-{round_idx}/")
            self.assertEqual(q.status_code, 200)
            self.assertEqual(q.json()["new_amount"], price)

        # 结算版本连续无重复
        versions = sorted(
            s["version"] for s in
            self.client.get(f"/api/batches/{self.batch_id}/settlements/").json()
        )
        self.assertEqual(versions, [1, 2, 3, 4])

        # 版本链最终只有一个 active 链头，金额为最后一次更正值
        listing = self.client.get("/api/harvest-sales/",
                                  params={"batch_id": self.batch_id}).json()
        active_heads = [s for s in listing if s["amount_status"] == "active"]
        self.assertEqual(len(active_heads), 1)
        self.assertEqual(active_heads[0]["total_amount"], 14.0)

        # 复式账面试算平衡：周期分析收入 == 账目行求和
        cycle = self.client.get(
            f"/api/analysis/cycle/{self.batch_id}/"
        ).json()["total_revenue"]
        ledger_sum = round(sum(s["total_amount"] for s in listing), 6)
        self.assertAlmostEqual(cycle, ledger_sum, places=6)
        self.assertAlmostEqual(cycle, 14.0, places=6)

        # 每个历史截止结算版本都能用快照复现当时收入
        for v in versions:
            snap = next(s for s in self.client.get(
                f"/api/batches/{self.batch_id}/settlements/").json()
                if s["version"] == v)
            cyc = self.client.get(
                f"/api/analysis/cycle/{self.batch_id}/",
                params={"cutoff_version": v}).json()["total_revenue"]
            self.assertAlmostEqual(cyc, snap["revenue"], places=6)


if __name__ == "__main__":
    unittest.main()
