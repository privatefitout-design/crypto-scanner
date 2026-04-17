import requests
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import os
import sys

TELEGRAM_TOKEN = os.getenv(“TELEGRAM_TOKEN”, “8731868942:AAEKTM-hbrskq52V3wFtoKfUEr2Hn5-mrHQ”)
CHAT_ID = os.getenv(“CHAT_ID”, “181943757”)
BINANCE_FAPI = “https://fapi.binance.com/fapi/v1”

def send_telegram(message: str):
url = f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”
payload = {“chat_id”: CHAT_ID, “text”: message, “parse_mode”: “HTML”}
try:
requests.post(url, json=payload, timeout=10)
except:
pass

def get_all_futures_tickers():
r = requests.get(f”{BINANCE_FAPI}/ticker/24hr”, timeout=15)
data = r.json()
return [item for item in data if item[“symbol”].endswith(“USDT”)]

def analyze_symbol(symbol: str):
try:
klines = requests.get(f”{BINANCE_FAPI}/klines?symbol={symbol}&interval=1d&limit=400”).json()
if len(klines) < 100:
return None
df = pd.DataFrame(klines, columns=[“open_time”,“open”,“high”,“low”,“close”,“volume”,“close_time”,“quote_volume”,“count”,“taker_buy_volume”,“taker_buy_quote_volume”,“ignore”])
for col in [“open”,“high”,“low”,“close”,“quote_volume”]:
df[col] = pd.to_numeric(df[col])
df[“EMA20”] = df[“close”].ewm(span=20, adjust=False).mean()
df[“EMA50”] = df[“close”].ewm(span=50, adjust=False).mean()
tr = pd.concat([df[“high”]-df[“low”],(df[“high”]-df[“close”].shift()).abs(),(df[“low”]-df[“close”].shift()).abs()],axis=1).max(axis=1)
atr = tr.rolling(14).mean()
df[“NATR”] = atr / df[“close”] * 100
current_price = df[“close”].iloc[-1]
max_duration = 0
best_amp_pct = 0
best_ema_comp = 0
for duration in range(6, 181):
if duration > len(df): break
period = df.iloc[-duration:]
amp_pct = (period[“high”].max() - period[“low”].min()) / period[“low”].min() * 100
if amp_pct > 10: continue
if duration + 90 > len(df): continue
before = df.iloc[-duration-90:-duration]
before_change = (before[“close”].iloc[-1] - before[“close”].iloc[0]) / before[“close”].iloc[0] * 100
if before_change < -20: continue
ema_comp_pct = abs(period[“EMA20”].iloc[-1] - period[“EMA50”].iloc[-1]) / current_price * 100
if ema_comp_pct > 3: continue
x = np.arange(len(period))
slope_ema = np.polyfit(x, period[“EMA20”], 1)[0]
if abs(slope_ema) > 0.001 * current_price: continue
slope_price = np.polyfit(x, period[“close”], 1)[0]
if abs(slope_price) > 0.002 * current_price: continue
if period[“NATR”].iloc[-1] >= 8: continue
if duration > max_duration:
max_duration = duration
best_amp_pct = amp_pct
best_ema_comp = ema_comp_pct
if max_duration < 6:
return None
if max_duration <= 12: base_pts = 40
elif max_duration <= 20: base_pts = 55
elif max_duration <= 30: base_pts = 70
elif max_duration <= 45: base_pts = 85
else: base_pts = 100
amp_penalty = 0
if best_amp_pct > 2:
if best_amp_pct <= 5: amp_penalty = 10
elif best_amp_pct <= 10: amp_penalty = 20
else: amp_penalty = 30
base_score = base_pts - amp_penalty
oi_resp = requests.get(f”{BINANCE_FAPI}/openInterestHist?symbol={symbol}&period=1h&limit=24”).json()
if len(oi_resp) < 6: return None
oi_list = [float(item[“sumOpenInterest”]) for item in oi_resp]
recent_oi = oi_list[-12:] if len(oi_list) >= 12 else oi_list[-6:]
if len(recent_oi) < 6: return None
h_klines = requests.get(f”{BINANCE_FAPI}/klines?symbol={symbol}&interval=1h&limit=30”).json()
df_h = pd.DataFrame(h_klines, columns=[“open_time”,“open”,“high”,“low”,“close”,“volume”,“close_time”,“quote_volume”,“count”,“taker_buy_volume”,“taker_buy_quote_volume”,“ignore”])
for col in [“open”,“high”,“low”,“close”]:
df_h[col] = pd.to_numeric(df_h[col])
price_6h_change = (df_h[“close”].iloc[-1] - df_h[“close”].iloc[-7]) / df_h[“close”].iloc[-7] * 100
if abs(price_6h_change) > 5: return None
x = np.arange(len(recent_oi))
y = np.array(recent_oi) / recent_oi[0]
slope = np.polyfit(x, y, 1)[0]
angle = np.degrees(np.arctan(slope * 30))
if not (45 <= angle <= 65) or angle >= 78: return None
if recent_oi[-1] <= recent_oi[-2]: return None
hourly_pct = np.diff(recent_oi) / np.array(recent_oi[:-1]) * 100
if np.std(hourly_pct) >= 6: return None
oi_deltas = np.diff(recent_oi)
if len(oi_deltas) >= 2 and oi_deltas[-1] <= 0 and oi_deltas[-2] <= 0:
return None
tr_h = pd.concat([df_h[“high”]-df_h[“low”],(df_h[“high”]-df_h[“close”].shift()).abs(),(df_h[“low”]-df_h[“close”].shift()).abs()],axis=1).max(axis=1)
atr_h = tr_h.rolling(14).mean()
df_h[“NATR”] = atr_h / df_h[“close”] * 100
natr_recent = df_h[“NATR”].dropna().iloc[-6:]
if len(natr_recent) < 2 or natr_recent.iloc[-1] <= natr_recent.iloc[-6:-1].mean():
return None
cv_oi = np.std(recent_oi) / np.mean(recent_oi) * 100 if np.mean(recent_oi) > 0 else 100
if cv_oi >= 6: return None
oi_score = 0
angle_dev = abs(angle - 55)
if angle_dev <= 5: oi_score += 18
elif angle_dev <= 10: oi_score += 14
else: oi_score += 10
def oi_growth(hours):
idx = max(0, len(recent_oi) - 1 - hours)
return round((recent_oi[-1] - recent_oi[idx]) / recent_oi[idx] * 100) if idx < len(recent_oi) else 0
oi12 = oi_growth(12)
growth_bonus = min(12, int(oi12 / 4))
oi_score += growth_bonus
if cv_oi < 4: oi_score += 5
if np.std(hourly_pct) < 3: oi_score += 5
oi_score = min(40, oi_score)
total_score = min(100, base_score + oi_score)
if total_score < 60: return None
if total_score >= 90: tier, emoji = “EXCEPTIONAL”, “🔥”
elif total_score >= 85: tier, emoji = “STRONG+”, “⚡”
elif total_score >= 75: tier, emoji = “STRONG”, “”
else: tier, emoji = “AVERAGE”, “”
now = datetime.now()
alert = f”””<b>🟢 {now.strftime(”%d.%m.%Y %H:%M”)}</b>

