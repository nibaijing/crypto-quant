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
        "MAX_POSITION_PCT": 1.0,
        "MACD_LONG_THRESHOLD": 20, "MACD_SHORT_THRESHOLD": -20,
        "MAX_HOLD_BARS": 32, "MIN_HOLD_BARS": 4,
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
    "MACD_LONG_THRESHOLD": 5,   # MACD阈值最多 ±5
    "MACD_SHORT_THRESHOLD": 5,
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

    # Rule 1: 胜率太低 → 提高 ADX (更严格的趋势过滤)
    if total_trades >= 5 and win_rate < 40:
        evolved["ADX_THRESHOLD"] = min(evolved.get("ADX_THRESHOLD", 35) + 5, 45)
        changes.append(f"ADX+5 → {evolved['ADX_THRESHOLD']} (提高趋势门槛)")

    # Rule 2: 大额亏损 → 收紧ATR止损
    if abs(max_loss) > 50:
        evolved["ATR_STOP_LONG"] = max(evolved.get("ATR_STOP_LONG", 2.0) - 0.4, 1.2)
        evolved["ATR_STOP_SHORT"] = max(evolved.get("ATR_STOP_SHORT", 2.5) - 0.4, 1.5)
        evolved["MAX_POSITION_PCT"] = max(evolved.get("MAX_POSITION_PCT", 0.30) - 0.05, 0.15)
        changes.append(f"ATR止损收紧, MAX_POSITION→{evolved['MAX_POSITION_PCT']:.0%}")

    # Rule 3: LONG 连续亏 → 更严格过滤 (提高 ADX，收紧 MACD)
    long_data = d.get("long", {})
    if long_data.get("count", 0) >= 3 and long_data.get("pnl", 0) < -30:
        evolved["ADX_THRESHOLD"] = min(evolved.get("ADX_THRESHOLD", 35) + 3, 45)
        evolved["MACD_LONG_THRESHOLD"] = min(evolved.get("MACD_LONG_THRESHOLD", 20) + 5, 35)
        changes.append("LONG亏损偏多，提高ADX+MACD门槛")

    # Rule 4: SHORT 连续亏 → 更严格过滤
    short_data = d.get("short", {})
    if short_data.get("count", 0) >= 3 and short_data.get("pnl", 0) < -30:
        evolved["ADX_THRESHOLD"] = min(evolved.get("ADX_THRESHOLD", 35) + 3, 45)
        evolved["MACD_SHORT_THRESHOLD"] = max(evolved.get("MACD_SHORT_THRESHOLD", -20) - 5, -35)
        changes.append("SHORT亏损偏多，提高ADX+MACD门槛")

    # Rule 5: 因子 bias — 调整信号阈值以顺应/抵御市场偏向
    # short_bias (市场看空): 收紧LONG门槛 + 略微放宽SHORT门槛
    # long_bias  (市场看多): 收紧SHORT门槛 + 略微放宽LONG门槛
    # 仅在置信度 > 0.5 时介入，避免噪声导致阈值漂移
    if _FACTOR_BIAS_AVAILABLE:
        try:
            bias = get_active_factor_bias()
            if bias["confidence"] > 0.5 and bias["bias"] != "neutral":
                if bias["bias"] == "short_bias":
                    # 顺应做空: 放宽SHORT, 收紧LONG
                    evolved["RSI_SHORT_ENTRY"] = max(
                        evolved.get("RSI_SHORT_ENTRY", 55) - 3, 45)
                    evolved["MACD_SHORT_THRESHOLD"] = min(
                        evolved.get("MACD_SHORT_THRESHOLD", -15) + 5, -5)
                    evolved["RSI_LONG_ENTRY"] = min(
                        evolved.get("RSI_LONG_ENTRY", 35) + 3, 45)
                    evolved["MACD_LONG_THRESHOLD"] = max(
                        evolved.get("MACD_LONG_THRESHOLD", 15) + 5, 30)
                    changes.append(
                        f"因子short_bias(置信{bias['confidence']}): "
                        f"顺应做空 — 放宽SHORT/收紧LONG阈值")
                elif bias["bias"] == "long_bias":
                    # 顺应做多: 放宽LONG, 收紧SHORT
                    evolved["RSI_LONG_ENTRY"] = max(
                        evolved.get("RSI_LONG_ENTRY", 35) - 3, 25)
                    evolved["MACD_LONG_THRESHOLD"] = max(
                        evolved.get("MACD_LONG_THRESHOLD", 15) - 5, 5)
                    evolved["RSI_SHORT_ENTRY"] = min(
                        evolved.get("RSI_SHORT_ENTRY", 55) + 3, 65)
                    evolved["MACD_SHORT_THRESHOLD"] = min(
                        evolved.get("MACD_SHORT_THRESHOLD", -15) - 5, -25)
                    changes.append(
                        f"因子long_bias(置信{bias['confidence']}): "
                        f"顺应做多 — 放宽LONG/收紧SHORT阈值")
        except Exception as e:
            changes.append(f"因子bias获取失败: {e}")

    # === 参数平滑: 限制单次变化幅度 ===
    for key in SMOOTH_LIMITS:
        if key in evolved and key in current:
            evolved[key] = _smooth_delta(key, evolved[key], current[key], SMOOTH_LIMITS)

    # Clamp
    evolved["RSI_LONG_ENTRY"] = max(min(evolved.get("RSI_LONG_ENTRY", 35), 45), 20)
    evolved["RSI_LONG_MAX_ENTRY"] = max(min(evolved.get("RSI_LONG_MAX_ENTRY", 65), 75), 50)
    evolved["RSI_LONG_EXIT"] = max(min(evolved.get("RSI_LONG_EXIT", 75), 85), 65)
    evolved["RSI_SHORT_ENTRY"] = max(min(evolved.get("RSI_SHORT_ENTRY", 55), 65), 40)
    evolved["RSI_SHORT_MIN_ENTRY"] = max(min(evolved.get("RSI_SHORT_MIN_ENTRY", 35), 45), 20)
    evolved["RSI_SHORT_EXIT"] = max(min(evolved.get("RSI_SHORT_EXIT", 40), 50), 25)
    evolved["ADX_THRESHOLD"] = max(min(evolved.get("ADX_THRESHOLD", 35), 50), 25)

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


