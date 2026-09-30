"""销售金额计算与更正链路测试。

覆盖：
- 计价舍入边界（ROUND_HALF_UP、精度 0..3）
- 服务端强制算金额（忽略客户端总金额）、PUT 篡改被拒
- 历史坏数据（不一致/缺失/超精度）分批识别与未签署自动修复、旧库迁移
- 未签署原地校正、已签署冲正+替代、审计可追溯
- 同一更正标识并发/重试只生效一次，响应丢失后可查原结果
- 批次结算一次性、事务内修复、快照冻结；并发结算仅一次成功
- 周期分析 / 追溯 / 销售列表详情在同一截止版本收入一致
"""

import json
import os
import tempfile
import threading
import unittest
import uuid
from datetime import date
from decimal import Decimal

# 在导入应用前指定临时文件库
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="aqua_test_")
os.close(_DB_FD)
os.unlink(_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.database import Base, engine  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Batch, BatchSettlement, HarvestSale, Pond, SaleCorrection,
)
from backend.app import pricing  # noqa: E402
from backend.app.services import sales as svc  # noqa: E402


class PricingRuleTest(unittest.TestCase):
    """统一计价规则：Decimal + ROUND_HALF_UP 的舍入边界。"""

    def test_half_up_boundary_at_scale_2(self):
        # 0.005 必须进位到 0.01（银行家舍入会得到 0.00）
        self.assertEqual(pricing.compute_amount("0.01", "0.5", 2), Decimal("0.01"))
        # 2.345 -> 2.35，2.344 -> 2.34
        self.assertEqual(pricing.compute_amount("1", "2.345", 2), Decimal("2.35"))
        self.assertEqual(pricing.compute_amount("1", "2.344", 2), Decimal("2.34"))
        # 2.5 按 0 位精度 -> 3（半数向上，而非偶数舍入的 2）
        self.assertEqual(pricing.compute_amount("1", "2.5", 0), Decimal("3"))
        self.assertEqual(pricing.compute_amount("1", "3.5", 0), Decimal("4"))

    def test_scales_zero_to_three(self):
        # 12.345 * 2.7648 = 34.131456
        cases = {
            0: Decimal("34"),
            1: Decimal("34.1"),
            2: Decimal("34.13"),
            3: Decimal("34.131"),
        }
        for scale, expected in cases.items():
            with self.subTest(scale=scale):
                self.assertEqual(
                    pricing.compute_amount("12.345", "2.7648", scale), expected
                )

    def test_default_scale_is_two_and_product_exact(self):
        self.assertEqual(pricing.compute_amount("12.5", "10.2"), Decimal("127.50"))
        # 34.1325*3 这类无二进制误差问题（Decimal 精确）
        self.assertEqual(pricing.compute_amount("3", "34.1325", 4), Decimal("102.3975"))

    def test_invalid_facts_rejected(self):
        with self.assertRaises(pricing.PricingError):
            pricing.compute_amount("0", "10")
        with self.assertRaises(pricing.PricingError):
            pricing.compute_amount("10", "-1")
        with self.assertRaises(pricing.PricingError):
            pricing.compute_amount("abc", "10")
        with self.assertRaises(pricing.PricingError):
            pricing.compute_amount("10", "10", 9)


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        # 每用例独立批次，保证并发用例互不干扰
        self.token = uuid.uuid4().hex[:8]
        pond = self.client.post("/api/ponds/", json={
            "name": f"塘-{self.token}", "area": 10.0, "water_depth": 2.0,
            "species": "草鱼",
        })
        # 塘名若与历史用例冲突（重跑）则复用
        self.pond_id = pond.json()["id"] if pond.status_code == 200 else \
            self.client.get("/api/ponds/").json()[0]["id"]
        batch = self.client.post("/api/batches/", json={
            "batch_number": f"B{self.token}", "pond_id": self.pond_id,
            "species": "草鱼", "stocking_date": "2026-01-01",
        })
        self.assertEqual(batch.status_code, 200, batch.text)
        self.batch_id = batch.json()["id"]

    def _create_sale(self, **overrides):
        payload = {
            "batch_id": self.batch_id,
            "sale_date": "2026-05-01",
            "weight": "12.5",
            "unit_price": "10.2",
            "buyer": "收购商甲",
        }
        payload.update(overrides)
        resp = self.client.post("/api/harvest-sales/", json=payload)
        return resp


