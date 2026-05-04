#!/usr/bin/env python3
"""
CryptoQuant 策略自进化系统

从每周/每日复盘报告中自动提取：
1. 胜率 < 40% → 收紧信号条件 (提高 ADX 阈值, 收窄 RSI)
2. 单笔最大亏损过大 → 降低杠杆或收紧止损
3. LONG/SHORT 不对称 → 调整方向偏好权重
4. 外部信号过滤: 当 sentiment=bearish 时只做 SHORT, bullish 只做 LONG

用法: python3 strategy_evolver.py [--apply] [--dry-run]
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import glob
import re

# 因子 bias 集成
try:
    from services.factor_analysis import get_active_factor_bias
    _FACTOR_BIAS_AVAILABLE = True
except ImportError:
    _FACTOR_BIAS_AVAILABLE = False

PROJECT = Path(__file__).parent
STRATEGY_FILE = PROJECT / "strategies" / "spot" / "optimized_v6.py"
REVIEW_DIR = PROJECT / "data" / "reviews"


def load_latest_review() -> Optional[dict]:
    """加载最新的复盘报告"""
    reviews = sorted(REVIEW_DIR.glob("daily_review_*.json"))
    if not reviews:
        return None
    return json.loads(reviews[-1].read_text("utf-8"))


def load_strategy_params() -> dict:
    """从 optimized_v6.py 提取当前策略参数"""
    content = STRATEGY_FILE.read_text("utf-8")
    import re

    patterns = {
        "RSI_LONG_ENTRY": r'RSI_LONG_ENTRY\s*=\s*(\d+)',
        "RSI_LONG_MAX_ENTRY": r'RSI_LONG_MAX_ENTRY\s*=\s*(\d+)',
        "RSI_LONG_EXIT": r'RSI_LONG_EXIT\s*=\s*(\d+)',
        "RSI_SHORT_ENTRY": r'RSI_SHORT_ENTRY\s*=\s*(\d+)',
        "RSI_SHORT_MIN_ENTRY": r'RSI_SHORT_MIN_ENTRY\s*=\s*(\d+)',
        "RSI_SHORT_EXIT": r'RSI_SHORT_EXIT\s*=\s*(\d+)',
        "ADX_THRESHOLD": r'ADX_THRESHOLD\s*=\s*(\d+)',
        "ATR_STOP_LONG": r'ATR_STOP_LONG\s*=\s*([\d.]+)',
        "ATR_STOP_SHORT": r'ATR_STOP_SHORT\s*=\s*([\d.]+)',
        "MAX_POSITION_PCT": r'MAX_POSITION_PCT\s*=\s*([\d.]+)',
        "MACD_LONG_THRESHOLD": r'MACD_LONG_THRESHOLD\s*=\s*(\d+)',
        "MACD_SHORT_THRESHOLD": r'MACD_SHORT_THRESHOLD\s*=\s*(-?\d+)',
        "MAX_HOLD_BARS": r'MAX_HOLD_BARS\s*=\s*(\d+)',
        "MIN_HOLD_BARS": r'MIN_HOLD_BARS\s*=\s*(\d+)',
        "COOLDOWN_BARS": r'COOLDOWN_BARS\s*=\s*(\d+)',
    }
    params = {}
    for key, pat in patterns.items():
        m = re.search(pat, content)
        if m:
            params[key] = int(m.group(1)) if "." not in m.group(1) else float(m.group(1))

    # Provide defaults for missing params
    defaults = {
        "RSI_LONG_ENTRY": 35, "RSI_LONG_MAX_ENTRY": 65, "RSI_LONG_EXIT": 75,
        "RSI_SHORT_ENTRY": 55, "RSI_SHORT_MIN_ENTRY": 35, "RSI_SHORT_EXIT": 40,
        "ADX_THRESHOLD": 35, "ATR_STOP_LONG": 1.2, "ATR_STOP_SHORT": 1.5,
        "MAX_POSITION_PCT": 0.15,
        "MACD_LONG_THRESHOLD": 20, "MACD_SHORT_THRESHOLD": -20,
        "MAX_HOLD_BARS": 32, "MIN_HOLD_BARS": 4, "COOLDOWN_BARS": 6,
    }
    for k, v in defaults.items():
        params.setdefault(k, v)

    return params


def _load_previous_params() -> Optional[dict]:
    """加载最近一次进化后的参数快照，用于平滑限制"""
    snapshots = sorted(REVIEW_DIR.glob("params_evolved_*.json"))
    if len(snapshots) < 2:
        return None
    # 取倒数第二个（最近一次是当前已经应用的）
    return json.loads(snapshots[-2].read_text("utf-8")).get("params", {})


def _smooth_delta(param_name: str, new_val: float, current_val: float, max_delta_map: dict) -> float:
    """参数平滑：限制单次变化幅度不超过 max_delta
    - 如果新值与当前值的差值在阈值内，允许
    - 如果超出阈值，截断到阈值边界
    - 返回平滑后的值
    """
    max_delta = max_delta_map.get(param_name)
    if max_delta is None:
        return new_val
    delta = new_val - current_val
    if abs(delta) <= max_delta:
        return new_val
    return current_val + (max_delta if delta > 0 else -max_delta)


# 参数平滑: 单次变化上限（防止相邻两天跳变过大）
SMOOTH_LIMITS = {
    "RSI_LONG_ENTRY": 3,   # RSI entry 最多 ±3
    "RSI_LONG_MAX_ENTRY": 3,
    "RSI_LONG_EXIT": 5,    # RSI exit 最多 ±5
    "RSI_SHORT_ENTRY": 3,
    "RSI_SHORT_MIN_ENTRY": 3,
    "RSI_SHORT_EXIT": 5,
    "ADX_THRESHOLD": 5,    # ADX 最多 ±5
    "ATR_STOP_LONG": 0.4,  # ATR 倍数最多 ±0.4
    "ATR_STOP_SHORT": 0.4,
    "MAX_POSITION_PCT": 0.05,  # 仓位最多 ±5%
}


def compute_evolved_params(review: dict, current: dict) -> dict:
    """根据复盘报告计算优化后的参数"""
    evolved = dict(current)
    s = review.get("summary", {})
    l = review.get("loss_analysis", {})
    d = review.get("direction", {})

    win_rate = s.get("win_rate", 50)
    max_loss = l.get("max_loss", 0)
    total_trades = s.get("total_trades", 0)
    consecutive = l.get("consecutive_losses", 0)

    changes = []

    # Rule 1: 胜率太低 → 收紧 ADX, RSI entry 阈值
    if total_trades >= 5 and win_rate < 40:
        evolved["ADX_THRESHOLD"] = min(evolved.get("ADX_THRESHOLD", 18) + 5, 28)
        evolved["RSI_LONG_ENTRY"] = evolved.get("RSI_LONG_ENTRY", 40) - 5
        evolved["RSI_LONG_EXIT"] = evolved.get("RSI_LONG_EXIT", 75) + 5
        evolved["RSI_SHORT_ENTRY"] = evolved.get("RSI_SHORT_ENTRY", 35) - 5
        evolved["RSI_SHORT_EXIT"] = evolved.get("RSI_SHORT_EXIT", 25) + 5
        changes.append(f"ADX+5 → {evolved['ADX_THRESHOLD']}, RSI收窄")

    # Rule 2: 大额亏损 → 收紧ATR止损
    if abs(max_loss) > 50:
        evolved["ATR_STOP_LONG"] = max(evolved.get("ATR_STOP_LONG", 2.0) - 0.4, 1.2)
        evolved["ATR_STOP_SHORT"] = max(evolved.get("ATR_STOP_SHORT", 2.5) - 0.4, 1.5)
        evolved["MAX_POSITION_PCT"] = max(evolved.get("MAX_POSITION_PCT", 0.30) - 0.05, 0.15)
        changes.append(f"ATR止损收紧, MAX_POSITION→{evolved['MAX_POSITION_PCT']:.0%}")

    # Rule 3: LONG 连续亏 → 更难触发
    long_data = d.get("long", {})
    if long_data.get("count", 0) >= 3 and long_data.get("pnl", 0) < -30:
        evolved["RSI_LONG_ENTRY"] = max(evolved.get("RSI_LONG_ENTRY", 40) - 3, 25)
        evolved["RSI_LONG_EXIT"] = min(evolved.get("RSI_LONG_EXIT", 75) - 5, 85)
        changes.append("LONG亏损偏多，降低触发 & 更早退出")

    # Rule 4: SHORT 连续亏
    short_data = d.get("short", {})
    if short_data.get("count", 0) >= 3 and short_data.get("pnl", 0) < -30:
        evolved["RSI_SHORT_ENTRY"] = min(evolved.get("RSI_SHORT_ENTRY", 35) + 3, 50)
        evolved["RSI_SHORT_EXIT"] = max(evolved.get("RSI_SHORT_EXIT", 25) + 5, 40)
        changes.append("SHORT亏损偏多，调高门槛")

    # Rule 5: 因子 bias — 来自 factor_analysis.py 的外部信号
    if _FACTOR_BIAS_AVAILABLE:
        try:
            bias = get_active_factor_bias()
            if bias["confidence"] > 0.3 and bias["bias"] != "neutral":
                if bias["bias"] == "long_bias":
                    # 因子看好做多 → 放宽 LONG entry, 收紧 SHORT entry
                    evolved["RSI_LONG_ENTRY"] = max(evolved.get("RSI_LONG_ENTRY", 40) - 5, 25)
                    evolved["RSI_SHORT_ENTRY"] = min(evolved.get("RSI_SHORT_ENTRY", 35) + 5, 50)
                    changes.append(f"因子long_bias(置信{bias['confidence']}): 放宽LONG触发, 收紧SHORT")
                elif bias["bias"] == "short_bias":
                    # 因子看好做空 → 放宽 SHORT entry, 收紧 LONG entry
                    evolved["RSI_SHORT_ENTRY"] = min(evolved.get("RSI_SHORT_ENTRY", 35) + 5, 50)
                    evolved["RSI_LONG_ENTRY"] = max(evolved.get("RSI_LONG_ENTRY", 40) + 5, 30)
                    changes.append(f"因子short_bias(置信{bias['confidence']}): 放宽SHORT触发, 收紧LONG")
        except Exception as e:
            changes.append(f"因子bias获取失败: {e}")

    # === 参数平滑: 限制单次变化幅度 ===
    for key in SMOOTH_LIMITS:
        if key in evolved and key in current:
            evolved[key] = _smooth_delta(key, evolved[key], current[key], SMOOTH_LIMITS)

    # Clamp
    evolved["RSI_LONG_ENTRY"] = max(min(evolved.get("RSI_LONG_ENTRY", 48), 55), 20)
    evolved["RSI_LONG_MAX_ENTRY"] = max(min(evolved.get("RSI_LONG_MAX_ENTRY", 65), 75), 50)
    evolved["RSI_LONG_EXIT"] = max(min(evolved.get("RSI_LONG_EXIT", 72), 90), 55)
    evolved["RSI_SHORT_ENTRY"] = max(min(evolved.get("RSI_SHORT_ENTRY", 50), 65), 30)
    evolved["RSI_SHORT_MIN_ENTRY"] = max(min(evolved.get("RSI_SHORT_MIN_ENTRY", 35), 50), 20)
    evolved["RSI_SHORT_EXIT"] = max(min(evolved.get("RSI_SHORT_EXIT", 45), 55), 20)
    evolved["ADX_THRESHOLD"] = max(min(evolved.get("ADX_THRESHOLD", 23), 35), 12)

    evolved["_changes"] = changes
    evolved["_evolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    evolved["_evolved_from_review"] = review.get("date", "unknown")

    return evolved


def restart_engine() -> bool:
    """重启 crypto-quant.service (systemd user)"""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "crypto-quant.service"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return True
        print(f"⚠️ 引擎重启失败: {result.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        print("⚠️ 引擎重启超时")
        return False
    except FileNotFoundError:
        print("⚠️ systemctl 不可用，请手动重启: systemctl --user restart crypto-quant.service")
        return False


def apply_params(new_params: dict, auto_restart: bool = True) -> bool:
    """将新参数写入 strategy 文件 (模块级常量), 可选自动重启引擎"""
    content = STRATEGY_FILE.read_text("utf-8")

    param_map = {
        "RSI_LONG_ENTRY": "RSI_LONG_ENTRY",
        "RSI_LONG_MAX_ENTRY": "RSI_LONG_MAX_ENTRY",
        "RSI_LONG_EXIT": "RSI_LONG_EXIT",
        "RSI_SHORT_ENTRY": "RSI_SHORT_ENTRY",
        "RSI_SHORT_MIN_ENTRY": "RSI_SHORT_MIN_ENTRY",
        "RSI_SHORT_EXIT": "RSI_SHORT_EXIT",
        "ADX_THRESHOLD": "ADX_THRESHOLD",
        "ATR_STOP_LONG": "ATR_STOP_LONG",
        "ATR_STOP_SHORT": "ATR_STOP_SHORT",
        "MAX_POSITION_PCT": "MAX_POSITION_PCT",
        "MACD_LONG_THRESHOLD": "MACD_LONG_THRESHOLD",
        "MACD_SHORT_THRESHOLD": "MACD_SHORT_THRESHOLD",
        "MAX_HOLD_BARS": "MAX_HOLD_BARS",
        "MIN_HOLD_BARS": "MIN_HOLD_BARS",
        "COOLDOWN_BARS": "COOLDOWN_BARS",
    }

    import re
    changes_made = 0
    for key, var_name in param_map.items():
        if key in new_params:
            val = new_params[key]
            pattern = rf'({var_name}\s*=\s*)(-?\d+\.?\d*)'
            replacement = rf'\g<1>{val}'
            new_content, n = re.subn(pattern, replacement, content)
            if n > 0:
                content = new_content
                changes_made += n

    if changes_made > 0:
        # Write back
        STRATEGY_FILE.write_text(content, "utf-8")
        # Also write params snapshot
        params_snapshot = {
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "params": {k: v for k, v in new_params.items() if not k.startswith("_")},
            "changes": new_params.get("_changes", []),
        }
        snapshot_path = PROJECT / "data" / "reviews" / f"params_evolved_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        snapshot_path.write_text(json.dumps(params_snapshot, ensure_ascii=False, indent=2), "utf-8")

        # 自动重启引擎加载新参数
        if auto_restart:
            print("🔄 正在重启交易引擎...")
            if restart_engine():
                print("✅ 引擎已重启，新策略参数已生效")
            else:
                print("⚠️ 自动重启失败，请手动执行: systemctl --user restart crypto-quant.service")
        return True

    return False


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    apply = "--apply" in sys.argv

    review = load_latest_review()
    if not review:
        print("❌ 无复盘报告，请先运行 daily_review.py")
        sys.exit(1)

    current = load_strategy_params()
    evolved = compute_evolved_params(review, current)

    print("📊 CryptoQuant 策略自进化")
    print(f"   复盘日期: {review.get('date')}")
    print(f"   胜率: {review['summary']['win_rate']}% | 交易: {review['summary']['total_trades']}次")
    print()
    print("   当前参数:", json.dumps({k: v for k, v in current.items()}, indent=2))
    print()
    print("   进化后参数:", json.dumps({k: v for k, v in evolved.items() if not k.startswith("_")}, indent=2))
    print()

    changes = evolved.get("_changes", [])
    if changes:
        print("📝 变更:")
        for c in changes:
            print(f"   • {c}")
    else:
        print("✅ 无需调整 — 当前参数已是最优")

    if apply:
        if apply_params(evolved):
            print()
            print("✅ 策略参数已更新到 optimized_v6.py")
        else:
            print("⚠️ 参数写入失败（可能参数名不匹配）")
    elif not dry_run:
        print()
        print("💡 加 --apply 应用变更，加 --dry-run 仅预览")