# 参数名 → 策略文件变量名映射 (同时被 apply_params 和 apply_ai_proposals 使用)
PARAM_MAP = {
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
}


def apply_params(new_params: dict, auto_restart: bool = True) -> bool:
    """将新参数写入 strategy 文件 (模块级常量), 可选自动重启引擎"""
    content = STRATEGY_FILE.read_text("utf-8")

    import re
    changes_made = 0
    for key, var_name in PARAM_MAP.items():
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


# ── AI 进化模式 ──────────────────────────────────────────────────────────

# 参数安全边界 (AI 提案会被 clamp 到此范围)
PARAM_BOUNDS = {
    "RSI_LONG_ENTRY": (20, 45),
    "RSI_LONG_MAX_ENTRY": (50, 75),
    "RSI_LONG_EXIT": (65, 85),
    "RSI_SHORT_ENTRY": (40, 65),
    "RSI_SHORT_MIN_ENTRY": (20, 45),
    "RSI_SHORT_EXIT": (25, 50),
    "ADX_THRESHOLD": (25, 50),
    "ATR_STOP_LONG": (1.0, 3.0),
    "ATR_STOP_SHORT": (1.2, 3.5),
    "MAX_POSITION_PCT": (0.10, 1.0),
    "MACD_LONG_THRESHOLD": (5, 35),
    "MACD_SHORT_THRESHOLD": (-35, -5),
    "MAX_HOLD_BARS": (12, 48),
    "MIN_HOLD_BARS": (2, 8),
}

# 方向漂移检测: 同一参数连续 N 天同向移动 → 锁定
DRIFT_LOCK_THRESHOLD = 3   # 连续3天同向 → 锁定
MAX_DAILY_PARAMS_CHANGED = 3  # 单日最多变更参数数


def _detect_drift_locks() -> dict[str, str]:
    """从历史快照检测哪些参数已被连续同向推动，返回锁定表。

    读取 params_evolved_*.json 快照，对每个参数检查最近 N 天
    是否持续同向变化。如果是，返回 {param_name: lock_reason}。
    """
    snapshots = sorted(REVIEW_DIR.glob("params_evolved_*.json"))
    if len(snapshots) < DRIFT_LOCK_THRESHOLD:
        return {}

    recent = snapshots[-DRIFT_LOCK_THRESHOLD:]
    history = []
    for sp in recent:
        data = json.loads(sp.read_text("utf-8"))
        params = data.get("params", {})
        history.append(params)

    locks = {}
    for param in PARAM_MAP:
        values = []
        for h in history:
            v = h.get(param)
            if v is not None:
                values.append(v)
        if len(values) < DRIFT_LOCK_THRESHOLD:
            continue

        # 检查是否连续同向
        directions = []
        for i in range(1, len(values)):
            delta = values[i] - values[i-1]
            if abs(delta) < 0.01:
                directions.append(0)
            else:
                directions.append(1 if delta > 0 else -1)

        # 所有方向都相同(非零) → 锁定
        if len(set(directions)) == 1 and directions[0] != 0:
            direction_word = "增加" if directions[0] > 0 else "减少"
            start_val = values[0]
            end_val = values[-1]
            locks[param] = (
                f"参数漂移锁定: {param} 已连续{DRIFT_LOCK_THRESHOLD}天{direction_word} "
                f"({start_val} → {end_val}). "
                f"跳过1天以阻断路径依赖。"
            )

    return locks


# ── AI 进化模式 ──────────────────────────────────────────────────────────

