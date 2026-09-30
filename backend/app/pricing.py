"""统一计价规则（版本化）。

销售总金额只能由服务端通过本模块产生：以调用方提交的原始重量与单价
（字符串保真）做 Decimal 精确运算，并按计价精度做 ROUND_HALF_UP 舍入。
任何接口都不得直接采信客户端提交的总金额。

版本：
- ``v1``        当前生效规则：amount = round_half_up(weight * unit_price, price_scale)
- ``legacy-v0`` 历史数据标记（float 直乘、无舍入约定），修复后重写为 v1。
"""

from decimal import Decimal, ROUND_HALF_UP, localcontext, InvalidOperation
from typing import Any

PRICING_VERSION = "v1"
LEGACY_VERSION = "legacy-v0"

DEFAULT_PRICE_SCALE = 2
MAX_PRICE_SCALE = 6

_QUANTIZED_UNITS = {
    scale: Decimal(1).scaleb(-scale) for scale in range(MAX_PRICE_SCALE + 1)
}


class PricingError(ValueError):
    """计价输入非法（非数值、精度越界、负数等）。"""


def normalize_price_scale(price_scale: Any) -> int:
    """归一化计价精度，允许 0..MAX_PRICE_SCALE，缺省为 2。"""
    if price_scale is None:
        return DEFAULT_PRICE_SCALE
    try:
        scale = int(price_scale)
    except (TypeError, ValueError) as exc:
        raise PricingError("计价精度必须是 0~6 的整数") from exc
    if scale < 0 or scale > MAX_PRICE_SCALE:
        raise PricingError(f"计价精度必须在 0~{MAX_PRICE_SCALE} 之间")
    return scale


def to_decimal(value: Any, field: str = "数值") -> Decimal:
    """把外部输入（str/int/float/Decimal）转为非科学计数法的精确 Decimal。"""
    if isinstance(value, Decimal):
        dec = value
    elif value is None:
        raise PricingError(f"{field}不能为空")
    else:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise PricingError(f"{field}不是合法数值: {value!r}") from exc
    if not dec.is_finite():
        raise PricingError(f"{field}必须是有限数值")
    return dec


def validate_facts(weight: Any, unit_price: Any, price_scale: Any = None) -> tuple[Decimal, Decimal, int]:
    """校验并返回（重量, 单价, 精度）事实三元组。"""
    scale = normalize_price_scale(price_scale)
    w = to_decimal(weight, "重量")
    p = to_decimal(unit_price, "单价")
    if w <= 0:
        raise PricingError("重量必须大于 0")
    if p < 0:
        raise PricingError("单价不能为负数")
    return w, p, scale


def compute_amount(weight: Any, unit_price: Any, price_scale: Any = None) -> Decimal:
    """按 v1 规则计算总金额：Decimal 相乘后按精度 ROUND_HALF_UP。"""
    w, p, scale = validate_facts(weight, unit_price, price_scale)
    with localcontext() as ctx:
        ctx.prec = 38
        product = w * p
        return product.quantize(_QUANTIZED_UNITS[scale], rounding=ROUND_HALF_UP)


def decimal_places(value: Decimal) -> int:
    """十进制小数位数（如 Decimal('34.1325') -> 4）。"""
    exponent = value.as_tuple().exponent
    return max(0, -exponent)


def stored_total_to_decimal(stored: Any) -> Decimal | None:
    """把历史存储金额转为 Decimal；NULL/不可解析返回 None。"""
    if stored is None:
        return None
    try:
        dec = Decimal(str(stored))
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None
