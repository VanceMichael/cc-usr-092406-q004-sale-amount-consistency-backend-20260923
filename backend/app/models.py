from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


class Pond(Base):
    __tablename__ = "ponds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    area = Column(Float, nullable=False, comment="面积(亩)")
    water_depth = Column(Float, nullable=False, comment="水深(米)")
    species = Column(String(100), comment="养殖品种")
    status = Column(String(20), default="active", comment="状态: active, inactive")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batches = relationship("Batch", back_populates="pond")


class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_number = Column(String(50), unique=True, index=True, nullable=False, comment="批次号")
    pond_id = Column(Integer, ForeignKey("ponds.id"), nullable=False)
    species = Column(String(100), nullable=False, comment="养殖品种")
    stocking_date = Column(Date, nullable=False, comment="放苗日期")
    estimated_harvest_date = Column(Date, comment="预计收获日期")
    actual_harvest_date = Column(Date, comment="实际收获日期")
    status = Column(String(20), default="active", comment="状态: active, harvested, closed")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pond = relationship("Pond", back_populates="batches")
    stocking_records = relationship("StockingRecord", back_populates="batch")
    feeding_records = relationship("FeedingRecord", back_populates="batch")
    water_quality_records = relationship("WaterQualityRecord", back_populates="batch")
    medication_records = relationship("MedicationRecord", back_populates="batch")
    cost_records = relationship("CostRecord", back_populates="batch")
    harvest_sales = relationship("HarvestSale", back_populates="batch")
    settlements = relationship("BatchSettlement", back_populates="batch")


