"""销售写入、服务端计价、更正链路（自动校正/冲正替代/幂等/重查）测试。"""

import unittest

from tests.support import configure_temp_db, make_client


class SaleWriteCorrectionTest(unittest.TestCase):
    def setUp(self):
        configure_temp_db()
        self.client = make_client()
        pond = self.client.post("/api/ponds/", json={
            "name": "塘-销售", "area": 10, "water_depth": 2, "species": "鲈鱼"
        }).json()
        self.batch_id = self.client.post("/api/batches/", json={
            "batch_number": "B-SALE", "pond_id": pond["id"],
            "species": "鲈鱼", "stocking_date": "2026-01-01",
        }).json()["id"]

    def _create_sale(self, weight=3.5, price=12.33, **extra):
        body = {
            "batch_id": self.batch_id, "sale_date": "2026-03-01",
            "weight": weight, "unit_price": price,
        }
        body.update(extra)
        return self.client.post("/api/harvest-sales/", json=body)

    def test_client_total_amount_is_ignored_and_recomputed(self):
        r = self._create_sale(total_amount=9999.0)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total_amount"], 43.16)
        self.assertEqual(r.json()["price_scale"], 2)
        self.assertEqual(r.json()["amount_version"], 1)
        self.assertEqual(r.json()["amount_status"], "active")
        self.assertTrue(r.json()["amount_consistent"])

    def test_create_with_explicit_scale(self):
        r = self._create_sale(weight=1.235, price=1, price_scale=3)
        self.assertEqual(r.json()["total_amount"], 1.235)
        r2 = self._create_sale(weight=1.235, price=1, price_scale=2)
        self.assertEqual(r2.json()["total_amount"], 1.24)

    def test_invalid_weight_rejected(self):
        r = self._create_sale(weight=-3)
        self.assertEqual(r.status_code, 400)

    def test_put_cannot_change_price_fields(self):
        sid = self._create_sale().json()["id"]
        self.assertEqual(
            self.client.put(f"/api/harvest-sales/{sid}/", json={"weight": 9.0}).status_code, 409
        )
        self.assertEqual(
            self.client.put(f"/api/harvest-sales/{sid}/", json={"unit_price": 9.0}).status_code, 409
        )
        self.assertEqual(
            self.client.put(f"/api/harvest-sales/{sid}/", json={"total_amount": 1.0}).status_code, 409
        )
        # 非计价字段仍可更新
        r = self.client.put(f"/api/harvest-sales/{sid}/", json={"buyer": "张三"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["buyer"], "张三")
        self.assertEqual(r.json()["total_amount"], 43.16)

    def test_unsigned_correction_auto_fixes_in_place(self):
        sid = self._create_sale().json()["id"]
        # 直接构造一条金额错误的未签署记录
        from app.database import SessionLocal
        from app.models import HarvestSale
        db = SessionLocal()
        s = db.query(HarvestSale).get(sid)
        s.total_amount = 40.00
        s.amount_consistent = False
        db.commit(); db.close()

        r = self.client.post(
            f"/api/harvest-sales/corrections/{sid}/",
            json={"correction_id": "corr-1", "weight": 3.5, "unit_price": 13.0},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "auto")
        self.assertEqual(body["old_amount"], 40.0)
        self.assertEqual(body["new_amount"], 45.5)
        self.assertEqual(body["result_sale_id"], sid)  # 原条目就地校正

        detail = self.client.get(f"/api/harvest-sales/{sid}/").json()
        self.assertEqual(detail["total_amount"], 45.5)
        self.assertEqual(detail["correction_id"], "corr-1")
        self.assertEqual(detail["amount_status"], "active")

    def test_signed_correction_uses_reversal_and_replacement(self):
        sid = self._create_sale().json()["id"]
        self.client.post(
            f"/api/batches/{self.batch_id}/settle/", json={"idempotency_key": "s-1"}
        )

        r = self.client.post(
            f"/api/harvest-sales/corrections/{sid}/",
            json={"correction_id": "corr-2", "weight": 3.5, "unit_price": 13.0},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "reversal")
        self.assertEqual(body["new_amount"], 45.5)
        replacement_id = body["result_sale_id"]
        reversal_id = body["reversal_sale_id"]
        self.assertIsNotNone(reversal_id)
        self.assertNotEqual(replacement_id, sid)

        # 原条目未被原地覆盖，标记 reversed 并指向替代条目
        old = self.client.get(f"/api/harvest-sales/{sid}/").json()
        self.assertEqual(old["amount_status"], "reversed")
        self.assertEqual(old["total_amount"], 43.16)
        self.assertEqual(old["replaced_by_id"], replacement_id)

        # 冲正行为负值
        rev = self.client.get(f"/api/harvest-sales/{reversal_id}/").json()
        self.assertEqual(rev["amount_status"], "void")
        self.assertEqual(rev["total_amount"], -43.16)

        # 替代行为新值
        rep = self.client.get(f"/api/harvest-sales/{replacement_id}/").json()
        self.assertEqual(rep["amount_status"], "active")
        self.assertEqual(rep["total_amount"], 45.5)
        self.assertEqual(rep["amount_version"], 2)

    def test_duplicate_correction_applies_once_and_is_queryable(self):
        sid = self._create_sale().json()["id"]
        payload = {"correction_id": "once-1", "weight": 4.0, "unit_price": 10.0}
        first = self.client.post(f"/api/harvest-sales/corrections/{sid}/", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["replayed"])
        first_result = first.json()["result_sale_id"]

        # 响应丢失后重复提交：返回原结果，只生效一次
        second = self.client.post(f"/api/harvest-sales/corrections/{sid}/", json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.json()["result_sale_id"], first_result)

        # 通过更正标识查询原结果
        queried = self.client.get("/api/harvest-sales/corrections/once-1/")
        self.assertEqual(queried.status_code, 200)
        self.assertEqual(queried.json()["result_sale_id"], first_result)

        # 金额只被改了一次（40.0，而非重复施加）
        self.assertEqual(self.client.get(f"/api/harvest-sales/{sid}/").json()["total_amount"], 40.0)

    def test_same_correction_id_with_different_body_conflicts(self):
        sid = self._create_sale().json()["id"]
        self.client.post(f"/api/harvest-sales/corrections/{sid}/",
                         json={"correction_id": "dup", "weight": 4.0, "unit_price": 10.0})
        conflict = self.client.post(f"/api/harvest-sales/corrections/{sid}/",
                                    json={"correction_id": "dup", "weight": 5.0, "unit_price": 10.0})
        self.assertEqual(conflict.status_code, 409)

    def test_create_idempotency_key_replays_same_result(self):
        body = {
            "batch_id": self.batch_id, "sale_date": "2026-03-02",
            "weight": 2.0, "unit_price": 10.0, "request_id": "req-7",
        }
        first = self.client.post("/api/harvest-sales/", json=body)
        second = self.client.post("/api/harvest-sales/", json=body)
        self.assertEqual(first.json()["id"], second.json()["id"])
        # 凭 request_id 查询原结果
        queried = self.client.get("/api/harvest-sales/by-request/req-7/")
        self.assertEqual(queried.status_code, 200)
        self.assertEqual(queried.json()["id"], first.json()["id"])

    def test_create_idempotency_conflicting_body(self):
        body = {
            "batch_id": self.batch_id, "sale_date": "2026-03-02",
            "weight": 2.0, "unit_price": 10.0, "request_id": "req-8",
        }
        self.client.post("/api/harvest-sales/", json=body)
        conflict = self.client.post("/api/harvest-sales/", json={**body, "weight": 3.0})
        self.assertEqual(conflict.status_code, 409)


if __name__ == "__main__":
    unittest.main()
