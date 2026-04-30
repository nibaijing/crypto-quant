#!/usr/bin/env python3
"""测试OKX实盘执行器的基本功能"""

import sys
import logging
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from core.config import get_config
from execution.okx_executor import OKXExecutor

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

logger = logging.getLogger(__name__)


def test_okx_executor():
    """测试OKX执行器"""
    logger.info("=" * 60)
    logger.info("开始测试OKX实盘执行器")
    logger.info("=" * 60)
    
    try:
        # 初始化配置
        config = get_config()
        logger.info(f"配置加载成功 | testnet={config.exchange.testnet}")
        
        # 检查API配置
        if not config.exchange.api_key or not config.exchange.api_secret:
            logger.warning("⚠️  未配置API Key，将使用模拟盘模式")
            logger.warning("   请在config/settings.yaml中设置API Key")
            logger.warning("   或设置环境变量: CQ_EXCHANGE__API_KEY, CQ_EXCHANGE__API_SECRET")
        
        # 初始化执行器
        logger.info("初始化OKX执行器...")
        executor = OKXExecutor(config)
        logger.info("✅ OKX执行器初始化成功")
        
        # 测试获取账户信息
        logger.info("\n测试1: 获取账户信息")
        account = executor.get_account()
        logger.info(f"   总权益: ${account.total_equity:,.2f}")
        logger.info(f"   可用余额: ${account.available_balance:,.2f}")
        logger.info(f"   保证金余额: ${account.margin_balance:,.2f}")
        logger.info(f"   浮动盈亏: ${account.unrealized_pnl:+.2f}")
        logger.info(f"   总交易: {account.total_trades}次")
        logger.info(f"   胜率: {account.win_rate:.1f}%")
        logger.info(f"   API连接: {'✅' if account.api_connected else '❌'}")
        logger.info(f"   风险等级: {account.risk_level}")
        
        # 测试获取行情
        logger.info("\n测试2: 获取行情")
        ticker = executor.get_ticker("BTC-USDT-SWAP")
        if ticker:
            logger.info(f"   BTC价格: ${ticker['price']:,.2f}")
        else:
            logger.warning("   ⚠️  获取行情失败")
        
        # 测试获取K线
        logger.info("\n测试3: 获取K线")
        klines = executor.get_klines("BTC-USDT-SWAP", "1h", 10)
        if klines:
            logger.info(f"   获取到 {len(klines)} 条K线")
            latest = klines[-1]
            logger.info(f"   最新: ${latest['close']:,.2f} | 时间: {latest['timestamp']}")
        else:
            logger.warning("   ⚠️  获取K线失败")
        
        # 测试持仓同步
        logger.info("\n测试4: 同步持仓")
        position = executor._sync_position()
        if position:
            logger.info(f"   持仓: {position.side} {position.size:.4f} @ ${position.entry_price:,.2f}")
            logger.info(f"   浮动盈亏: ${position.unrealized_pnl:+.,.2f} ({position.unrealized_pnl_pct:+.2f}%)")
            logger.info(f"   杠杆: {position.leverage}x")
            logger.info(f"   强平价格: ${position.liq_price:,.2f}")
        else:
            logger.info("   当前无持仓")
        
        # 测试风控检查
        logger.info("\n测试5: 风控检查")
        passed, reason = executor._check_risk("BTC-USDT-SWAP", "buy", 0.01)
        logger.info(f"   风控检查: {'✅ 通过' if passed else '❌ 拒绝'}")
        logger.info(f"   原因: {reason}")
        
        logger.info("\n" + "=" * 60)
        logger.info("✅ 所有测试完成")
        logger.info("=" * 60)
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 测试失败: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    success = test_okx_executor()
    sys.exit(0 if success else 1)
