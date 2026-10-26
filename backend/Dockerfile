# 后端镜像：Python 3.12 + 依赖 + 源码（含知识库 PDF 与已训练模型）
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用 Docker 层缓存：requirements 不变时不再重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝源码（.dockerignore 已排除 __pycache__ 等杂物）
COPY . .

EXPOSE 8000

# 环境变量由 docker-compose 注入（MYSQL_HOST=simulator 等），.env 仅作本地兜底
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
