"""统一计价规则。

总金额只能由服务端按本模块的唯一规则产生：

    总金额 = (重量 × 单价) 按 price_scale 位小数四舍五入（ROUND_HALF_UP）

所有金额（创建、更正、冲正替代、历史修复、周期分析汇总）都必须经过这里，
保证周期分析、追溯接口与销售详情在同一截止版本下得到完全相同的收入。
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional, Tuple

DEFAULT_PRICE_SCALE = 2
MIN_PRICE_SCALE = 0
MAX_PRICE_SCALE = 6

# 允许客户/调用方表达"无金额"的唯一方式；任何具体数值都以服务端计算为准。
_CLIENT_CAN_SUPPLY_TOTAL = False


class PriceValidationError(ValueError):
    """输入的重量/单价/精度不合法。"""


def normalize_scale(scale: Optional[int]) -> int:
    if scale is None:
        return DEFAULT_PRICE_SCALE
    if not isinstance(scale, int) or isinstance(scale, bool):
        raise PriceValidationError("计价精度必须为整数")
    if scale < MIN_PRICE_SCALE or scale > MAX_PRICE_SCALE:
        raise PriceValidationError(
            f"计价精度必须在 {MIN_PRICE_SCALE}~{MAX_PRICE_SCALE} 位小数之间"
        )
    return scale


def _to_decimal(value, field: str, allow_negative: bool = False) -> Decimal:
    if value is None:
        raise PriceValidationError(f"{field}不能为空")
    if isinstance(value, bool):
        raise PriceValidationError(f"{field}必须为非负数值")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PriceValidationError(f"{field}不是合法数值")
    if d.is_nan() or d.is_infinite() or (d < 0 and not allow_negative):
        raise PriceValidationError(f"{field}必须为非负有限数值")
    return d


def calculate_total_amount(
    weight, unit_price, scale: Optional[int] = None, allow_negative: bool = False
) -> float:
    """统一入口：按规则计算总金额并返回 float（落库/响应用）。

    allow_negative 仅供服务端构造冲正条目（负重量）使用，对外接口恒为 False。
    """
    scale = normalize_scale(scale)
    w = _to_decimal(weight, "重量", allow_negative)
    p = _to_decimal(unit_price, "单价")
    quant = Decimal(1).scaleb(-scale)  # 1, 0.1, 0.01 ...
    total = (w * p).quantize(quant, rounding=ROUND_HALF_UP)
    return float(total)


def calc_decimal(
    weight, unit_price, scale: Optional[int] = None, allow_negative: bool = False
) -> Decimal:
    """同 calculate_total_amount，但返回未转 float 的 Decimal（精确比较/汇总用）。"""
    scale = normalize_scale(scale)
    w = _to_decimal(weight, "重量", allow_negative)
    p = _to_decimal(unit_price, "单价")
    quant = Decimal(1).scaleb(-scale)
    return (w * p).quantize(quant, rounding=ROUND_HALF_UP)


def decimal_sum(values) -> Decimal:
    """对可迭代的 float 金额做精确求和（分析/结算快照用）。"""
    total = Decimal("0")
    for v in values:
        if v is not None:
            total += Decimal(str(v))
    return total


def diagnose(
    stored_amount: Optional[float],
    weight,
    unit_price,
    scale: Optional[int] = None,
) -> Tuple[bool, bool, bool, float]:
    """对单条历史记录做金额体检。

    返回 (一致?, 缺失?, 超精度?, 应有金额)。
    - missing:   总金额为 None
    - over_precision: 存储值的有效小数位数超过 price_scale
    - inconsistent: 非缺失时存储值与统一规则重算值不精确相等
    """
    scale = normalize_scale(scale)
    expected_d = calc_decimal(weight, unit_price, scale)

    if stored_amount is None:
        return False, True, False, float(expected_d)

    stored_d = Decimal(str(stored_amount))
    # 以原始表示的小数位数判定超精度（不做 normalize，保留尾随 0 的语义）
    exponent = stored_d.as_tuple().exponent
    over = isinstance(exponent, int) and exponent < -scale

    mismatched = stored_d != expected_d
    consistent = (not over) and (not mismatched)
    return consistent, False, over, float(expected_d)
