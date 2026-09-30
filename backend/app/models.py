from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
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
    """出塘销售记录。

    金额事实链：weight_raw / unit_price_raw（调用方原始字符串，可追溯事实）、
    price_scale（计价精度）、pricing_version（生效计价版本）共同决定 total_amount，
    总金额只能由服务端 pricing 模块产生，不接受客户端直接提交。

    状态机：
    - active        有效记录（参与收入汇总）
    - reversed      已被冲正（保留原行留痕，不再参与收入；correction_id 指向冲正动作）
    已结算批次的记录 is_settled=1，更正时只能冲正旧行并追加替代行，禁止原地覆盖。
    """
    __tablename__ = "harvest_sales"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    sale_date = Column(Date, nullable=False, comment="销售日期")
    weight = Column(Float, nullable=False, comment="重量(公斤)，服务端按事实归一化")
    unit_price = Column(Float, nullable=False, comment="单价(元/公斤)，服务端按事实归一化")
    total_amount = Column(Float, comment="总金额(元)，只能由服务端按统一规则计算")
    buyer = Column(String(200), comment="买家")
    batch_number = Column(String(50), comment="追溯批次号")
    quality_grade = Column(String(50), comment="质量等级")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    # ---- 可追溯事实与更正链 ----
    weight_raw = Column(String(64), comment="原始重量（调用方提交原文）")
    unit_price_raw = Column(String(64), comment="原始单价（调用方提交原文）")
    price_scale = Column(Integer, default=2, comment="计价精度(小数位数)")
    pricing_version = Column(String(20), default="v1", comment="计价规则生效版本")
    status = Column(String(20), default="active", nullable=False, index=True,
                    comment="记录状态: active, reversed")
    is_settled = Column(Integer, default=0, nullable=False, comment="是否已进入批次结算(签署)")
    supersedes_id = Column(Integer, ForeignKey("harvest_sales.id"),
                           comment="本替代行所冲正的原销售行")
    # SQLite 无法 ALTER 增加外键，这里以普通整型列记录审计关联，
    # 由服务端保证引用完整性（SaleCorrection 表保留指向销售行的外键）。
    corrected_by_id = Column(Integer, comment="本行被哪次更正冲正/修正(sale_corrections.id)")
    created_by_correction_id = Column(Integer, comment="本行由哪次更正(替代行)产生(sale_corrections.id)")

    batch = relationship("Batch", back_populates="harvest_sales")
    supersedes = relationship("HarvestSale", remote_side=[id], foreign_keys=[supersedes_id])


class SaleCorrection(Base):
    """销售更正审计记录。

    correction_id 为调用方提供的幂等键（唯一）：同一标识重复提交只能生效一次，
    响应丢失后凭该标识可查询首次处理结果。
    """
    __tablename__ = "sale_corrections"

    id = Column(Integer, primary_key=True, index=True)
    correction_id = Column(String(64), unique=True, nullable=False, index=True,
                           comment="调用方幂等更正标识")
    sale_id = Column(Integer, ForeignKey("harvest_sales.id"), nullable=False,
                     comment="被更正的原始销售行")
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    mode = Column(String(20), nullable=False, comment="更正方式: inplace, reversal")
    reason = Column(String(500), comment="更正原因")

    old_weight = Column(String(64), comment="更正前原始重量")
    old_unit_price = Column(String(64), comment="更正前原始单价")
    old_price_scale = Column(Integer, comment="更正前计价精度")
    old_total_amount = Column(Float, comment="更正前总金额")
    old_pricing_version = Column(String(20), comment="更正前计价版本")

    new_weight = Column(String(64), comment="更正后原始重量")
    new_unit_price = Column(String(64), comment="更正后原始单价")
    new_price_scale = Column(Integer, comment="更正后计价精度")
    new_total_amount = Column(Float, comment="更正后总金额")
    new_pricing_version = Column(String(20), comment="更正后计价版本")

    reversed_sale_id = Column(Integer, ForeignKey("harvest_sales.id"),
                              comment="冲正方式下被冲正的原行")
    replacement_sale_id = Column(Integer, ForeignKey("harvest_sales.id"),
                                 comment="冲正方式下产生的替代行")
    created_at = Column(DateTime, default=datetime.utcnow)


class BatchSettlement(Base):
    """批次结算（签署）记录。

    每次结算在一个事务内固化截止版本收入快照；同一批次只能结算一次。
    结算后本批销售全部签署(is_settled=1)，更正必须走冲正+替代。
    """
    __tablename__ = "batch_settlements"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_batch_settlements_batch_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False,
                      comment="批次（同一批次仅允许一次成功结算）")
    settlement_no = Column(String(64), unique=True, nullable=False, comment="结算单号")
    cutoff_version = Column(String(20), nullable=False, comment="结算截止计价版本")
    total_revenue = Column(Float, nullable=False, comment="截止版本下有效收入合计")
    sale_count = Column(Integer, nullable=False, default=0, comment="纳入结算的有效销售行数")
    sale_ids_json = Column(Text, comment="纳入结算的销售行ID集合(JSON)，用于截止版本回溯")
    repaired_count = Column(Integer, nullable=False, default=0, comment="结算事务内自动修复的历史坏行数")
    status = Column(String(20), default="settled", nullable=False, comment="结算状态")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="settlements")
