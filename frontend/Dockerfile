# 前端镜像：多阶段构建——先用 node 编译出静态文件，再用 nginx 托管并反代 API
# node 用 20-alpine：本机已缓存该镜像（离线可用）；npm engines 仅告警不阻断
FROM node:20-alpine AS build
WORKDIR /app

# 先装依赖（层缓存：package.json 不变时跳过 npm ci）
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---- 运行阶段 ----
FROM nginx:alpine

# 反代配置：静态文件 + /api 转发后端（SSE 关闭缓冲）
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
