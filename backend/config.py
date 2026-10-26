"""全局配置文件（所有后端进程共用）。

【零基础说明】
- 一个项目里会有很多"魔法字符串"：数据库密码、API 地址、端口号……
  把它们统一放到 backend/.env 文件里（不进 Git、不进代码），改配置不用改代码。
- pydantic-settings 做的事：启动时自动读取 .env，把每一行 `KEY=value`
  映射到下面同名的类属性（大小写不敏感：.env 写 MYSQL_HOST，代码用 mysql_host）。
- 【重要】所有进程（模拟器/采集服务/API/智能体）都必须在 backend 目录下启动，
  因为 env_file=".env" 是相对"当前工作目录"找文件的。

谁调用它：backend 下几乎每个模块都 `from config import settings` 拿配置。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_file：指定要读取的配置文件；extra="ignore"：.env 里多出来的配置项不报错
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ===== LLM / Embedding 配置（阶段三智能体使用） =====
    # llm_*：对话大模型（走智谱开放平台的 OpenAI 兼容接口）
    # embedding_*：文本向量化模型，RAG 检索时把文字变成一串数字（向量）再比相似度
    llm_api_key: str = ""
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_model: str = "glm-4.7-flash"
    embedding_model: str = "embedding-3"
    # 向量维度必须与 Milvus 建集合时的 dim 一致，否则写入/检索会报错
    embedding_dim: int = 1024

    # ===== MySQL 配置（阶段二采集数据都存在这里） =====
    # 与 docker-compose.yml 中的 printagent-mysql 对应：宿主机端口 3307 -> 容器 3306
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_user: str = "root"
    mysql_password: str = "printagent"
    mysql_db: str = "print_agent"

    # ===== Milvus 配置（阶段三 RAG 向量数据库） =====
    # 19530 是 Milvus 的 gRPC 服务端口，pymilvus 客户端连这个
    milvus_uri: str = "http://localhost:19530"

    # ===== Modbus TCP 配置（阶段二工业采集） =====
    # Modbus 是工业设备间最常用的通信协议之一：一台"客户端"（我们采集服务）
    # 向一台"服务端"（PLC 设备）按"寄存器地址"读写数值。
    # 默认 5020 是我们模拟器监听的端口（真实设备通常是 502）。
    modbus_host: str = "127.0.0.1"
    modbus_port: int = 5020
    # 轮询周期：每隔多少秒把所有点位读一遍（现场一般 1~5 秒）
    poll_interval_s: float = 2.0

    @property
    def mysql_url(self) -> str:
        """拼出 SQLAlchemy 需要的连接串。

        格式：mysql+pymysql://用户名:密码@主机:端口/库名?charset=utf8mb4
        - mysql+pymysql 表示用 pymysql 这个驱动去连
        - utf8mb4 是 MySQL 的完整 UTF-8 编码（支持 emoji 和全部中文）
        """
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )


# 模块级单例：其他模块 `from config import settings` 直接用，全局只有这一份配置
settings = Settings()
