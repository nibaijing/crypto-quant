"""回测报告生成器 - 生成 HTML / Markdown 格式的详细报告"""

import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from core.config import get_config


class BacktestReporter:
    """回测报告生成"""
    
    def __init__(self, results: Dict[str, Any], 
                 strategy_name: str = "Unnamed Strategy"):
        self.results = results
        self.strategy_name = strategy_name
        self.config = get_config()
    
    def summary_markdown(self) -> str:
        """生成 Markdown 格式的摘要报告"""
        r = self.results
        
        report = f"""# 📊 回测报告: {self.strategy_name}

**生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## 核心指标

| 指标 | 数值 |
|------|------|
| 初始资金 | {r['initial_capital']:,.2f} USDT |
| 最终权益 | {r['final_equity']:,.2f} USDT |
| **总收益率** | **{r['total_return_pct']:.2f}%** |
| 最大回撤 | {r['max_drawdown_pct']:.2f}% |
| 夏普比率 | {r['sharpe_ratio']:.2f} |

## 交易统计

| 指标 | 数值 |
|------|------|
| 总交易次数 | {r['total_trades']} |
| 盈利次数 | {r['winning_trades']} |
| 亏损次数 | {r['losing_trades']} |
| 胜率 | {r['win_rate_pct']:.1f}% |
| 盈亏比 | {r['profit_factor']:.2f} |

## 成本

| 项目 | 金额 (USDT) |
|------|-------------|
| 总手续费 | {r['total_commission']:,.2f} |
| 资金费率 | {r['total_funding']:,.2f} |

---

> **说明:** 回测基于历史数据，过去表现不代表未来收益。实盘需考虑滑点、延迟、流动性等因素。
"""
        return report
    
    def save_markdown(self, path: Path = None):
        """保存报告到 Markdown 文件"""
        if path is None:
            path = self.config.results_path / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        
        md = self.summary_markdown()
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        
        print(f"报告已保存: {path}")
        return path
    
    def export_trades_csv(self, path: Path = None) -> Path:
        """导出成交记录为 CSV"""
        if path is None:
            path = self.config.results_path / f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        fills = self.results.get("fills", [])
        records = []
        for f in fills:
            records.append({
                "timestamp": f.timestamp,
                "datetime": datetime.fromtimestamp(f.timestamp / 1000).isoformat(),
                "symbol": f.symbol,
                "side": f.side,
                "size": f.size,
                "price": f.price,
                "commission": f.commission,
                "pos_side": f.pos_side,
            })
        
        df = pd.DataFrame(records)
        df.to_csv(path, index=False)
        
        print(f"成交记录已导出: {path}")
        return path