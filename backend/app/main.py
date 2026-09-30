from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .database import engine, Base
from .migrations import ensure_schema
from .pricing import PricingError
from .routers import ponds, batches, stocking, feeding, water_quality, medication, costs, harvest, analysis, settlements

# 幂等建表/补列：新库直接建全量结构，旧库补齐事实列并回填遗留版本标记。
ensure_schema(Base, engine)

app = FastAPI(
    title="水产养殖管理系统",
    description="一个完整的水产养殖管理系统，支持塘口管理、投苗记录、日常管理、成本核算、出塘销售和养殖周期分析",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ponds.router)
app.include_router(batches.router)
app.include_router(settlements.router)
app.include_router(stocking.router)
app.include_router(feeding.router)
app.include_router(water_quality.router)
app.include_router(medication.router)
app.include_router(costs.router)
app.include_router(harvest.router)
app.include_router(analysis.router)

@app.exception_handler(PricingError)
def pricing_error_handler(request: Request, exc: PricingError):
    # 计价事实非法属于客户端输入错误，统一返回 400 且不产生任何写入。
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def root():
    return {
        "message": "欢迎使用水产养殖管理系统API",
        "docs": "/docs",
        "version": "1.1.0"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
