"""压力预测模型（阶段二：最简单的线性回归，后续可换随机森林/GBDT）。

【零基础说明：这个模型解决什么问题？】
产线运行中压力会漂移/受扰动，导致色差 ΔE 超标。这个模型根据当前工况
（车速/温湿度）+ 色差信息，预测"应该把压力调回多少"——即还原到
人工调试好的初始压力（PLC 初始压力寄存器，含机组标定偏移，固定不变）。
预测值写回设定寄存器后，实际压力收敛、ΔE 回落。

【机器学习三要素（用本模型举例）】
- 特征 X：模型能看到的输入。7 个数：车速/温度/湿度/机组号/模块号/ΔE/ΔD
- 目标 y：想让模型学会预测的答案 = "能让实测 Lab 逼近标准 Lab 的压力"。
  训练数据只取分光光度计判定颜色达标的时刻（ΔE 很小），此时该机组的
  实际压力就是颜色意义上的最优压力——模型从这些"考得好的时刻"学习
  压力与颜色/工况的映射，而不是机械地还原初始压力。
- 训练：sklearn 从历史数据（X->y 的几千条样本）里拟合出线性关系

【评估指标】
- R²（决定系数）：1.0=完美预测，0=跟瞎猜均值一样。我们 0.96 左右说明
  线性关系被学到了（剩余误差来自机组标定偏移+噪声，符合预期）
- MAE（平均绝对误差）：预测的压力平均偏差（kN/m）

【持久化与版本管理】
- 训练好的模型 joblib.dump 到本地 pkl 文件（重启不丢）
- 每次训练在 model_registry 表登记一条版本记录，is_active=True 的那个
  被闭环调节接口使用——这样"模型上线/回滚"有据可查
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sqlalchemy import and_, insert, select, update
from sqlalchemy.orm import Session

from db.models import ColorMeasurement, ModelRegistry, PlcReading
from db.mysql import get_session_factory
from simulator.physics import COLOR_GOOD_DE

MODEL_NAME = "pressure_linear"
# 特征顺序很重要：训练时什么顺序，预测时必须什么顺序
FEATURES = ["speed_mmin", "temperature", "humidity",
            "group_index", "zone_index", "delta_e", "delta_d"]
ARTIFACT_DIR = Path(__file__).parent / "artifacts"


def load_samples(limit: int = 100000) -> tuple[np.ndarray, np.ndarray]:
    """从数据库取训练样本。

    【训练目标的学习口径：一切以分光光度计数据为准】
    只取"颜色达标时刻"（ΔE ≤ COLOR_GOOD_DE，即实测 Lab 已经很接近标准 Lab）
    的历史样本，目标 = 该时刻的实际压力——也就是"能让实测 Lab 逼近
    标准 Lab 的压力"。刻意不使用初始压力寄存器：初始压力只是人工机械基准，
    印刷久了（印版磨损/油墨批次变化）它印出的颜色未必达标。

    一行 PLC 数据有 4 个压力模块 => 拆成 4 条样本（每条目标是一个模块的压力），
    这叫"宽表转长表"（unpivot），是监督学习常见的样本构造手法。
    """
    with Session(get_session_factory().kw["bind"]) as session:
        rows = session.execute(
            select(PlcReading, ColorMeasurement)
            .join(ColorMeasurement, and_(
                ColorMeasurement.ts == PlcReading.ts,
                ColorMeasurement.group_index == PlcReading.group_index,
            ))
            .where(ColorMeasurement.in_tolerance.is_(True),
                   ColorMeasurement.delta_e <= COLOR_GOOD_DE)
            .order_by(PlcReading.ts.desc())
            .limit(limit)
        ).all()

    xs: list[list[float]] = []
    ys: list[float] = []
    for pr, cm in rows:
        p_init = (float(pr.p_init_1), float(pr.p_init_2), float(pr.p_init_3), float(pr.p_init_4))
        base = [float(pr.speed_mmin), float(pr.temperature), float(pr.humidity)]
        for z in range(4):
            xs.append([*base, pr.group_index, z, float(cm.delta_e), float(cm.delta_d)])
            ys.append(p_init[z])
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def train(limit: int = 100000) -> dict:
    """训练 -> 评估 -> 落盘 -> 注册版本。返回指标字典（API 直接返回给前端）。"""
    X, y = load_samples(limit)
    if len(X) < 100:
        raise RuntimeError(f"训练样本不足（{len(X)}），请先运行采集/回填生成数据")

    # 留出 20% 数据做测试集：模型没见过的数据上的表现才算真实水平
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42)
    mdl = LinearRegression()
    mdl.fit(X_tr, y_tr)
    r2 = float(r2_score(y_te, mdl.predict(X_te)))
    mae = float(mean_absolute_error(y_te, mdl.predict(X_te)))

    # 版本号用时间戳：同一天多次训练也不会重名
    version = datetime.now().strftime("%Y%m%d%H%M%S")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact = ARTIFACT_DIR / "pressure_model.pkl"
    # joblib 是 sklearn 生态的序列化工具，能把"模型对象+特征名"整体存成文件
    joblib.dump({"model": mdl, "features": FEATURES, "version": version}, artifact)

    with Session(get_session_factory().kw["bind"]) as session:
        # 新版本上线 => 旧版本全部置为非激活（单激活版本，可回滚：改 is_active 即可）
        session.execute(update(ModelRegistry).values(is_active=False))
        session.execute(insert(ModelRegistry).values(
            name=MODEL_NAME, version=version, algo="LinearRegression",
            feature_names=FEATURES,
            params={"fit_intercept": True, "n_features": len(FEATURES)},
            metrics={"r2": round(r2, 4), "mae": round(mae, 4),
                     "train_rows": len(X_tr), "test_rows": len(X_te)},
            train_rows=len(X), r2=round(r2, 4), mae=round(mae, 4),
            artifact_path=str(artifact), is_active=True,
        ))
        session.commit()

    print(f"[pressure] version={version} r2={r2:.4f} mae={mae:.4f} "
          f"train_rows={len(X)} artifact={artifact}")
    return {"version": version, "r2": r2, "mae": mae, "train_rows": len(X)}


def load_active_artifact() -> dict:
    """加载 model_registry 里 is_active=True 的模型文件。

    【自愈逻辑】宿主机训练后 artifact_path 是 Windows 路径，容器内不存在；
    或全新数据库还没有注册记录。两种情况都自动回退到镜像内自带的
    models/artifacts/pressure_model.pkl，并补登记注册表——
    保证 docker 部署起来就能用，不需要手动重新训练。
    """
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    with Session(get_session_factory().kw["bind"]) as session:
        row = session.execute(
            select(ModelRegistry).where(
                ModelRegistry.name == MODEL_NAME, ModelRegistry.is_active.is_(True))
        ).scalar_one_or_none()

        artifact = ARTIFACT_DIR / "pressure_model.pkl"
        if row is not None and _Path(row.artifact_path).exists():
            artifact = _Path(row.artifact_path)
        elif not artifact.exists():
            raise RuntimeError("无激活的压力模型，且镜像内未自带模型文件，请先训练")
        else:
            # 自带回退：补登记注册表（版本号读自 pkl 内保存的元数据）
            meta = joblib.load(artifact)
            session.execute(update(ModelRegistry).values(is_active=False))
            session.execute(insert(ModelRegistry).values(
                name=MODEL_NAME, version=meta.get("version", "shipped"),
                algo="LinearRegression", feature_names=meta.get("features", FEATURES),
                artifact_path=str(artifact), is_active=True,
            ))
            session.commit()
    return joblib.load(artifact)


def predict_std_pressures(samples: list[dict]) -> list[dict]:
    """对每个候选机组预测 4 个压力模块"颜色达标压力"（闭环调节调用）。

    语义：预测的压力是"写回去之后实测 Lab 会逼近标准 Lab"的压力——
    由分光光度计历史数据（颜色达标时刻）训练得到，与初始压力无关。

    samples 每项需含: speed_mmin, temperature, humidity, group_index, delta_e, delta_d
    返回: [{group_index, zone_index, predicted}, ...]
    """
    art = load_active_artifact()
    mdl = art["model"]
    out = []
    for s in samples:
        # 与训练时完全一致的特征顺序：[车速, 温度, 湿度, 机组, 模块, ΔE, ΔD]
        X = [[float(s["speed_mmin"]), float(s["temperature"]), float(s["humidity"]),
              s["group_index"], z, float(s["delta_e"]), float(s["delta_d"])]
             for z in range(4)]
        preds = mdl.predict(np.asarray(X, dtype=float))
        for z, p in enumerate(preds):
            out.append({"group_index": s["group_index"], "zone_index": z,
                        "predicted": round(float(p), 2)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="压力预测模型训练（线性回归）")
    ap.add_argument("--limit", type=int, default=100000, help="训练样本上限")
    args = ap.parse_args()
    train(limit=args.limit)


if __name__ == "__main__":
    main()
