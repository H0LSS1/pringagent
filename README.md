# PrintAgent — 印刷工艺智能体

以深圳博泰印刷智能化项目为背景的本地复现：**工业数据采集 + 压力预测闭环 + 印刷工艺智能体（RAG / Text2SQL）**。

## 架构

```
[模拟 PLC + 分光光度计] --Modbus TCP:5020--> [采集服务 poller] --> MySQL(print_agent, :3307)
        ^   |                                        |
        |   +-- 写设定寄存器（闭环下行）              +--> [FastAPI 数据服务 :8000] <--> [Vue3 前端]
        |                                            |
        +-- [压力预测模型 LinearRegression] <--训练数据--+
                                                     阶段三: + Milvus(:19530) + LangGraph 智能体 + SSE
```

## 目录

```
backend/
  config.py            # pydantic-settings 读取 .env
  register_map.py      # Modbus 寄存器地址映射（模拟器/采集端共用）
  simulator/           # Modbus TCP 模拟器 + 印刷物理模型（压力-色差联动, CIEDE2000）
  collector/           # 轮询采集（超时重传/断线重连/时序对齐/批量入库）+ 历史回填
  db/                  # SQLAlchemy ORM models（15 张表）+ 初始化种子
  models/              # 压力预测模型（线性回归 + joblib + model_registry）
  app/                 # FastAPI 数据服务（监控/历史/质量/模型/闭环控制）
frontend/              # Vue3 + Element Plus + ECharts（监控大屏/压力管理/智能体对话）
docker-compose.yml     # MySQL 3307 + Milvus 19530/9091（内嵌 etcd + 本地存储）
deploy/embedEtcd.yaml
```

## 快速启动

```powershell
# 0. 基础设施（MySQL + Milvus）
docker compose up -d

# 1. 建库 + 种子数据（backend 目录下）
.venv\Scripts\python -m db.init_db --groups 10

# 2. PLC / 分光光度计（常驻）该项目为模拟，非实机
.venv\Scripts\python -m simulator.modbus_server --groups 10

# 3a. 历史数据回填（约 11h 跨度、每表 20 万行；供模型训练与 Text2SQL 演示）
.venv\Scripts\python -m collector.backfill --cycles 20000

# 3b. 实时采集（常驻；或 --cycles 10 联调）
.venv\Scripts\python -m collector.main

# 4. 训练压力预测模型（线性回归, R²≈0.97）
.venv\Scripts\python -m models.pressure --limit 100000

# 5. 数据服务 API
.venv\Scripts\python -m app.main --port 8000

# 6. 前端（frontend 目录下）
npm run dev          # http://localhost:5173 ，/api 自动代理到 8000

## 前端页面

| 路径 | 页面 | 功能 |
|---|---|---|
| `/` | 监控大屏 | 统计卡片（车速/ΔE/超差数/采集成功率）、ΔE 与压力曲线、机组状态表、告警 |
| `/pressure` | 压力管理 | 模型版本与一键训练、闭环调节（含演练模式）、四模块实际/标准/设定对比柱图 |
| `/chat` | 工艺智能体 | SSE 流式对话：知识问答（条款溯源标签）、查数（SQL 折叠+结果表+ECharts） |
```

## API 一览（阶段二）

| 方法 | 路径                                                | 说明                                                            |
| ---- | --------------------------------------------------- | --------------------------------------------------------------- |
| GET  | /api/health                                         | 健康检查（含 DB ping）                                          |
| GET  | /api/devices                                        | 设备与机组列表                                                  |
| GET  | /api/dashboard/latest                               | 每机组最新工况 + 色彩质量（大屏数据源）                         |
| GET  | /api/history/readings?group_index=&minutes=&step_s= | 工况历史（分桶聚合，ECharts 直用）                              |
| GET  | /api/history/colors?group_index=&minutes=&step_s=   | ΔE/ΔD/合格率历史                                                |
| GET  | /api/quality/report?hours=                          | 质量报表（各机组 ΔE 统计/合格率）                               |
| GET  | /api/alarms?limit=                                  | 告警（ΔE 越限沿触发）                                           |
| GET  | /api/collect/status?hours=                          | 采集批次成功率/耗时                                             |
| POST | /api/model/train                                    | 训练压力模型（写入 model_registry）                             |
| GET  | /api/model/active                                   | 当前激活模型与指标                                              |
| POST | /api/control/adjust                                 | **闭环调节**：实时读 PLC/分光光度计 → 预测 → 写设定寄存器       |
| POST | /api/chat/stream                                    | **智能体对话（SSE 流式）**：意图路由→知识检索/Text2SQL→归因解读 |

## 智能体（阶段三）

```
[知识库 PDF + 人工条目] --rag/ingest--> Milvus(向量) + MySQL knowledge_chunks(原文)
                                             |
用户问题 -> 意图路由 -> 知识检索(BM25+向量 RRF 融合) -> 归因解读(带条款溯源) --\
         (LangGraph)                                                          +--> SSE 流式响应
                    -> Text2SQL(sqlglot 沙箱校验, 失败自动重试) -> 表格+ECharts --/
```

```powershell
# 知识库入库（一次即可；换文档后重跑）
.venv\Scripts\python -m rag.ingest          # --dry-run 预览

# 对话测试（或直接用前端聊天页）
curl.exe -s -N -X POST http://127.0.0.1:8000/api/chat/stream `
  -H "Content-Type: application/json" --data-binary "{\"message\": \"色差公差是多少\"}"
```

- LLM：智谱开放平台（OpenAI 兼容），`.env` 里 `LLM_MODEL` 可换
  `glm-4.7-flash`（默认，关思考快而稳）或 `glm-5.3-flash`（强制思考，代码自动适配）
- SSE 事件类型：`session / node / status / citations / sql / table / chart / answer_delta / answer / done`
- 长期记忆：会话按 session_id 存 MySQL，前端自动恢复最近会话，支持历史会话切换/新对话
- 回答为流式输出（answer_delta 逐段推送）

## 一键部署（Docker Compose + nginx，阶段六）

```powershell
docker compose up -d --build        # 全栈 7 服务，含 nginx(:8081)
```

部署完成浏览器打开 **http://localhost:8081**（8080 已被焊缝系统占用）。

- 包含 7 个服务：nginx(前端+反代) / backend / simulator / collector / initdb(一次性) / mysql / milvus
- 对外仅暴露 **8081**；数据存 docker 卷，重启不丢
- 端口语义：**3307 只是宿主机映射端口**（宿主机 venv 直连 MySQL 用）；
  容器之间走内部网络 3306，部署编排已用 `MYSQL_PORT=3306` 覆盖 `.env`
- nginx 已关闭 `/api` 缓冲（SSE 流式打字机正常）并放宽读超时
- 模型自愈：容器内找不到宿主机训练产物路径时，自动注册镜像内自带模型

## 评测（阶段五）

```powershell
#测评设置较少，详细请查看backend/eval/cases.py
.venv\Scripts\python -m eval.rag_eval    # RAG 检索命中率，实测 12/12 = 100%，
.venv\Scripts\python -m eval.sql_eval    # Text2SQL 成功率，实测 90%（目标 90%，simple 100%）
.venv\Scripts\python -m eval.e2e         # REST P95<=28ms + 智能体端到端时延
```

评测集在 `backend/eval/cases.py`（RAG 12 例 / SQL 10 例，含复杂聚合与 JOIN 口径）。
智能体端到端时延受 GLM 公共服务负载影响（429 限流时段 10~18s，良好时段约 7-9s）；
系统自身 REST 面 P95 ≤ 28ms，非瓶颈。