class StockingRecord(Base):
    __tablename__ = "stocking_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    species = Column(String(100), nullable=False, comment="品种")
    quantity = Column(Integer, nullable=False, comment="数量(尾)")
    source = Column(String(200), comment="来源")
    batch_number = Column(String(50), comment="苗种批次号")
    weight_per_unit = Column(Float, comment="单重(克/尾)")
    total_weight = Column(Float, comment="总重量(公斤)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="stocking_records")


class FeedingRecord(Base):
    __tablename__ = "feeding_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    feeding_date = Column(Date, nullable=False, comment="投喂日期")
    feed_type = Column(String(100), nullable=False, comment="饲料类型")
    feed_quantity = Column(Float, nullable=False, comment="投喂量(公斤)")
    feeding_time = Column(String(20), comment="投喂时间")
    weather = Column(String(50), comment="天气情况")
    water_temperature = Column(Float, comment="水温(℃)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="feeding_records")


class WaterQualityRecord(Base):
    __tablename__ = "water_quality_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    record_date = Column(Date, nullable=False, comment="检测日期")
    record_time = Column(String(20), comment="检测时间")
    water_temperature = Column(Float, comment="水温(℃)")
    ph_value = Column(Float, comment="pH值")
    dissolved_oxygen = Column(Float, comment="溶解氧(mg/L)")
    ammonia_nitrogen = Column(Float, comment="氨氮(mg/L)")
    nitrite = Column(Float, comment="亚硝酸盐(mg/L)")
    transparency = Column(Float, comment="透明度(cm)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="water_quality_records")


class MedicationRecord(Base):
    __tablename__ = "medication_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    medication_date = Column(Date, nullable=False, comment="用药日期")
    drug_name = Column(String(200), nullable=False, comment="药品名称")
    drug_type = Column(String(50), comment="药品类型")
    dosage = Column(Float, comment="用量")
    dosage_unit = Column(String(20), default="kg", comment="用量单位")
    administration_method = Column(String(100), comment="施用方法")
    purpose = Column(String(200), comment="用途")
    manufacturer = Column(String(200), comment="生产厂家")
    batch_number = Column(String(50), comment="药品批次号")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="medication_records")


class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    cost_date = Column(Date, nullable=False, comment="费用日期")
    cost_type = Column(String(50), nullable=False, comment="费用类型: feed, medicine, labor, electricity, other")
    amount = Column(Float, nullable=False, comment="金额(元)")
    description = Column(String(500), comment="费用描述")
    quantity = Column(Float, comment="数量")
    unit = Column(String(20), comment="单位")
    unit_price = Column(Float, comment="单价")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="cost_records")


class HarvestSale(Base):
    """出塘销售记录（版本化、可追溯）。

    原始事实（重量 weight、单价 unit_price、计价精度 price_scale、生效版本
    amount_version）一经写入不可原地修改；总金额 total_amount 只能由服务端按
    统一规则（见 services.pricing）产生。

    更正链路：
    - amount_status == "active"  正常生效条目
    - amount_status == "reversed" 已被冲正的历史条目（replaced_by 指向替代条目）
    - amount_status == "void"     冲正条目（与被冲正条目金额相反、同 weight/price）
    - root_sale_id 指向同一业务事实的版本链根条目；替代条目版本号递增。
    """

    __tablename__ = "harvest_sales"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    sale_date = Column(Date, nullable=False, comment="销售日期")
    weight = Column(Float, nullable=False, comment="重量(公斤)，原始事实")
    unit_price = Column(Float, nullable=False, comment="单价(元/公斤)，原始事实")
    total_amount = Column(Float, nullable=True, comment="总金额(元)，只能由服务端计算")
    buyer = Column(String(200), comment="买家")
    batch_number = Column(String(50), comment="追溯批次号")
    quality_grade = Column(String(50), comment="质量等级")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    # 计价可追溯事实
    price_scale = Column(Integer, nullable=False, default=2, comment="计价小数位精度")
    amount_version = Column(Integer, nullable=False, default=1, comment="生效版本，版本链内递增")
    amount_status = Column(
        String(20), nullable=False, default="active",
        comment="金额状态: active, reversed, void",
    )
    root_sale_id = Column(
        Integer, ForeignKey("harvest_sales.id"), nullable=True,
        comment="版本链根条目ID；根条目指向自身",
    )
    replaced_by_id = Column(
        Integer, ForeignKey("harvest_sales.id"), nullable=True,
        comment="冲正后替代条目ID",
    )
    correction_id = Column(
        String(64), nullable=True, index=True,
        comment="产生该条目的更正标识（幂等键）",
    )
    locked_version = Column(
        Integer, nullable=True,
        comment="锁定该条目的结算版本；None=未签署，可自动校正；非空=只能冲正替代",
    )
    effective_from = Column(
        Integer, nullable=False, default=0,
        comment="条目生效的起始结算版本；0 表示首个结算版本之前即生效",
    )
    amount_consistent = Column(
        Boolean, nullable=False, default=True,
        comment="总金额是否与重量×单价按精度一致（历史体检标记）",
    )

    batch = relationship("Batch", back_populates="harvest_sales")
    root = relationship("HarvestSale", remote_side=[id], foreign_keys=[root_sale_id])
    replaced_by = relationship("HarvestSale", remote_side=[id], foreign_keys=[replaced_by_id])


class BatchSettlement(Base):
    """批次结算（签署）记录。

    每个批次的结算版本号连续递增；一旦签署，当时生效的销售条目即被锁定，
    之后更正只能通过冲正+替代进行，替代条目的 settlement_id 指向更新的结算，
    截止版本分析仍可复现签署时的收入。
    """

    __tablename__ = "batch_settlements"
    __table_args__ = (
        UniqueConstraint("batch_id", "version", name="uq_settlement_batch_version"),
        UniqueConstraint("idempotency_key", name="uq_settlement_idempotency_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False, comment="结算版本号，批次内连续递增")
    settled_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment="签署时间")
    revenue = Column(Float, nullable=False, comment="签署时快照收入")
    cost = Column(Float, nullable=False, comment="签署时快照成本")
    profit = Column(Float, nullable=False, comment="签署时快照利润")
    sale_count = Column(Integer, nullable=False, default=0, comment="计入收入的销售条目数")
    cutoff_version = Column(Integer, nullable=False, comment="截止版本(=本结算版本)")
    idempotency_key = Column(String(64), nullable=True, comment="结算幂等键")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="settlements")
    items = relationship("SettlementSaleItem", back_populates="settlement")


class SettlementSaleItem(Base):
    """结算签署时计入收入的销售条目快照。

    截止版本分析（周期分析/追溯/详情）在 cutoff_version 等于某结算版本时，
    直接以该快照为准复现签署时收入，不受后续冲正替代影响。
    """

    __tablename__ = "settlement_sale_items"
    __table_args__ = (
        UniqueConstraint("settlement_id", "sale_id", name="uq_settlement_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    settlement_id = Column(Integer, ForeignKey("batch_settlements.id"), nullable=False, index=True)
    sale_id = Column(Integer, ForeignKey("harvest_sales.id"), nullable=False)
    sale_date = Column(Date, nullable=False)
    weight = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)
    total_amount = Column(Float, nullable=True)
    price_scale = Column(Integer, nullable=False)
    amount_version = Column(Integer, nullable=False)
    amount_status = Column(String(20), nullable=False, default="active")
    root_sale_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    settlement = relationship("BatchSettlement", back_populates="items")


class SaleCorrection(Base):
    """销售更正请求记录（幂等事实）。

    同一 correction_id 重复提交只生效一次：首次请求落库后，响应丢失重放或
    查询都返回同一结果；若重放携带不同请求体则冲突（409）。
    """

    __tablename__ = "sale_corrections"
    __table_args__ = (
        Index("ix_sale_corrections_corr", "correction_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    correction_id = Column(String(64), nullable=False, unique=True, comment="更正标识(幂等键)")
    sale_id = Column(Integer, ForeignKey("harvest_sales.id"), nullable=False, comment="被更正条目")
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    mode = Column(String(20), nullable=False, comment="更正方式: auto, reversal")
    request_hash = Column(String(128), nullable=False, comment="请求体指纹，重放冲突检测")
    status = Column(String(20), nullable=False, default="applied", comment="applied")
    result_sale_id = Column(
        Integer, ForeignKey("harvest_sales.id"), nullable=True,
        comment="更正产生的结果条目（自动校正为原条目，冲正为替代条目）",
    )
    reversal_sale_id = Column(
        Integer, ForeignKey("harvest_sales.id"), nullable=True,
        comment="冲正方式下的冲正条目ID",
    )
    detail = Column(Text, nullable=True, comment="结果摘要(JSON)")
    created_at = Column(DateTime, default=datetime.utcnow)


class IdempotentResult(Base):
    """通用幂等结果：请求键 -> 首次处理结果，支持响应丢失后重放/查询。"""

    __tablename__ = "idempotent_results"

    id = Column(Integer, primary_key=True, index=True)
    scope = Column(String(40), nullable=False, comment="业务域，如 harvest_sale_create")
    request_key = Column(String(64), nullable=False, unique=True, comment="幂等键")
    request_hash = Column(String(128), nullable=False, comment="首次请求体指纹")
    result_id = Column(Integer, nullable=True, comment="首次结果主键")
    response_json = Column(Text, nullable=False, comment="首次响应快照(JSON)")
    created_at = Column(DateTime, default=datetime.utcnow)


class AmountFixBatch(Base):
    """历史金额修复批次（分批识别与处理的游标/状态）。"""

    __tablename__ = "amount_fix_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_ref = Column(String(64), nullable=False, unique=True, index=True, comment="修复批次标识")
    status = Column(String(20), nullable=False, default="open", comment="open, scanning, completed")
    last_sale_id = Column(Integer, nullable=False, default=0, comment="扫描游标")
    total_scanned = Column(Integer, nullable=False, default=0)
    total_inconsistent = Column(Integer, nullable=False, default=0, comment="金额不一致数")
    total_missing = Column(Integer, nullable=False, default=0, comment="缺失金额数")
    total_over_precision = Column(Integer, nullable=False, default=0, comment="超出精度数")
    total_auto_fixed = Column(Integer, nullable=False, default=0, comment="未签署自动校正数")
    total_reversed = Column(Integer, nullable=False, default=0, comment="已签署冲正替代数")
    total_blocked = Column(Integer, nullable=False, default=0, comment="待冲正/受阻数")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AmountFixItem(Base):
    """修复批次内识别出的单条问题记录及处理结果。"""

    __tablename__ = "amount_fix_items"
    __table_args__ = (
        UniqueConstraint("fix_batch_id", "sale_id", name="uq_fix_item_sale"),
    )

    id = Column(Integer, primary_key=True, index=True)
    fix_batch_id = Column(Integer, ForeignKey("amount_fix_batches.id"), nullable=False, index=True)
    sale_id = Column(Integer, ForeignKey("harvest_sales.id"), nullable=False)
    issue = Column(String(30), nullable=False, comment="问题类型: inconsistent, missing, over_precision")
    old_amount = Column(Float, nullable=True)
    expected_amount = Column(Float, nullable=True)
    signed = Column(Boolean, nullable=False, default=False, comment="识别时是否已进入结算")
    action = Column(String(20), nullable=False, comment="处理: auto_fixed, reversal, pending")
    result_sale_id = Column(Integer, ForeignKey("harvest_sales.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