def apply_ai_proposals(
    proposals: list[dict],
    current: dict,
    *,
    auto_restart: bool = True,
    dry_run: bool = False,
) -> tuple[dict, list[str]]:
    """应用 AI 复盘返回的参数调整提案。

    Parameters
    ----------
    proposals : list[dict]
        AI 返回的 parameter_proposals 列表, 每项:
        {"param_name": "ADX_THRESHOLD", "new_value": 40, "reasoning": "..."}
    current : dict
        当前策略参数快照
    auto_restart : bool
        是否重启引擎
    dry_run : bool
        仅预览, 不写入

    Returns
    -------
    (applied_params, log_lines)
    """
    log_lines = []
    evolved = dict(current)

    # ── 漂移检测：阻断「一条道走到黑」─━─
    drift_locks = _detect_drift_locks()
    if drift_locks:
        log_lines.append("─── 漂移锁定 ───")
        for param, reason in drift_locks.items():
            log_lines.append(f"🚫 {reason}")
        log_lines.append("")

    applied_count = 0
    for prop in proposals:
        name = prop.get("param_name", "")
        if not name or name not in PARAM_MAP:
            log_lines.append(f"⚠️ 未知参数 '{name}' — 跳过")
            continue

        target_val = prop.get("new_value")
        if target_val is None:
            log_lines.append(f"⚠️ {name}: new_value 缺失 — 跳过")
            continue

        reasoning = prop.get("reasoning", "(no reasoning)")

        # 类型强制转换
        current_val = current.get(name, 0)
        if isinstance(current_val, float):
            target_val = float(target_val)
        elif isinstance(current_val, int):
            target_val = int(target_val)

        # Clamp 到安全边界
        bounds = PARAM_BOUNDS.get(name)
        if bounds:
            old_target = target_val
            target_val = max(bounds[0], min(bounds[1], target_val))
            if target_val != old_target:
                log_lines.append(
                    f"🔒 {name}: {old_target} 越界, clamp → {target_val} "
                    f"(安全范围 [{bounds[0]}, {bounds[1]}])")

        # Smooth delta
        if name in SMOOTH_LIMITS:
            smoothed = _smooth_delta(name, target_val, current_val, SMOOTH_LIMITS)
            if smoothed != target_val:
                log_lines.append(
                    f"📏 {name}: {target_val} 跳变过大, smooth → {smoothed}")
                target_val = smoothed

        if target_val == current_val:
            log_lines.append(f"⏭ {name}: {current_val} (无变化, 跳过)")
            continue

        # 漂移锁定检查
        if name in drift_locks:
            log_lines.append(f"🚫 {name}: {drift_locks[name]}")
            continue

        # 单日变更数上限
        if applied_count >= MAX_DAILY_PARAMS_CHANGED:
            log_lines.append(
                f"⏭ {name}: 已达单日上限({MAX_DAILY_PARAMS_CHANGED}个参数), 跳过")
            continue

        log_lines.append(
            f"✅ {name}: {current_val} → {target_val} | {reasoning}")
        evolved[name] = target_val
        applied_count += 1

    changes = [l for l in log_lines if l.startswith("✅")]
    if not changes:
        log_lines.append("📊 AI 未提出任何有效参数变更 — 保持当前参数")
        return current, log_lines

    if dry_run:
        log_lines.append("🔍 dry-run 模式 — 未写入文件")
        return evolved, log_lines

    # 应用
    evolved["_changes"] = changes
    evolved["_evolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    evolved["_source"] = "ai_review"

    if apply_params(evolved, auto_restart=auto_restart):
        log_lines.append("🔄 策略文件已更新, 引擎已重启")
    else:
        log_lines.append("⚠️ 参数写入失败 (无匹配的常量)")

    return evolved, log_lines


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    apply = "--apply" in sys.argv
    ai_mode = "--ai-evolve" in sys.argv

    if ai_mode:
        # AI 进化模式: 从 JSON 文件读取 AI 提案
        proposals_path = None
        for i, arg in enumerate(sys.argv):
            if arg == "--ai-proposals":
                proposals_path = sys.argv[i + 1]
                break

        if not proposals_path:
            print("❌ --ai-evolve 需要 --ai-proposals <path_to_ai_proposals.json>")
            sys.exit(1)

        proposals_data = json.loads(Path(proposals_path).read_text())
        proposals = proposals_data.get("parameter_proposals", [])

        if not proposals:
            print("✅ AI 判断无需调整参数 — 当前参数已是最优")
            sys.exit(0)

        current = load_strategy_params()

        print("🤖 CryptoQuant AI 策略进化")
        print(f"   AI 提案数: {len(proposals)}")
        print()
        print("   当前参数:", json.dumps({k: v for k, v in current.items()}, indent=2))
        print()

        evolved, log = apply_ai_proposals(
            proposals, current, auto_restart=apply, dry_run=dry_run)
        for line in log:
            print(f"   {line}")

        if not dry_run and apply:
            print()
            print("✅ AI 策略参数已更新")
        elif not dry_run:
            print()
            print("💡 加 --apply 应用变更，加 --dry-run 仅预览")

        sys.exit(0)

    # ── 传统硬编码进化模式 (保留向后兼容) ──
    review = load_latest_review()
    if not review:
        print("❌ 无复盘报告，请先运行 daily_review.py")
        sys.exit(1)

    current = load_strategy_params()
    evolved = compute_evolved_params(review, current)

    print("📊 CryptoQuant 策略自进化 (规则引擎)")
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