from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .database import engine, Base, WriteConflictError
from .migrations import ensure_schema
from .routers import ponds, batches, stocking, feeding, water_quality, medication, costs, harvest, analysis, fixes

# 新表建表 + 旧库列迁移与历史金额体检回填（幂等）
Base.metadata.create_all(bind=engine)
ensure_schema(engine)

app = FastAPI(
    title="水产养殖管理系统",
    description="支持塘口管理、投苗、投喂、水质、用药、成本、销售版本化更正、批次结算与周期分析",
    version="2.0.0"
)


@app.exception_handler(WriteConflictError)
async def write_conflict_handler(request: Request, exc: WriteConflictError):
    # 可重试冲突：客户端凭同一幂等键重试即可，不会重复生效
    return JSONResponse(status_code=409, content={"detail": str(exc), "retryable": True})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ponds.router)
app.include_router(batches.router)
app.include_router(stocking.router)
app.include_router(feeding.router)
app.include_router(water_quality.router)
app.include_router(medication.router)
app.include_router(costs.router)
app.include_router(harvest.router)
app.include_router(analysis.router)
app.include_router(fixes.router)

@app.get("/")
def root():
    return {
        "message": "欢迎使用水产养殖管理系统API",
        "docs": "/docs",
        "version": "2.0.0"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
