#!/usr/bin/env python3
"""
LightGBM 滚动训练脚本 — 定期拉取最新数据 + 重训练 + 保存模型

用法:
  python3 scripts/retrain_lgb.py           # 立即重训练
  python3 scripts/retrain_lgb.py --days 60 # 用60天数据

Cron 集成:
  0 4 * * * cd /home/ni/crypto_quant && python3 scripts/retrain_lgb.py

  每周日凌晨 4:00 自动拉新数据重训练。
"""

import sys, logging
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from scripts.extract_15m_data import fetch_klines_paginated, add_labels
from data.alpha_factors import AlphaFactors
from ml.lgb_predictor import LGBPredictor, LGBAdapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("retrain")


def main():
    days = 30
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])

    logger.info(f"📡 拉取最新 {days} 天 15m K 线...")
    df = fetch_klines_paginated(days)
    if df.empty:
        logger.error("❌ 数据拉取失败")
        sys.exit(1)

    logger.info(f"📊 {len(df):,} 条 K 线")

    # 添加标签
    df = add_labels(df, horizons=[4, 8, 24])

    # 计算 Alpha 因子
    logger.info("🧮 计算 Alpha 因子...")
    af = AlphaFactors()
    df = af.compute(df)

    # 删除前 120 行 + NaN
    df_clean = df.iloc[120:].dropna(
        subset=[c for c in df.columns if c.startswith("alpha_") and "RESI" not in c] + ["label_dir_4", "label_dir_8", "label_dir_24"]
    )
    logger.info(f"🎯 训练样本: {len(df_clean):,} 行")

    # 训练三个周期的模型
    horizons = [4, 8, 24]
    models = {}
    for h in horizons:
        logger.info(f"🧠 训练 h={h} 分类器...")
        predictor = LGBPredictor(horizon=h, model_type="classifier")
        metrics = predictor.train(df_clean, label_col=f"label_dir_{h}")
        models[h] = predictor
        predictor.save()

        auc = predictor.metrics.get("auc", 0)
        acc = predictor.metrics.get("accuracy", 0)
        logger.info(f"  ✅ h={h}: AUC={auc:.3f} Acc={acc:.1%}")

    # 保存训练元数据
    import json
    from datetime import datetime, timezone

    meta = {
        "retrained_at": datetime.now(timezone.utc).isoformat(),
        "data_range": f"{pd.to_datetime(df_clean['open_time'].min(), unit='ms')} ~ {pd.to_datetime(df_clean['open_time'].max(), unit='ms')}",
        "samples": len(df_clean),
        "days": days,
        "models": {
            str(h): {
                "auc": models[h].metrics.get("auc", 0),
                "accuracy": models[h].metrics.get("accuracy", 0),
                "precision": models[h].metrics.get("precision", 0),
            }
            for h in horizons
        },
        "top_features": {
            str(h): list(models[h].feature_importance.items())[:5]
            for h in horizons
        },
    }

    meta_path = PROJECT / "data" / "models" / "training_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    logger.info(f"💾 元数据已保存: {meta_path}")

    # 验证模型可加载
    adapter = LGBAdapter(horizon=24)
    if adapter.is_loaded():
        logger.info("✅ 模型验证通过 — h=24 分类器已就绪")


if __name__ == "__main__":
    main()