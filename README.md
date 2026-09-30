# q004 水产养殖服务

本项目是水产养殖管理后端，维护塘口、养殖批次、投苗、投喂、水质、用药、成本、销售与周期分析数据。业务数据保存在 SQLite 文件中，HTTP 接口由 FastAPI 提供。

## 销售金额一致性约定

总金额只能由服务端按统一规则产生，调用方提交的 `total_amount` 一律忽略：

```
总金额 = (重量 × 单价) 按 price_scale 位小数四舍五入（ROUND_HALF_UP，默认 2 位）
```

- 原始事实可追溯：`weight`、`unit_price`、`price_scale`、`amount_version`、`root_sale_id`、`effective_from`、`locked_version`。
- 重量/单价/总金额不允许 `PUT` 原地修改；计价更正走 `POST /api/harvest-sales/corrections/{sale_id}/`。
- 同一 `correction_id` 只生效一次；重复提交返回原结果（`replayed=true`），请求体冲突返回 409；响应丢失可 `GET /api/harvest-sales/corrections/{correction_id}/` 查询原结果。
- 未签署记录更正时自动校正；已进入结算的记录通过「冲正行（负）+ 替代行（新版本）」处理，原条目保留为 `reversed` 审计事实，绝不原地覆盖。
- 批次结算 `POST /api/batches/{batch_id}/settle/` 生成连续 `cutoff_version` 快照并锁定销售条目；结算与更正均在 `BEGIN IMMEDIATE` 单事务内完成，写锁冲突返回 409（`retryable=true`），凭幂等键重试。
- 历史坏数据通过 `POST /api/amount-fixes/` 分批识别（不一致 / 缺失金额 / 超出精度），未签署自动校正、已签署冲正替代，游标可续跑、可重复调用。
- 周期分析、批次追溯、销售列表/详情共用同一截止版本取数口径，同一 `cutoff_version` 下收入完全相同。

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
