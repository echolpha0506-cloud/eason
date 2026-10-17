import os
import time
import requests
import ccxt
import pandas as pd
from datetime import datetime, timezone

# ==================== 配置 ====================
SYMBOLS = ["BTC/USDT", "ETH/USDT"]  # 同时监控
SERVERCHAN_KEY = os.getenv("SERVERCHAN_KEY", "SCT440554TBR9un4ZZPUOu6r28LJvNDpY8")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "50"))  # 建议稍长一点，避免请求过快
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "8"))

ADX_THRESHOLD = 25
VOL_MULT = 1.8
BODY_MIN = 0.70

# ==================== 初始化 ====================
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# 每个交易对单独记录冷却时间
last_signal_time = {symbol: 0 for symbol in SYMBOLS}

def send_wechat(title: str, content: str):
    if not SERVERCHAN_KEY:
        print("未配置 SERVERCHAN_KEY，跳过推送")
        return
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    data = {"title": title, "desp": content}
    try:
        resp = requests.post(url, data=data, timeout=10)
        print("微信推送结果:", resp.text)
    except Exception as e:
        print("推送失败:", e)

def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def ta_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def ta_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calculate_simple_adx(df, period=14):
    up = df['high'].diff()
    down = -df['low'].diff()
    plus_dm = up.where((up > down) & (up > 0), 0)
    minus_dm = down.where((down > up) & (down > 0), 0)
    tr = ta_atr(df, period)
    plus_di = 100 * (plus_dm.rolling(period).mean() / tr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / tr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)) * 100
    return dx.rolling(period).mean()

def calculate_indicators(df: pd.DataFrame):
    df = df.copy()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['rsi'] = ta_rsi(df['close'], 14)
    df['vol_ma'] = df['volume'].rolling(20).mean()
    df['atr'] = ta_atr(df, 14)
    df['atr_ma'] = df['atr'].rolling(50).mean()
    df['adx'] = calculate_simple_adx(df, 14)
    return df

def check_signal(symbol: str):
    global last_signal_time
    
    df_1m = fetch_ohlcv(symbol, '1m', 120)
    df_5m = fetch_ohlcv(symbol, '5m', 50)
    df_15m = fetch_ohlcv(symbol, '15m', 50)
    
    df_1m = calculate_indicators(df_1m)
    df_5m = calculate_indicators(df_5m)
    df_15m = calculate_indicators(df_15m)
    
    row = df_1m.iloc[-2]
    row5 = df_5m.iloc[-2]
    row15 = df_15m.iloc[-2]
    
    trend_1m_bull = row['ema9'] > row['ema21'] and row['close'] > row['ema9']
    trend_1m_bear = row['ema9'] < row['ema21'] and row['close'] < row['ema9']
    trend_5m_bull = row5['ema9'] > row5['ema21']
    trend_15m_bull = row15['ema9'] > row15['ema21']
    
    body = abs(row['close'] - row['open'])
    range_ = row['high'] - row['low'] + 1e-10
    body_ratio = body / range_
    bull_pa = row['close'] > row['open'] and body_ratio >= BODY_MIN and row['close'] >= df_1m.iloc[-3]['high']
    bear_pa = row['close'] < row['open'] and body_ratio >= BODY_MIN and row['close'] <= df_1m.iloc[-3]['low']
    
    vol_ok = row['volume'] > row['vol_ma'] * VOL_MULT
    atr_ok = row['atr'] > row['atr_ma'] * 0.7
    rsi_bull = 53 < row['rsi'] < 72
    rsi_bear = 28 < row['rsi'] < 47
    adx_ok = row['adx'] > ADX_THRESHOLD
    
    long_signal = (trend_1m_bull and trend_5m_bull and trend_15m_bull and 
                   adx_ok and rsi_bull and vol_ok and atr_ok and bull_pa)
    short_signal = (trend_1m_bear and not trend_5m_bull and not trend_15m_bull and 
                    adx_ok and rsi_bear and vol_ok and atr_ok and bear_pa)
    
    now = time.time()
    if now - last_signal_time[symbol] < COOLDOWN_MINUTES * 60:
        return None
    
    if long_signal:
        last_signal_time[symbol] = now
        return "UP"
    if short_signal:
        last_signal_time[symbol] = now
        return "DOWN"
    return None

def main():
    print(f"机器人启动 | 监控: {', '.join(SYMBOLS)} | 检查间隔: {CHECK_INTERVAL}s")
    send_wechat("事件合约机器人启动", f"开始同时监控 {', '.join(SYMBOLS)}")
    
    while True:
        for symbol in SYMBOLS:
            try:
                signal = check_signal(symbol)
                if signal:
                    price = exchange.fetch_ticker(symbol)['last']
                    title = f"事件合约信号：{symbol} {signal}"
                    content = f"""
**交易对**: {symbol}
**方向**: {signal}
**当前价格**: {price}
**时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC
**建议**: 3-5分钟事件合约
"""
                    print(title)
                    send_wechat(title, content)
                else:
                    print(f"{datetime.now().strftime('%H:%M:%S')} {symbol} 无信号")
            except Exception as e:
                print(f"{symbol} 错误:", e)
            
            time.sleep(3)  # 每个币种之间稍微间隔，避免请求过快
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
