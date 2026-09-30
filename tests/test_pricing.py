"""统一计价规则与舍入边界测试。"""

import unittest
from decimal import Decimal

from app.services import pricing


class PricingRuleTest(unittest.TestCase):
    def test_half_up_rounding_at_scale_two(self):
        # ROUND_HALF_UP：经典案例 2.675 应进位为 2.68，
        # 而 Python 内建 round 受浮点/银行家舍入影响得到 2.67
        self.assertEqual(pricing.calculate_total_amount(2.675, 1, 2), 2.68)
        self.assertEqual(round(2.675, 2), 2.67)
        # 第 3 位为 5 进位、为 4 舍去
        self.assertEqual(pricing.calculate_total_amount(1.005, 10, 2), 10.05)
        self.assertEqual(pricing.calculate_total_amount(3.335, 4, 2), 13.34)
        self.assertEqual(pricing.calculate_total_amount(2.5, 2.5, 2), 6.25)
        self.assertEqual(pricing.calculate_total_amount(1.004, 10, 2), 10.04)

    def test_custom_scale_rounding(self):
        self.assertEqual(pricing.calculate_total_amount(1.235, 1, 3), 1.235)
        self.assertEqual(pricing.calculate_total_amount(1.235, 1, 2), 1.24)
        self.assertEqual(pricing.calculate_total_amount(1.235, 1, 1), 1.2)
        self.assertEqual(pricing.calculate_total_amount(1.235, 1, 0), 1.0)

    def test_zero_and_integers(self):
        self.assertEqual(pricing.calculate_total_amount(0, 100, 2), 0.0)
        self.assertEqual(pricing.calculate_total_amount(10, 10, 0), 100.0)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(pricing.PriceValidationError):
            pricing.calculate_total_amount(-1, 10, 2)
        with self.assertRaises(pricing.PriceValidationError):
            pricing.calculate_total_amount(1, -10, 2)
        with self.assertRaises(pricing.PriceValidationError):
            pricing.normalize_scale(7)
        with self.assertRaises(pricing.PriceValidationError):
            pricing.normalize_scale(-1)
        with self.assertRaises(pricing.PriceValidationError):
            pricing.calculate_total_amount(None, 10, 2)

    def test_decimal_sum_is_exact(self):
        total = pricing.decimal_sum([0.1, 0.2, 43.155])
        self.assertEqual(total, Decimal("43.455"))

    def test_diagnose_classifies_issues(self):
        # 一致
        ok, missing, over, expected = pricing.diagnose(43.16, 3.5, 12.33, 2)
        self.assertTrue(ok)
        self.assertEqual(expected, 43.16)

        # 不一致（金额与重量×单价不符）
        ok, missing, over, expected = pricing.diagnose(43.99, 3.5, 12.33, 2)
        self.assertFalse(ok)
        self.assertFalse(over)
        self.assertFalse(missing)

        # 缺失金额
        ok, missing, over, expected = pricing.diagnose(None, 3.5, 12.33, 2)
        self.assertTrue(missing)
        self.assertFalse(ok)

        # 超精度：存储值小数位多于精度（按精度重算后值不同）
        ok, missing, over, expected = pricing.diagnose(12.345, 12.345, 1, 2)
        self.assertTrue(over)
        self.assertFalse(ok)

        # 未超精度：3 位精度存储 3 位
        ok, missing, over, expected = pricing.diagnose(12.345, 12.345, 1, 3)
        self.assertFalse(over)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