<b>LONG {emoji} {tier}</b>
<b>{symbol}</b> • Score: <b>{int(total_score)}</b>

📏 <b>База:</b> {max_duration}д • сжатие <b>{round(best_ema_comp,1)}%</b>
📐 <b>OI угол:</b> {int(angle)}°
📈 <b>OI рост:</b> <b>+{oi_growth(3)}%</b> (3ч) • <b>+{oi_growth(6)}%</b> (6ч) • <b>+{oi12}%</b> (12ч)
📉 <b>Цена:</b> ±{abs(round(price_6h_change,1))}% (6ч)

<i>NATR 1H awakening • CV <6% • Антипамп ✅</i>”””
return alert
except:
return None

def scan():
tickers = get_all_futures_tickers()
symbols = [t[“symbol”] for t in tickers if float(t.get(“priceChangePercent”, 0)) < 25 and float(t.get(“quoteVolume”, 0)) > 50000]
alerts = []
with ThreadPoolExecutor(max_workers=8) as executor:
future_to_sym = {executor.submit(analyze_symbol, sym): sym for sym in symbols}
for future in as_completed(future_to_sym):
result = future.result()
if result:
alerts.append(result)
for alert in alerts:
send_telegram(alert)
print(alert)
if not alerts:
print(f”[{datetime.now()}] No signals”)

if **name** == “**main**”:
if len(sys.argv) > 1 and sys.argv[1] == “DEBUG”:
sym = sys.argv[2] if len(sys.argv) > 2 else “BTCUSDT”
result = analyze_symbol(sym)
print(result if result else f”No signal for {sym}”)
else:
print(“🚀 Crypto Pre-Breakout Scanner v3 запущен — каждый час”)
scheduler = BlockingScheduler()
scheduler.add_job(scan, IntervalTrigger(minutes=60), next_run_time=datetime.now())
scheduler.start()
