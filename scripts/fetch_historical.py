#!/usr/bin/env python3
"""拉取历史数据: BTC 日线 + Fear & Greed 历史"""
import requests, json, time
from datetime import datetime, timezone
from pathlib import Path

# 1. BTC 日线数据
resp = requests.get('https://api.binance.com/api/v3/klines', params={
    'symbol': 'BTCUSDT', 'interval': '1d', 'limit': 365
})
resp.raise_for_status()
btc_data = resp.json()

# 2. Fear & Greed 历史
time.sleep(1)
fg_resp = requests.get('https://api.alternative.me/fng/?limit=365')
fg_resp.raise_for_status()
fg_data = fg_resp.json()

# 3. 保存
output = {
    'btc_daily': [
        {'close_time': int(k[6]), 'open': float(k[1]), 'high': float(k[2]),
         'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])}
        for k in btc_data
    ],
    'fear_greed_history': [
        {'value': int(d['value']), 'timestamp': int(d['timestamp']),
         'classification': d['value_classification']}
        for d in fg_data['data']
    ]
}

DATA_DIR = Path('/home/ni/crypto_quant/data')
DATA_DIR.mkdir(parents=True, exist_ok=True)
with open(DATA_DIR / 'historical_data.json', 'w') as f:
    json.dump(output, f, indent=2)

print(f"BTC bars: {len(output['btc_daily'])}")
print(f"FG entries: {len(output['fear_greed_history'])}")
print(f"BTC range: {datetime.utcfromtimestamp(output['btc_daily'][0]['close_time']/1000)} -> {datetime.utcfromtimestamp(output['btc_daily'][-1]['close_time']/1000)}")
print(f"FG range: {datetime.utcfromtimestamp(output['fear_greed_history'][-1]['timestamp'])} -> {datetime.utcfromtimestamp(output['fear_greed_history'][0]['timestamp'])}")