from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Union
from datetime import date, datetime

class PondBase(BaseModel):
    name: str
    area: float
    water_depth: float
    species: Optional[str] = None
    status: Optional[str] = "active"

class PondCreate(PondBase):
    pass

class PondUpdate(BaseModel):
    name: Optional[str] = None
    area: Optional[float] = None
    water_depth: Optional[float] = None
    species: Optional[str] = None
    status: Optional[str] = None

class PondResponse(PondBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class BatchBase(BaseModel):
    batch_number: str
    pond_id: int
    species: str
    stocking_date: date
    estimated_harvest_date: Optional[date] = None
    actual_harvest_date: Optional[date] = None
    status: Optional[str] = "active"

class BatchCreate(BatchBase):
    pass

class BatchUpdate(BaseModel):
    batch_number: Optional[str] = None
    pond_id: Optional[int] = None
    species: Optional[str] = None
    stocking_date: Optional[date] = None
    estimated_harvest_date: Optional[date] = None
    actual_harvest_date: Optional[date] = None
    status: Optional[str] = None

class BatchResponse(BatchBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class StockingRecordBase(BaseModel):
    batch_id: int
    species: str
    quantity: int
    source: Optional[str] = None
    batch_number: Optional[str] = None
    weight_per_unit: Optional[float] = None
    total_weight: Optional[float] = None
    notes: Optional[str] = None

class StockingRecordCreate(StockingRecordBase):
    pass

class StockingRecordUpdate(BaseModel):
    batch_id: Optional[int] = None
    species: Optional[str] = None
    quantity: Optional[int] = None
    source: Optional[str] = None
    batch_number: Optional[str] = None
    weight_per_unit: Optional[float] = None
    total_weight: Optional[float] = None
    notes: Optional[str] = None

class StockingRecordResponse(StockingRecordBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True

class FeedingRecordBase(BaseModel):
    batch_id: int
    feeding_date: date
    feed_type: str
    feed_quantity: float
    feeding_time: Optional[str] = None
    weather: Optional[str] = None
    water_temperature: Optional[float] = None
    notes: Optional[str] = None

class FeedingRecordCreate(FeedingRecordBase):
    pass

class FeedingRecordUpdate(BaseModel):
    batch_id: Optional[int] = None
    feeding_date: Optional[date] = None
    feed_type: Optional[str] = None
    feed_quantity: Optional[float] = None
    feeding_time: Optional[str] = None
    weather: Optional[str] = None
    water_temperature: Optional[float] = None
    notes: Optional[str] = None

class FeedingRecordResponse(FeedingRecordBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True

class WaterQualityRecordBase(BaseModel):
    batch_id: int
    record_date: date
    record_time: Optional[str] = None
    water_temperature: Optional[float] = None
    ph_value: Optional[float] = None
    dissolved_oxygen: Optional[float] = None
    ammonia_nitrogen: Optional[float] = None
    nitrite: Optional[float] = None
    transparency: Optional[float] = None
    notes: Optional[str] = None

class WaterQualityRecordCreate(WaterQualityRecordBase):
    pass

class WaterQualityRecordUpdate(BaseModel):
    batch_id: Optional[int] = None
    record_date: Optional[date] = None
    record_time: Optional[str] = None
    water_temperature: Optional[float] = None
    ph_value: Optional[float] = None
    dissolved_oxygen: Optional[float] = None
    ammonia_nitrogen: Optional[float] = None
    nitrite: Optional[float] = None
    transparency: Optional[float] = None
    notes: Optional[str] = None

class WaterQualityRecordResponse(WaterQualityRecordBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True

class MedicationRecordBase(BaseModel):
    batch_id: int
    medication_date: date
    drug_name: str
    drug_type: Optional[str] = None
    dosage: Optional[float] = None
    dosage_unit: Optional[str] = "kg"
    administration_method: Optional[str] = None
    purpose: Optional[str] = None
    manufacturer: Optional[str] = None
    batch_number: Optional[str] = None
    notes: Optional[str] = None

class MedicationRecordCreate(MedicationRecordBase):
    pass

class MedicationRecordUpdate(BaseModel):
    batch_id: Optional[int] = None
    medication_date: Optional[date] = None
    drug_name: Optional[str] = None
    drug_type: Optional[str] = None
    dosage: Optional[float] = None
    dosage_unit: Optional[str] = None
    administration_method: Optional[str] = None
    purpose: Optional[str] = None
    manufacturer: Optional[str] = None
    batch_number: Optional[str] = None
    notes: Optional[str] = None

class MedicationRecordResponse(MedicationRecordBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True

class CostRecordBase(BaseModel):
    batch_id: int
    cost_date: date
    cost_type: str
    amount: float
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    notes: Optional[str] = None

class CostRecordCreate(CostRecordBase):
    pass

class CostRecordUpdate(BaseModel):
    batch_id: Optional[int] = None
    cost_date: Optional[date] = None
    cost_type: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    notes: Optional[str] = None

class CostRecordResponse(CostRecordBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True

class HarvestSaleBase(BaseModel):
    batch_id: int
    sale_date: date
    weight: Union[float, str]
    unit_price: Union[float, str]
    buyer: Optional[str] = None
    batch_number: Optional[str] = None
    quality_grade: Optional[str] = None
    notes: Optional[str] = None
    price_scale: Optional[int] = None


class HarvestSaleCreate(HarvestSaleBase):
    # 仅用于向后兼容地解析旧客户端字段；服务端绝不采信，金额由 pricing 统一计算。
    total_amount: Optional[Union[float, str]] = None


class HarvestSaleUpdate(BaseModel):
    # 重量/单价/精度/计价版本出现在这里仅为被服务层显式拦截并引导到更正接口，
    # 绝不允许经普通 PUT 修改。
    batch_id: Optional[int] = None
    sale_date: Optional[date] = None
    weight: Optional[Union[float, str]] = None
    unit_price: Optional[Union[float, str]] = None
    price_scale: Optional[int] = None
    pricing_version: Optional[str] = None
    buyer: Optional[str] = None
    batch_number: Optional[str] = None
    quality_grade: Optional[str] = None
    notes: Optional[str] = None
    total_amount: Optional[Union[float, str]] = None


class HarvestSaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: int
    sale_date: date
    weight: float
    unit_price: float
    total_amount: Optional[float] = None
    buyer: Optional[str] = None
    batch_number: Optional[str] = None
    quality_grade: Optional[str] = None
    notes: Optional[str] = None
    weight_raw: Optional[str] = None
    unit_price_raw: Optional[str] = None
    price_scale: Optional[int] = None
    pricing_version: Optional[str] = None
    status: str
    is_settled: bool = False
    supersedes_id: Optional[int] = None
    corrected_by_id: Optional[int] = None
    created_by_correction_id: Optional[int] = None
    created_at: datetime


class SaleCorrectionCreate(BaseModel):
    weight: Union[float, str]
    unit_price: Union[float, str]
    price_scale: Optional[int] = None
    reason: Optional[str] = None
    # 更正时可一并修正的非金额元数据（冲正方式下写入替代行）
    sale_date: Optional[date] = None
    buyer: Optional[str] = None
    batch_number: Optional[str] = None
    quality_grade: Optional[str] = None
    notes: Optional[str] = None
    # 仅为兼容旧客户端，忽略
    total_amount: Optional[Union[float, str]] = None


class SaleCorrectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    correction_id: str
    sale_id: int
    batch_id: int
    mode: str
    reason: Optional[str] = None
    old_weight: Optional[str] = None
    old_unit_price: Optional[str] = None
    old_price_scale: Optional[int] = None
    old_total_amount: Optional[float] = None
    old_pricing_version: Optional[str] = None
    new_weight: Optional[str] = None
    new_unit_price: Optional[str] = None
    new_price_scale: Optional[int] = None
    new_total_amount: Optional[float] = None
    new_pricing_version: Optional[str] = None
    reversed_sale_id: Optional[int] = None
    replacement_sale_id: Optional[int] = None
    replayed: bool = False
    created_at: datetime


class DataIssue(BaseModel):
    sale_id: int
    batch_id: int
    issue_type: str
    stored_amount: Optional[float] = None
    expected_amount: float
    price_scale: Optional[int] = None
    pricing_version: Optional[str] = None


class DataRepairReport(BaseModel):
    scanned: int
    issues: List[DataIssue] = []
    repaired: int = 0
    skipped_settled: int = 0
    repaired_sale_ids: List[int] = []
    settled_sale_ids: List[int] = []


class BatchSettlementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: int
    settlement_no: str
    cutoff_version: str
    total_revenue: float
    sale_count: int
    repaired_count: int
    status: str
    created_at: datetime


class BatchSettlementSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    settlement_no: str
    cutoff_version: str
    total_revenue: float
    sale_count: int
    repaired_count: int
    status: str
    created_at: datetime

class CostSummaryItem(BaseModel):
    type: str
    amount: float

class FeedingSummaryItem(BaseModel):
    feed_type: str
    total_quantity: float
    feeding_count: int

class CultureCycleAnalysis(BaseModel):
    batch_number: str
    pond_name: str
    species: str
    stocking_date: date
    harvest_date: Optional[date] = None
    days_cultured: Optional[int] = None
    initial_quantity: int
    harvest_weight: float
    survival_rate: float
    feed_total: float
    feed_conversion_ratio: float
    area: float
    yield_per_mu: float
    total_cost: float
    total_revenue: float
    profit: float
    revenue_version: str = "v1"
    sale_count: int = 0
    settled: bool = False
    settlement_no: Optional[str] = None
    cost_summary: Optional[dict] = None
    feeding_summary: Optional[dict] = None

class StockingRecordTrace(BaseModel):
    species: str
    quantity: int
    source: Optional[str] = None
    batch_number: Optional[str] = None
    stocking_date: Optional[date] = None

class FeedingRecordTrace(BaseModel):
    feeding_date: date
    feed_type: str
    quantity: float
    unit: Optional[str] = None

class WaterQualityRecordTrace(BaseModel):
    record_date: date
    water_temperature: Optional[float] = None
    ph_value: Optional[float] = None
    dissolved_oxygen: Optional[float] = None

class MedicationRecordTrace(BaseModel):
    medication_date: date
    medication_name: str
    dosage: Optional[float] = None
    unit: Optional[str] = None

class CostRecordTrace(BaseModel):
    cost_date: date
    cost_type: str
    amount: float
    description: Optional[str] = None

class HarvestSaleTrace(BaseModel):
    id: int
    sale_date: date
    weight: float
    unit_price: float
    total_amount: Optional[float] = None
    buyer: Optional[str] = None
    status: str = "active"
    price_scale: Optional[int] = None
    pricing_version: Optional[str] = None
    is_settled: bool = False
    supersedes_id: Optional[int] = None

class BatchInfo(BaseModel):
    batch_number: str
    species: str
    stocking_date: date
    harvest_date: Optional[date] = None
    status: str
    pond_id: Optional[int] = None

class PondInfo(BaseModel):
    name: Optional[str] = None
    area: Optional[float] = None
    water_depth: Optional[float] = None

class BatchTraceability(BaseModel):
    batch: BatchInfo
    pond_info: PondInfo
    stocking_records: List[StockingRecordTrace] = []
    feeding_records: List[FeedingRecordTrace] = []
    water_quality_records: List[WaterQualityRecordTrace] = []
    medication_records: List[MedicationRecordTrace] = []
    cost_records: List[CostRecordTrace] = []
    harvest_sales: List[HarvestSaleTrace] = []
    reversed_sales: List[HarvestSaleTrace] = []
    total_revenue: float = 0
    revenue_version: str = "v1"
    sale_count: int = 0
    settled: bool = False
    settlement_no: Optional[str] = None
