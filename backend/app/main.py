"""PrintAgent 数据服务 API（FastAPI 应用入口）。

【零基础说明：什么是 API / FastAPI？】
- API = 后端暴露给前端的一组"网址"（接口）。前端发 HTTP 请求到某个网址，
  后端返回 JSON 数据。比如前端要画曲线，就请求 /api/history/readings。
- FastAPI 是 Python 最流行的 Web 框架之一：写一个函数 + 装饰器，
  它自动处理路由、参数校验、生成文档（/docs 页面可以在线试调）。

【本文件职责】
1. 创建 app 对象（所有接口挂在它上面）
2. 挂 CORS 中间件：允许前端开发服务器（不同端口/域名）跨域访问
3. include 两个路由模块：数据接口（routes_data）+ 模型/闭环接口（routes_model）
4. 健康检查 /api/health

启动（backend 目录下）：
    .venv\\Scripts\\python -m app.main            # 默认 127.0.0.1:8000
    .venv\\Scripts\\python -m app.main --port 8000
启动后浏览器打开 http://127.0.0.1:8000/docs 可视化调试所有接口。
"""

import argparse

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes_chat import router as chat_router
from app.routes_data import router as data_router
from app.routes_model import router as model_router
from db.mysql import ping

app = FastAPI(
    title="PrintAgent API",
    version="0.3.0",
    description="印刷数据采集/质量/模型/控制 数据服务 + 印刷工艺智能体（RAG / Text2SQL / SSE）",
)

# CORS（跨源资源共享）：前端 vite 默认跑在 5173 端口，与后端 8000 不同源，
# 浏览器默认拦截这种跨源请求，这里声明允许（开发期放开 *，上线应收紧白名单）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 把路由模块的接口挂到 /api 前缀下
app.include_router(data_router, prefix="/api")
app.include_router(model_router, prefix="/api")
app.include_router(chat_router, prefix="/api")


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


@app.get("/api/health")
def health():
    """健康检查：容器编排/前端启动前都会先探这个接口。"""
    return {"status": "ok", "db": ping(), "time": datetime_now_iso()}


def main() -> None:
    ap = argparse.ArgumentParser(description="PrintAgent API 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    # uvicorn 是 ASGI 服务器，真正监听端口、处理 HTTP 的是它
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