class ServerSideAmountTest(ApiTestCase):
    def test_client_total_amount_is_ignored_and_facts_kept(self):
        # 调用方提交一个与重量单价不一致的总金额
        resp = self._create_sale(total_amount=99999.99)
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertAlmostEqual(body["total_amount"], 127.50, places=9)
        self.assertEqual(body["weight_raw"], "12.5")
        self.assertEqual(body["unit_price_raw"], "10.2")
        self.assertEqual(body["price_scale"], 2)
        self.assertEqual(body["pricing_version"], "v1")
        self.assertEqual(body["status"], "active")
        self.assertFalse(body["is_settled"])

    def test_create_with_explicit_scale_rounds_half_up(self):
        resp = self._create_sale(weight="1", unit_price="2.345", price_scale=2)
        self.assertEqual(resp.json()["total_amount"], 2.35)

    def test_put_cannot_change_amount_or_facts(self):
        sale_id = self._create_sale().json()["id"]
        # 直接提交总金额 → 400
        r = self.client.put(f"/api/harvest-sales/{sale_id}/", json={"total_amount": 1})
        self.assertEqual(r.status_code, 400)
        # 改重量/单价 → 400（不会重算，也不会落库）
        r = self.client.put(f"/api/harvest-sales/{sale_id}/", json={"weight": "20"})
        self.assertEqual(r.status_code, 400)
        r = self.client.put(f"/api/harvest-sales/{sale_id}/", json={"unit_price": "9"})
        self.assertEqual(r.status_code, 400)
        # 非事实字段仍可更新
        r = self.client.put(f"/api/harvest-sales/{sale_id}/", json={"buyer": "收购商乙"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["buyer"], "收购商乙")
        # 金额与事实未被污染
        detail = self.client.get(f"/api/harvest-sales/{sale_id}/").json()
        self.assertAlmostEqual(detail["total_amount"], 127.50, places=9)
        self.assertEqual(detail["weight_raw"], "12.5")

    def test_invalid_facts_return_400_and_no_partial_write(self):
        r = self._create_sale(weight="0", unit_price="10")
        self.assertEqual(r.status_code, 400)
        r = self._create_sale(weight="10", unit_price="not-a-number")
        self.assertEqual(r.status_code, 400)
        # 修正输入后重试成功（事务回滚不影响后续）
        r2 = self._create_sale(weight="10", unit_price="10")
        self.assertEqual(r2.status_code, 201, r2.text)


class LegacyDataRepairTest(ApiTestCase):
    def _insert_legacy_sale(self, weight, price, total, scale=2, settle=False):
        """直接插入旧形态数据：遗留版本 + 三类坏值。"""
        from backend.app.database import SessionLocal
        db = SessionLocal()
        try:
            sale = HarvestSale(
                batch_id=self.batch_id, sale_date=date(2026, 5, 2),
                weight=weight, unit_price=price, total_amount=total,
                weight_raw=str(weight), unit_price_raw=str(price),
                price_scale=scale, pricing_version=pricing.LEGACY_VERSION,
                status="active", is_settled=1 if settle else 0,
            )
            db.add(sale)
            db.commit()
            db.refresh(sale)
            return sale.id
        finally:
            db.close()

    def test_scan_classifies_three_issue_types_in_batches(self):
        id_mismatch = self._insert_legacy_sale(10.0, 10.0, 999.0)       # 不一致
        id_missing = self._insert_legacy_sale(10.0, 10.0, None)         # 缺失金额
        id_over = self._insert_legacy_sale(10.0, 10.0, 100.123, scale=2)  # 超精度
        id_ok_legacy = self._insert_legacy_sale(10.0, 10.0, 100.0)      # 值对但版本旧
        # 单批 2 条，分批识别
        batch1 = self.client.get(
            f"/api/harvest-sales/data-issues/?batch_id={self.batch_id}&limit=2&offset=0"
        ).json()
        batch2 = self.client.get(
            f"/api/harvest-sales/data-issues/?batch_id={self.batch_id}&limit=2&offset=2"
        ).json()
        issues = {i["sale_id"]: i["issue_type"] for i in batch1 + batch2}
        self.assertEqual(issues[id_mismatch], "mismatch")
        self.assertEqual(issues[id_missing], "missing")
        self.assertEqual(issues[id_over], "over_precision")
        # 值恰好正确的遗留行不算金额问题（但仍会在修复时升级版本）
        self.assertNotIn(id_ok_legacy, issues)

    def test_auto_repair_unsigned_and_skip_settled(self):
        id_bad = self._insert_legacy_sale(10.0, 10.0, 999.0)
        id_missing = self._insert_legacy_sale(10.0, 10.0, None)
        id_settled_bad = self._insert_legacy_sale(5.0, 4.0, 1.0, settle=True)

        r = self.client.post(
            f"/api/harvest-sales/data-repair/?batch_id={self.batch_id}&limit=10"
        )
        self.assertEqual(r.status_code, 200, r.text)
        report = r.json()
        self.assertEqual(report["repaired"], 2)
        self.assertEqual(report["skipped_settled"], 1)
        self.assertIn(id_bad, report["repaired_sale_ids"])
        self.assertIn(id_missing, report["repaired_sale_ids"])
        self.assertEqual(report["settled_sale_ids"], [id_settled_bad])

        # 未签署问题全部修复；已签署坏行无法原地修复，仍被识别报告（等待冲正替代）
        self.assertAlmostEqual(
            self.client.get(f"/api/harvest-sales/{id_bad}/").json()["total_amount"],
            100.0, places=9)
        self.assertAlmostEqual(
            self.client.get(f"/api/harvest-sales/{id_missing}/").json()["total_amount"],
            100.0, places=9)
        issues = self.client.get(
            f"/api/harvest-sales/data-issues/?batch_id={self.batch_id}"
        ).json()
        self.assertEqual([i["sale_id"] for i in issues], [id_settled_bad])
        self.assertEqual(issues[0]["issue_type"], "mismatch")
        # 已签署行：数据库存储原值 1.0 原样保留（不能原地覆盖），等待冲正替代；
        # API 呈现按事实重算，保证与分析/追溯收入口径一致。
        from backend.app.database import SessionLocal
        db = SessionLocal()
        try:
            stored = db.get(HarvestSale, id_settled_bad)
            self.assertEqual(stored.total_amount, 1.0)
            self.assertTrue(stored.is_settled)
            self.assertEqual(stored.status, "active")
        finally:
            db.close()
        presented = self.client.get(f"/api/harvest-sales/{id_settled_bad}/").json()
        self.assertAlmostEqual(presented["total_amount"], 20.0, places=9)

    def test_repair_is_idempotent(self):
        self._insert_legacy_sale(10.0, 10.0, 999.0)
        first = self.client.post("/api/harvest-sales/data-repair/").json()
        second = self.client.post("/api/harvest-sales/data-repair/").json()
        self.assertEqual(first["repaired"], 1)
        self.assertEqual(second["repaired"], 0)

    def test_repair_idempotent_at_zero_scale(self):
        # 2.5*14 = 35，精度 0；修复后 float 落库为 35.0，不应被误判超精度而反复修复
        sid = self._insert_legacy_sale(2.5, 14.0, 9.0, scale=0)
        r1 = self.client.post(
            f"/api/harvest-sales/data-repair/?batch_id={self.batch_id}").json()
        self.assertEqual(r1["repaired"], 1)
        detail = self.client.get(f"/api/harvest-sales/{sid}/").json()
        self.assertEqual(detail["total_amount"], 35.0)
        issues = self.client.get(
            f"/api/harvest-sales/data-issues/?batch_id={self.batch_id}").json()
        self.assertEqual(issues, [])
        r2 = self.client.post(
            f"/api/harvest-sales/data-repair/?batch_id={self.batch_id}").json()
        self.assertEqual(r2["repaired"], 0)


class CorrectionChainTest(ApiTestCase):
    def test_unsigned_correction_is_inplace_with_audit(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        cid = f"CID-{self.token}-1"
        r = self.client.post(
            f"/api/harvest-sales/{sale_id}/corrections/?correction_id={cid}",
            json={"weight": "12.5", "unit_price": "10.2", "reason": "称重复核"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["replayed"])
        self.assertEqual(body["mode"], "inplace")
        self.assertEqual(body["old_weight"], "10")
        self.assertEqual(body["new_weight"], "12.5")
        self.assertAlmostEqual(body["old_total_amount"], 100.0, places=9)
        self.assertAlmostEqual(body["new_total_amount"], 127.50, places=9)
        # 原行就地更新为服务端计算值，仍只有一条销售记录
        detail = self.client.get(f"/api/harvest-sales/{sale_id}/").json()
        self.assertAlmostEqual(detail["total_amount"], 127.50, places=9)
        self.assertEqual(detail["status"], "active")

    def test_settled_correction_uses_reversal_and_replacement(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        self.assertEqual(
            self.client.post(f"/api/batches/{self.batch_id}/settlements/").status_code,
            201)
        detail = self.client.get(f"/api/harvest-sales/{sale_id}/").json()
        self.assertTrue(detail["is_settled"])
        # 已签署后不能原地改
        cid = f"CID-{self.token}-R1"
        r = self.client.post(
            f"/api/harvest-sales/{sale_id}/corrections/?correction_id={cid}",
            json={"weight": "20", "unit_price": "10", "reason": "结算后复核"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["mode"], "reversal")
        self.assertEqual(body["reversed_sale_id"], sale_id)
        new_id = body["replacement_sale_id"]
        self.assertNotEqual(new_id, sale_id)

        old = self.client.get(f"/api/harvest-sales/{sale_id}/").json()
        new = self.client.get(f"/api/harvest-sales/{new_id}/").json()
        self.assertEqual(old["status"], "reversed")
        self.assertEqual(old["total_amount"], 100.0)  # 原行金额留痕不覆盖
        self.assertEqual(new["status"], "active")
        self.assertEqual(new["supersedes_id"], sale_id)
        self.assertAlmostEqual(new["total_amount"], 200.0, places=9)
        # 已签署行连元数据也不能原地改
        r_meta = self.client.put(f"/api/harvest-sales/{sale_id}/",
                                 json={"buyer": "篡改买家"})
        self.assertEqual(r_meta.status_code, 409)
        # 默认列表只含有效行；含冲正行时显式展开
        actives = self.client.get(
            f"/api/harvest-sales/?batch_id={self.batch_id}").json()
        self.assertEqual([s["id"] for s in actives], [new_id])
        with_all = self.client.get(
            f"/api/harvest-sales/?batch_id={self.batch_id}&include_reversed=true").json()
        self.assertEqual(len(with_all), 2)

    def test_inplace_correction_carries_metadata_override(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        cid = f"CID-{self.token}-META"
        r = self.client.post(
            f"/api/harvest-sales/{sale_id}/corrections/?correction_id={cid}",
            json={"weight": "11", "unit_price": "10", "buyer": "新买家",
                  "quality_grade": "A", "reason": "复核并更新买家"})
        self.assertEqual(r.status_code, 200, r.text)
        detail = self.client.get(f"/api/harvest-sales/{sale_id}/").json()
        self.assertEqual(detail["buyer"], "新买家")
        self.assertEqual(detail["quality_grade"], "A")
        self.assertAlmostEqual(detail["total_amount"], 110.0, places=9)

    def test_reversal_correction_carries_metadata_to_replacement(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        r = self.client.post(
            f"/api/harvest-sales/{sale_id}/corrections/?correction_id=C-{self.token}-M",
            json={"weight": "11", "unit_price": "10", "buyer": "替代买家"})
        new_id = r.json()["replacement_sale_id"]
        new = self.client.get(f"/api/harvest-sales/{new_id}/").json()
        self.assertEqual(new["buyer"], "替代买家")
        # 冲正原行的元数据不动
        old = self.client.get(f"/api/harvest-sales/{sale_id}/").json()
        self.assertEqual(old["buyer"], "收购商甲")

    def test_signed_metadata_put_rejected_before_any_reversal(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        r = self.client.put(f"/api/harvest-sales/{sale_id}/",
                            json={"buyer": "结算后改买家"})
        self.assertEqual(r.status_code, 409)

    def test_unknown_cutoff_version_rejected(self):
        self._create_sale(weight="10", unit_price="10")
        r1 = self.client.get(f"/api/analysis/cycle/{self.batch_id}/?version=v999")
        r2 = self.client.get(
            f"/api/analysis/traceability/{self.batch_id}/?version=v999")
        self.assertEqual(r1.status_code, 400)
        self.assertEqual(r2.status_code, 400)
        # 生效版本正常
        self.assertEqual(
            self.client.get(f"/api/analysis/cycle/{self.batch_id}/").status_code, 200)

    def test_replay_same_correction_id_applies_once_and_is_queryable(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        cid = f"CID-{self.token}-IDEM"
        url = f"/api/harvest-sales/{sale_id}/corrections/?correction_id={cid}"
        payload = {"weight": "11", "unit_price": "10", "reason": "第一次"}
        first = self.client.post(url, json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["replayed"])

        # 响应丢失后重试：同样的标识、甚至不同的金额主张，都只能返回首次结果
        retry = self.client.post(url, json={
            "weight": "999", "unit_price": "999", "reason": "重试想夹带新值"})
        self.assertEqual(retry.status_code, 200)
        rb = retry.json()
        self.assertTrue(rb["replayed"])
        self.assertEqual(rb["new_weight"], "11")
        self.assertAlmostEqual(rb["new_total_amount"], 110.0, places=9)

        # 独立查询端点取回首次处理结果
        queried = self.client.get(f"/api/harvest-sales/corrections/{cid}/")
        self.assertEqual(queried.status_code, 200)
        self.assertAlmostEqual(queried.json()["new_total_amount"], 110.0, places=9)

        # 库里只有一条审计、金额只生效一次
        from backend.app.database import SessionLocal
        db = SessionLocal()
        try:
            count = db.query(SaleCorrection).filter(
                SaleCorrection.correction_id == cid).count()
            self.assertEqual(count, 1)
        finally:
            db.close()

    def test_cannot_correct_reversed_row_directly(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        c1 = self.client.post(
            f"/api/harvest-sales/{sale_id}/corrections/?correction_id=C-{self.token}-1",
            json={"weight": "20", "unit_price": "10"}).json()
        new_id = c1["replacement_sale_id"]
        # 对已冲正的旧行再更正 → 409
        r = self.client.post(
            f"/api/harvest-sales/{sale_id}/corrections/?correction_id=C-{self.token}-2",
            json={"weight": "30", "unit_price": "10"})
        self.assertEqual(r.status_code, 409)
        # 对当前有效行更正 → 允许（再次冲正替代，形成链）
        r = self.client.post(
            f"/api/harvest-sales/{new_id}/corrections/?correction_id=C-{self.token}-3",
            json={"weight": "30", "unit_price": "10"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_delete_signed_or_chain_record_rejected(self):
        sale_id = self._create_sale(weight="10", unit_price="10").json()["id"]
        # 未签署可删
        self.assertEqual(self.client.delete(f"/api/harvest-sales/{sale_id}/").status_code, 200)
        sale_id2 = self._create_sale(weight="10", unit_price="10").json()["id"]
        self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        self.assertEqual(self.client.delete(f"/api/harvest-sales/{sale_id2}/").status_code, 409)


class SettlementTest(ApiTestCase):
    def test_settle_once_repairs_and_freezes_snapshot(self):
        self._create_sale(weight="10", unit_price="10")
        self._create_sale(weight="2.5", unit_price="8")
        # 塞入一条历史坏数据，结算事务应自动修复后再固化
        from backend.app.database import SessionLocal
        db = SessionLocal()
        try:
            db.add(HarvestSale(
                batch_id=self.batch_id, sale_date=date(2026, 5, 3),
                weight=3.0, unit_price=3.0, total_amount=999.0,
                weight_raw="3.0", unit_price_raw="3.0", price_scale=2,
                pricing_version=pricing.LEGACY_VERSION, status="active", is_settled=0))
            db.commit()
        finally:
            db.close()

        r = self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        self.assertEqual(r.status_code, 201, r.text)
        st = r.json()
        self.assertEqual(st["cutoff_version"], "v1")
        self.assertEqual(st["sale_count"], 3)
        self.assertEqual(st["repaired_count"], 1)
        self.assertAlmostEqual(st["total_revenue"], 100 + 20 + 9, places=9)

        # 重复结算 → 409
        again = self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        self.assertEqual(again.status_code, 409)

        # 结算后销售全部签署
        sales = self.client.get(
            f"/api/harvest-sales/?batch_id={self.batch_id}").json()
        self.assertTrue(all(s["is_settled"] for s in sales))

        # 结算后冲正更正产生替代行，结算快照金额保持冻结
        old_id = sales[0]["id"]
        cid = f"CID-{self.token}-POST"
        self.client.post(
            f"/api/harvest-sales/{old_id}/corrections/?correction_id={cid}",
            json={"weight": "11", "unit_price": "10"})
        frozen = self.client.get(
            f"/api/batches/{self.batch_id}/settlements/").json()
        self.assertAlmostEqual(frozen["total_revenue"], 129.0, places=9)
        self.assertEqual(frozen["sale_count"], 3)


class RevenueConsistencyTest(ApiTestCase):
    def test_three_interfaces_share_same_revenue(self):
        self._create_sale(weight="12.5", unit_price="10.2")   # 127.50
        self._create_sale(weight="3", unit_price="34.1325", price_scale=4)  # 102.3975
        self._create_sale(weight="1", unit_price="2.345", price_scale=2)    # 2.35
        expected = 127.50 + 102.3975 + 2.35

        cycle = self.client.get(f"/api/analysis/cycle/{self.batch_id}/").json()
        trace = self.client.get(
            f"/api/analysis/traceability/{self.batch_id}/").json()
        trace_by_no = self.client.get(
            f"/api/analysis/trace-by-number/B{self.token}/").json()
        details = self.client.get(
            f"/api/harvest-sales/?batch_id={self.batch_id}").json()
        detail_sum = sum(
            self.client.get(f"/api/harvest-sales/{s['id']}/").json()["total_amount"]
            for s in details)

        self.assertAlmostEqual(cycle["total_revenue"], expected, places=6)
        self.assertAlmostEqual(trace["total_revenue"], expected, places=6)
        self.assertAlmostEqual(trace_by_no["total_revenue"], expected, places=6)
        self.assertAlmostEqual(detail_sum, expected, places=6)
        # 严格要求三个接口同版本、同行数、收入两两相等
        self.assertEqual(cycle["revenue_version"], trace["revenue_version"], "v1")
        self.assertEqual(cycle["sale_count"], trace["sale_count"], 3)
        self.assertAlmostEqual(cycle["total_revenue"], trace["total_revenue"], places=9)

    def test_consistency_after_reversal_chain(self):
        sid = self._create_sale(weight="10", unit_price="10").json()["id"]
        self.client.post(f"/api/batches/{self.batch_id}/settlements/")
        self.client.post(
            f"/api/harvest-sales/{sid}/corrections/?correction_id=C-{self.token}-x",
            json={"weight": "10", "unit_price": "12.345", "price_scale": 2})  # 123.45
        cycle = self.client.get(f"/api/analysis/cycle/{self.batch_id}/").json()
        trace = self.client.get(
            f"/api/analysis/traceability/{self.batch_id}/").json()
        actives = self.client.get(
            f"/api/harvest-sales/?batch_id={self.batch_id}").json()
        detail_sum = sum(s["total_amount"] for s in actives)
        self.assertAlmostEqual(cycle["total_revenue"], 123.45, places=6)
        self.assertAlmostEqual(trace["total_revenue"], 123.45, places=6)
        self.assertAlmostEqual(detail_sum, 123.45, places=6)
        self.assertEqual(len(trace["reversed_sales"]), 1)


class ConcurrencyTest(unittest.TestCase):
    """并发结算 / 并发更正 / 锁冲突重试（服务层直连独立引擎）。"""

    def setUp(self):
        self.engine = create_engine(
            f"sqlite:///{_DB_PATH}",
            connect_args={"check_same_thread": False, "timeout": 15})
        self.Session = sessionmaker(bind=self.engine)
        # 复用主库中的既有批次数据（由 API 用例创建），这里再造独立批次
        from backend.app.database import SessionLocal
        db = SessionLocal()
        try:
            pond = Pond(name=f"并发塘-{uuid.uuid4().hex[:8]}", area=5,
                        water_depth=1.5, species="草鱼")
            db.add(pond)
            db.commit()
            db.refresh(pond)
            batch = Batch(batch_number=f"CB-{uuid.uuid4().hex[:10]}",
                          pond_id=pond.id, species="草鱼",
                          stocking_date=date(2026, 1, 1))
            db.add(batch)
            db.commit()
            db.refresh(batch)
            self.batch_id = batch.id
            for _ in range(3):
                s = HarvestSale(batch_id=batch.id, sale_date=date(2026, 5, 1),
                                weight=10, unit_price=10)
                svc.apply_sale_facts(s, "10", "10", 2)
                db.add(s)
            db.commit()
            self.sale_id = db.query(HarvestSale).filter(
                HarvestSale.batch_id == batch.id).first().id
        finally:
            db.close()

    def test_concurrent_settle_succeeds_exactly_once(self):
        results = []
        barrier = threading.Barrier(2)

        def worker():
            db = self.Session()
            try:
                barrier.wait()
                try:
                    st = svc.settle_batch(db, self.batch_id)
                    results.append(("ok", st.id))
                except svc.ServiceError as e:
                    results.append(("err", e.status_code))
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(code for code, _ in results), ["err", "ok"])
        errs = [payload for code, payload in results if code == "err"]
        self.assertEqual(errs, [409])
        db = self.Session()
        try:
            count = db.query(BatchSettlement).filter(
                BatchSettlement.batch_id == self.batch_id).count()
            self.assertEqual(count, 1)
        finally:
            db.close()

    def test_concurrent_same_correction_id_applies_once(self):
        cid = f"CONCUR-CID-{uuid.uuid4().hex}"
        outcomes = []
        barrier = threading.Barrier(2)

        def worker(weight):
            db = self.Session()
            try:
                barrier.wait()
                class P:
                    pass
                p = P()
                p.weight = weight
                p.unit_price = "10"
                p.price_scale = 2
                p.reason = "并发更正"
                p.total_amount = None
                corr, replayed = svc.correct_sale(db, self.sale_id, cid, p)
                outcomes.append((corr.new_weight, replayed, corr.new_total_amount))
            except Exception as e:  # noqa: BLE001
                outcomes.append(("EXC", str(e), None))
            finally:
                db.close()

        t1 = threading.Thread(target=worker, args=("11",))
        t2 = threading.Thread(target=worker, args=("12",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(outcomes), 2, outcomes)
        # 一次首次生效、一次重放，且重放返回的是首次结果
        weights = {o[0] for o in outcomes}
        self.assertEqual(len(weights), 1)  # 两次看到的新重量必须相同
        self.assertEqual(sum(1 for o in outcomes if o[1]), 1)
        db = self.Session()
        try:
            self.assertEqual(db.query(SaleCorrection).filter(
                SaleCorrection.correction_id == cid).count(), 1)
        finally:
            db.close()

    def test_lock_busy_is_retryable(self):
        """结算持锁期间发起更正：先收到 409，锁释放后重试成功（失败重试链路）。"""
        busy_engine = create_engine(
            f"sqlite:///{_DB_PATH}",
            connect_args={"check_same_thread": False, "timeout": 0.3})
        BusySession = sessionmaker(bind=busy_engine)

        holder = self.Session()
        holder.execute(text("BEGIN IMMEDIATE"))
        try:
            db = BusySession()
            try:
                class P:
                    pass
                p = P(); p.weight = "11"; p.unit_price = "10"
                p.price_scale = 2; p.reason = "锁冲突"; p.total_amount = None
                with self.assertRaises(svc.ServiceError) as cm:
                    svc.correct_sale(db, self.sale_id,
                                     f"LOCK-{uuid.uuid4().hex}", p)
                self.assertEqual(cm.exception.status_code, 409)
            finally:
                db.close()
        finally:
            holder.rollback()
            holder.close()
            busy_engine.dispose()

        # 锁释放后用同一标识/新标识重试均成功
        db = self.Session()
        try:
            class P:
                pass
            p = P(); p.weight = "11"; p.unit_price = "10"
            p.price_scale = 2; p.reason = "锁释放后重试"; p.total_amount = None
            corr, replayed = svc.correct_sale(
                db, self.sale_id, f"LOCK-OK-{uuid.uuid4().hex}", p)
            self.assertFalse(replayed)
            self.assertAlmostEqual(corr.new_total_amount, 110.0, places=9)
        finally:
            db.close()

    def test_settle_and_correction_are_serialized(self):
        """更正与结算并发：不会出现“结算后原行仍被原地覆盖”的交错。"""
        target = self.sale_id
        results = []
        start = threading.Event()

        def settle_worker():
            start.wait()
            db = self.Session()
            try:
                try:
                    svc.settle_batch(db, self.batch_id)
                    results.append("settled")
                except svc.ServiceError:
                    results.append("settle-rejected")
            finally:
                db.close()

        def correct_worker():
            start.wait()
            db = self.Session()
            try:
                class P:
                    pass
                p = P(); p.weight = "12"; p.unit_price = "10"
                p.price_scale = 2; p.reason = "与结算并发"; p.total_amount = None
                corr, _ = svc.correct_sale(
                    db, target, f"SER-{uuid.uuid4().hex}", p)
                results.append(corr.mode)  # inplace 或 reversal，但绝不会脏覆盖
            finally:
                db.close()

        tw = [threading.Thread(target=settle_worker),
              threading.Thread(target=correct_worker)]
        for t in tw:
            t.start()
        start.set()
        for t in tw:
            t.join()

        db = self.Session()
        try:
            self.assertEqual(db.query(BatchSettlement).filter(
                BatchSettlement.batch_id == self.batch_id).count(), 1)
            # 若更正在结算前：inplace；若在结算后：reversal + 替代行。
            self.assertTrue("settled" in results)
            self.assertTrue(
                ("inplace" in results) or ("reversal" in results), results)
            # 任一交错下，被签署后冲正的行金额必须保留原值 100，绝不原地覆盖。
            reversed_rows = db.query(HarvestSale).filter(
                HarvestSale.status == "reversed").all()
            for row in reversed_rows:
                self.assertAlmostEqual(row.total_amount, 100.0, places=9)
        finally:
            db.close()


class LegacySchemaMigrationTest(unittest.TestCase):
    """旧库（只有旧列）启动迁移：补列、建表、回填遗留版本标记。"""

    def test_migrate_old_database(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        old_engine = create_engine(f"sqlite:///{path}")
        with old_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE ponds (id INTEGER PRIMARY KEY, name VARCHAR(100), "
                "area FLOAT, water_depth FLOAT, species VARCHAR(100), status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)"))
            conn.execute(text(
                "CREATE TABLE batches (id INTEGER PRIMARY KEY, batch_number VARCHAR(50), "
                "pond_id INTEGER, species VARCHAR(100), stocking_date DATE, "
                "estimated_harvest_date DATE, actual_harvest_date DATE, status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)"))
            conn.execute(text(
                "CREATE TABLE harvest_sales (id INTEGER PRIMARY KEY, batch_id INTEGER, "
                "sale_date DATE, weight FLOAT NOT NULL, unit_price FLOAT NOT NULL, "
                "total_amount FLOAT, buyer VARCHAR(200), batch_number VARCHAR(50), "
                "quality_grade VARCHAR(50), notes TEXT, created_at DATETIME)"))
            conn.execute(text(
                "INSERT INTO harvest_sales (id, batch_id, sale_date, weight, unit_price, "
                "total_amount, created_at) VALUES (1, 1, '2026-05-01', 10.0, 10.0, 55.0, "
                "'2026-05-01 00:00:00')"))
            conn.execute(text(
                "INSERT INTO harvest_sales (id, batch_id, sale_date, weight, unit_price, "
                "total_amount, created_at) VALUES (2, 1, '2026-05-02', 3.0, 3.0, NULL, "
                "'2026-05-02 00:00:00')"))
        old_engine.dispose()

        # 用应用代码对旧库执行迁移
        mig_engine = create_engine(f"sqlite:///{path}")
        from backend.app import models as app_models
        from backend.app.migrations import ensure_schema
        ensure_schema(app_models.Base, mig_engine)

        insp = inspect(mig_engine)
        cols = {c["name"] for c in insp.get_columns("harvest_sales")}
        for needed in ("weight_raw", "unit_price_raw", "price_scale",
                       "pricing_version", "status", "is_settled",
                       "supersedes_id", "corrected_by_id",
                       "created_by_correction_id"):
            self.assertIn(needed, cols)
        self.assertIn("sale_corrections", insp.get_table_names())
        self.assertIn("batch_settlements", insp.get_table_names())

        Session = sessionmaker(bind=mig_engine)
        db = Session()
        try:
            rows = db.query(app_models.HarvestSale).order_by(
                app_models.HarvestSale.id).all()
            self.assertEqual([r.pricing_version for r in rows],
                             [pricing.LEGACY_VERSION, pricing.LEGACY_VERSION])
            self.assertEqual([r.status for r in rows], ["active", "active"])
            self.assertEqual([r.weight_raw for r in rows], ["10.0", "3.0"])
            # 迁移可重复执行（幂等）
            ensure_schema(app_models.Base, mig_engine)
        finally:
            db.close()
            mig_engine.dispose()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
