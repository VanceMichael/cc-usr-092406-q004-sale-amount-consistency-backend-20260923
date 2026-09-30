# q004 水产养殖服务

本项目是水产养殖管理后端，维护塘口、养殖批次、投苗、投喂、水质、用药、成本、销售与周期分析数据。业务数据保存在 SQLite 文件中，HTTP 接口由 FastAPI 提供。

## 测试命令

```bash
python3 -m unittest discover -s tests -v
```

## 编译与构建命令

```bash
python3 -m compileall -q backend/app
```

## 启动命令

```bash
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后可访问 `/health` 检查服务状态。开发环境不得提交真实账号、连接凭据或生产数据。

## 销售金额计算与更正规则

总金额只能由服务端按统一计价规则产生（`app/pricing.py`，当前版本 `v1`）：
`总金额 = ROUND_HALF_UP(原始重量 × 原始单价, 计价精度)`，全程 Decimal 精确运算。

- 销售创建/更新接口忽略调用方提交的 `total_amount`；普通 PUT 不允许改重量、单价、精度与金额，只能走更正接口。
- 每条销售保存可追溯事实：`weight_raw`、`unit_price_raw`、`price_scale`、`pricing_version`。
- 历史问题分批识别：`GET /api/harvest-sales/data-issues/`（`mismatch` 金额不一致 / `missing` 缺失 / `over_precision` 超出精度，支持 `batch_id/limit/offset`）。
- 未签署数据自动校正：`POST /api/harvest-sales/data-repair/`（单事务、分批、幂等）；已进入结算的行只报告不覆盖。
- 更正：`POST /api/harvest-sales/{id}/corrections/?correction_id=<幂等键>`。
  - 未签署：事务内原地校正并写审计；已签署：原行置 `reversed` 留痕 + 追加替代行，绝不原地覆盖。
  - 同一 `correction_id` 重复提交只生效一次（重放返回首次结果）；`GET /api/harvest-sales/corrections/{correction_id}/` 可查询原结果。
- 批次结算：`POST /api/batches/{id}/settlements/`，单事务内先修复该批未签署坏数据、再固化截止版本收入快照并签署全部有效销售；重复结算返回 409。
- 所有写事务以 `BEGIN IMMEDIATE` 取 SQLite 写锁，销售写入、结算、并发更正彼此串行化。
- 周期分析 `/api/analysis/cycle/`、追溯 `/api/analysis/traceability/` 与销售详情在同一截止版本（`revenue_version=v1`）下使用同一生效链收入口径，冲正行不计入收入。

