"""
╔══════════════════════════════════════════════════════════════╗
║           CRYPTO SCANNER v5 — PRE-BREAKOUT ONLY             ║
║                  ⚡ ПАТТЕРН D                                ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  ТРЁХФАЗНОЕ НАКОПЛЕНИЕ:                                      ║
║                                                              ║
║  Фаза 1  СТОИТ ДОЛГО    цена в узком диапазоне 20-180д       ║
║                         OI тихий, объём мёртвый              ║
║                         Нет даунтренда перед базой           ║
║                                                              ║
║  Фаза 2  OI РАСТЁТ      цена почти не двигается  ← МЫ ЗДЕСЬ ║
║                         OI плавно растёт +20%+ за 24-48ч    ║
║                         Умные деньги тихо набирают позицию   ║
║                                                              ║
║  Фаза 3  БУМ            пробой — сканер уже оповестил        ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║  Данные:   Binance Futures API (бесплатно, без ключа)        ║
║  Алерты:   Telegram                                          ║
║  Интервал: каждый час                                        ║
╚══════════════════════════════════════════════════════════════╝
"""

import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from apscheduler.schedulers.blocking import BlockingScheduler

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════

TELEGRAM_TOKEN  = "8731868942:AAEKTM-hbrskq52V3wFtoKfUEr2Hn5-mrHQ"
CHAT_ID         = "181943757"

MIN_SCORE       = 25    # порог срабатывания
TOP_RESULTS     = 7     # топ сигналов в одном сообщении
SCAN_HOURS      = 0.5   # каждые 30 минут
SLEEP_REQ       = 0.05  # пауза между запросами
WORKERS         = 4     # параллельных потоков

# Пороги фазы 2 — можно настраивать
OI_24H_MIN      = 3     # минимальный рост OI за 3-6ч (%)
PRICE_CHG_MAX   = 25    # максимальное изменение цены за 6ч (%)
BASE_RANGE_MAX  = 80    # максимальный диапазон базы (%)
DOWNTREND_MAX   = -35   # порог даунтренда перед базой (%)

# ══════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  BINANCE API
# ══════════════════════════════════════════════════════════════

BASE = "https://fapi.binance.com"

def api(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def get_symbols():
    data = api(f"{BASE}/fapi/v1/exchangeInfo")
    if not data:
        return []
    return [
        s['symbol'] for s in data['symbols']
        if s['quoteAsset'] == 'USDT'
        and s['status'] == 'TRADING'
        and s['contractType'] == 'PERPETUAL'
    ]

def klines(symbol, interval, limit):
    data = api(f"{BASE}/fapi/v1/klines",
               {"symbol": symbol, "interval": interval, "limit": limit})
    if not data or not isinstance(data, list) or len(data) < 5:
        return None
    df = pd.DataFrame(data, columns=[
        'open_time','open','high','low','close','volume',
        'close_time','quote_vol','trades',
        'taker_buy_base','taker_buy_quote','ignore'
    ])
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

def oi_hist(symbol, period, limit):
    data = api(f"{BASE}/futures/data/openInterestHist",
               {"symbol": symbol, "period": period, "limit": limit})
    if not data or not isinstance(data, list):
        return None
    df = pd.DataFrame(data)
    df['oi'] = df['sumOpenInterest'].astype(float)
    return df

# ══════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════

def oi_growth(oi_df, days):
    """Рост OI: первая половина периода vs вторая."""
    if oi_df is None or len(oi_df) < max(days, 8):
        return 0, False
    sl    = oi_df.iloc[-days:]
    half  = len(sl) // 2
    early = sl['oi'].iloc[:half].mean()
    late  = sl['oi'].iloc[half:].mean()
    g     = round((late - early) / early * 100, 1) if early > 0 else 0
    return g, g >= 10

def calc_natr(df, n=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return (tr.rolling(n).mean() / c * 100).round(3)

def oi_slope_angle(oi_series):
    """
    Угол наклона роста OI через линейную регрессию.
    Нормализуем значения чтобы получить реальный угол.
    0-90°: 45-65° = идеальное планомерное накопление.
    """
    try:
        y = oi_series.values.astype(float)
        if len(y) < 3:
            return 0
        x = np.arange(len(y))
        mn = y.min(); mx = y.max()
        y_norm = (y - mn) / (mx - mn) if mx != mn else np.zeros_like(y)
        slope, _ = np.polyfit(x, y_norm, 1)
        angle = round(np.degrees(np.arctan(slope * len(y))), 1)
        return max(0, min(90, angle))
    except:
        return 0

def no_downtrend(k1d, pre_days=90, skip_last=20):
    """
    Защита от нисходящего накопления.
    True = нет сильного даунтренда перед текущей зоной.
    """
    if k1d is None or len(k1d) < pre_days:
        return True
    c     = k1d['close']
    start = c.iloc[-pre_days]
    end   = c.iloc[-skip_last]
    trend = (end - start) / start * 100
    return trend > DOWNTREND_MAX

# ══════════════════════════════════════════════════════════════
#  ПАТТЕРН D — PRE-BREAKOUT ⚡
# ══════════════════════════════════════════════════════════════

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def pattern_d(symbol, k1d, k1h, oi_d, oi_1h):
    """
    UPGRADED 16.04.2026

    ФАЗА 1 — БАЗА:
      EMA 20/50 сжаты горизонтально
      Цена в узком диапазоне без HH/LL
      Scoring по длине базы × амплитуде (макс 50)

    ФАЗА 2 — OI НАКОПЛЕНИЕ:
      OI угол 45-55° идеал (макс 40)
      Цена почти не двигается
      OI растёт прямо сейчас

    ФАЗА 3 — NATR ПРОБУЖДЕНИЕ:
      NATR начинает расти (макс 20)
      OI дневной подтверждает
    """

    if k1d is None or k1h is None or oi_1h is None:
        return 0, {}
    if len(k1d) < 25 or len(k1h) < 12 or len(oi_1h) < 6:
        return 0, {}

    score = 0
    d = {"pattern": "D_upgraded", "symbol": symbol}

    price_now = k1d['close'].iloc[-1]
    d['price_now'] = round(price_now, 8)

    # ══════════════════════════════════════════════════════
    #  ФАЗА 1 — БАЗА (макс 50 очков)
    # ══════════════════════════════════════════════════════

    # Защита от даунтренда перед базой
    if len(k1d) >= 90:
        pre_start = k1d['close'].iloc[-90]
        pre_end   = k1d['close'].iloc[-20]
        pre_trend = round((pre_end - pre_start) / pre_start * 100, 1)
        d['pre_trend_pct'] = pre_trend
        if pre_trend < DOWNTREND_MAX:
            return 0, {}
    else:
        d['pre_trend_pct'] = None

    # Ищем лучшее базовое окно от 6 до 180 дней
    best_window = None
    best_range  = 999
    for bw in [6, 10, 14, 20, 30, 45, 60, 90, 120, 180]:
        if len(k1d) < bw + 3:
            continue
        b  = k1d.iloc[-bw:-3]
        bh = b['high'].max()
        bl = b['low'].min()
        br = round((bh - bl) / bl * 100, 1) if bl > 0 else 999
        if br < best_range and br <= BASE_RANGE_MAX:
            best_range  = br
            best_window = bw

    if best_window is None or best_window < 6:
        return 0, {}

    base        = k1d.iloc[-best_window:-3]
    base_high   = base['high'].max()
    base_low    = base['low'].min()
    base_range  = best_range
    d['base_days']      = best_window
    d['base_range_pct'] = base_range

    if price_now < base_low * 0.95:
        return 0, {}

    # Проверка HH/LL внутри базы
    third = max(2, len(base) // 3)
    p1 = base.iloc[:third]
    p2 = base.iloc[third:third*2]
    p3 = base.iloc[third*2:]
    ll = p1['low'].min() > p2['low'].min() > p3['low'].min()
    hl = p1['low'].min() < p2['low'].min() < p3['low'].min()
    hh = p1['high'].max() < p2['high'].max() < p3['high'].max()
    if ll and not hl:
        return 0, {}  # даунтренд внутри базы

    # EMA сжатие — EMA20 и EMA50 близко друг к другу
    ema20 = calc_ema(k1d['close'], 20)
    ema50 = calc_ema(k1d['close'], 50)
    if len(ema20) >= 5 and len(ema50) >= 5:
        ema_gap = abs(ema20.iloc[-3] - ema50.iloc[-3]) / ema50.iloc[-3] * 100
        ema_gap_prev = abs(ema20.iloc[-10] - ema50.iloc[-10]) / ema50.iloc[-10] * 100
        d['ema_gap_pct']  = round(ema_gap, 2)
        ema_squeezed = ema_gap < 3.0  # EMA сжаты
        ema_expanding = ema20.iloc[-1] > ema20.iloc[-5]  # EMA20 начинает подниматься
    else:
        ema_squeezed  = False
        ema_expanding = False
        d['ema_gap_pct'] = None

    # SCORING ФАЗЫ 1
    # 1. Длина базы (потолок score)
    if best_window >= 60:   phase1_base = 40
    elif best_window >= 30: phase1_base = 30
    elif best_window >= 20: phase1_base = 20
    elif best_window >= 13: phase1_base = 15
    else:                   phase1_base = 10  # 6-12 дней

    # 2. Амплитуда (бонус/штраф)
    if base_range < 3:    phase1_amp = 10
    elif base_range < 6:  phase1_amp = 6
    elif base_range < 10: phase1_amp = 3
    elif base_range < 15: phase1_amp = 0
    else:                 phase1_amp = -5

    # 3. EMA сжатие бонус
    phase1_ema = 0
    if ema_squeezed:   phase1_ema += 6
    if ema_expanding:  phase1_ema += 4

    # 4. Штраф за HH/LL
    if hh and hl:   phase1_trend_penalty = -int((phase1_base + phase1_amp) * 0.4)
    elif hh or ll:  phase1_trend_penalty = -int((phase1_base + phase1_amp) * 0.2)
    else:           phase1_trend_penalty = 0

    phase1_score = max(0, phase1_base + phase1_amp + phase1_ema + phase1_trend_penalty)
    phase1_score = min(50, phase1_score)  # потолок 50
    score += phase1_score
    d['phase1_score'] = phase1_score

    # ══════════════════════════════════════════════════════
    #  ФАЗА 2 — OI НАКОПЛЕНИЕ (макс 40 очков)
    # ══════════════════════════════════════════════════════

    oi_now_val = oi_1h['oi'].iloc[-1]

    def oi_chg(n):
        if len(oi_1h) < n: return 0
        v = oi_1h['oi'].iloc[-n]
        return round((oi_now_val - v) / v * 100, 1) if v > 0 else 0

    oi_3h  = oi_chg(3)
    oi_6h  = oi_chg(6)
    oi_12h = oi_chg(12)
    oi_best = max(oi_3h, oi_6h, oi_12h)

    d['oi_3h_growth_pct']  = oi_3h
    d['oi_6h_growth_pct']  = oi_6h
    d['oi_12h_growth_pct'] = oi_12h
    d['oi_24h_growth_pct'] = oi_best

    if oi_best < OI_24H_MIN:
        return 0, {}

    # OI должен расти прямо сейчас
    if len(oi_1h) >= 3:
        if oi_1h['oi'].iloc[-1] < oi_1h['oi'].iloc[-3] * 1.002:
            return 0, {}
        if oi_1h['oi'].iloc[-1] < oi_1h['oi'].iloc[-2] and oi_1h['oi'].iloc[-2] < oi_1h['oi'].iloc[-3]:
            return 0, {}

    # Угол OI
    angle_6h  = oi_slope_angle(oi_1h['oi'].iloc[-6:])
    angle_12h = oi_slope_angle(oi_1h['oi'].iloc[-12:]) if len(oi_1h) >= 12 else 0
    best_angle = max(angle_6h, angle_12h)
    d['oi_angle_6h']  = angle_6h
    d['oi_angle_12h'] = angle_12h

    if best_angle > 78:
        return 0, {}

    # Scoring угла OI
    if 45 <= best_angle <= 55:   phase2_angle = 25
    elif 35 <= best_angle < 45:  phase2_angle = 15
    elif 55 < best_angle <= 65:  phase2_angle = 15
    elif 65 < best_angle <= 75:  phase2_angle = 5
    else:                        phase2_angle = 3

    # Плавность OI
    oi_window  = min(12, len(oi_1h))
    oi_changes = oi_1h['oi'].iloc[-oi_window:].pct_change().dropna()
    oi_smooth  = round(oi_changes.std() * 100, 2)
    d['oi_smooth'] = oi_smooth
    if oi_smooth < 3:   phase2_smooth = 5
    elif oi_smooth < 6: phase2_smooth = 3
    else:               phase2_smooth = 0

    # Цена в Фазе 2
    p6 = k1h['close'].iloc[-6] if len(k1h) >= 6 else k1h['close'].iloc[0]
    price_chg_6h = abs(round((price_now - p6) / p6 * 100, 2)) if p6 > 0 else 99
    d['price_chg_24h_pct'] = price_chg_6h

    if price_chg_6h > PRICE_CHG_MAX:
        return 0, {}

    if price_chg_6h < 1.5:  phase2_price = 15
    elif price_chg_6h < 3:  phase2_price = 8
    elif price_chg_6h < 5:  phase2_price = 4
    else:                    phase2_price = 0

    phase2_score = min(40, phase2_angle + phase2_smooth + phase2_price)
    score += phase2_score
    d['phase2_score'] = phase2_score

    # ══════════════════════════════════════════════════════
    #  ФАЗА 3 — NATR ПРОБУЖДЕНИЕ (макс 20 очков)
    # ══════════════════════════════════════════════════════

    natr_1h = calc_natr(k1h, n=7).dropna()
    if len(natr_1h) >= 12:
        natr_1h_base = round(natr_1h.iloc[-12:-3].mean(), 2)
        natr_1h_now  = round(natr_1h.iloc[-3:].mean(), 2)
        natr_awaken  = round(natr_1h_now / natr_1h_base, 2) if natr_1h_base > 0 else 1
        d['natr_awakening'] = natr_awaken

        if natr_awaken > 3.5:
            return 0, {}
        if natr_awaken > 1.8:   phase3_natr = 15
        elif natr_awaken > 1.3: phase3_natr = 10
        elif natr_awaken > 1.0: phase3_natr = 5
        else:                   phase3_natr = 0
    else:
        phase3_natr = 0
        d['natr_awakening'] = None

    # OI дневной подтверждение
    oi_daily_g, oi_daily_ok = oi_growth(oi_d, days=7)
    d['oi_daily_growth_pct'] = oi_daily_g
    if oi_daily_ok:
        if oi_daily_g > 40:   phase3_daily = 5
        elif oi_daily_g > 20: phase3_daily = 3
        else:                  phase3_daily = 1
    else:
        phase3_daily = 0

    phase3_score = min(20, phase3_natr + phase3_daily)
    score += phase3_score
    d['phase3_score'] = phase3_score

    # Итоговый score кепируем на 100
    score = min(100, score)
    d['score'] = score
    return score, d

# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

def tg_send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"TG error: {e}")

def tg_send_chart(sym, caption):
    """Отправляет скриншот чарта в Telegram через TradingView image API."""
    try:
        # Пробуем TradingView
        chart_url = (
            f"https://charts.tradingview.com/chart-image/"
            f"?symbol=BINANCE:{sym}.P"
            f"&interval=60&width=800&height=400&theme=dark"
        )
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            json={
                "chat_id": CHAT_ID,
                "photo": chart_url,
                "caption": caption,
                "parse_mode": "Markdown"
            },
            timeout=15
        )
        if r.status_code == 200 and r.json().get('ok'):
            return True

        # Запасной вариант — Binance chart
        chart_url2 = f"https://bin.bnbstatic.com/image/admin_mgs_image_upload/20240101/{sym}.png"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            json={
                "chat_id": CHAT_ID,
                "photo": chart_url2,
                "caption": caption,
                "parse_mode": "Markdown"
            },
            timeout=15
        )
        return True
    except Exception as e:
        log.error(f"Chart send error {sym}: {e}")
        return False

def fmt_signal(r, rank):
    sym    = r['symbol']
    score  = r.get('score', 0)
    pchg   = r.get('price_chg_24h_pct', '?')
    brange = r.get('base_range_pct', '?')
    bdays  = r.get('base_days', '?')
    oi3    = r.get('oi_3h_growth_pct', '?')
    oi6    = r.get('oi_6h_growth_pct', '?')
    oi12   = r.get('oi_12h_growth_pct', '?')
    angle  = r.get('oi_angle_12h', r.get('oi_angle_6h', '?'))
    p1     = r.get('phase1_score', '?')
    p2     = r.get('phase2_score', '?')
    p3     = r.get('phase3_score', '?')

    if score >= 90:   badge = " 🔥 EXCEPTIONAL"
    elif score >= 75: badge = " ⚡ STRONG+"
    elif score >= 60: badge = " STRONG"
    elif score >= 40: badge = " AVERAGE"
    else:             badge = ""

    lines = [
        f"🟢 *LONG*{badge}",
        f"{'─' * 24}",
        f"*{sym}*  Score: `{score}` · Ф1:`{p1}` Ф2:`{p2}` Ф3:`{p3}`",
        f"База: `{bdays}д` · сжатие `{brange}%`",
        f"OI угол: `{angle}°`",
        f"OI: `+{oi3}%` (3ч) · `+{oi6}%` (6ч) · `+{oi12}%` (12ч)",
        f"Цена: `±{pchg}%`",
        f"",
    ]
    return "\n".join(lines)

def fmt_alert(results, total_scanned):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    msg = f"_{now}_\n\n"
    for i, r in enumerate(results, 1):
        msg += fmt_signal(r, i)
    return msg

# ══════════════════════════════════════════════════════════════
#  ОСНОВНОЙ СКАНЕР
# ══════════════════════════════════════════════════════════════

def get_prefilter():
    """
    Быстрый предфильтр — один запрос на все 538 пар.
    Оставляем только пары где цена почти не двигалась за 24ч.
    Отсекает ~80% символов до детального скана.
    """
    try:
        data = api(f"{BASE}/fapi/v1/ticker/24hr")
        if not data or not isinstance(data, list):
            return None
        result = {}
        for t in data:
            if not t['symbol'].endswith('USDT'):
                continue
            pct = abs(float(t.get('priceChangePercent', 99)))
            vol = float(t.get('quoteVolume', 0))
            result[t['symbol']] = {
                'price_chg_24h': pct,
                'volume_24h': vol
            }
        return result
    except:
        return None

def scan_symbol(sym):
    """Полная проверка одного символа — запускается параллельно."""
    try:
        k1d  = klines(sym, "1d",  200)
        k1h  = klines(sym, "1h",   50)
        oi_d = oi_hist(sym, "1d",  60)
        oi_1h= oi_hist(sym, "1h",  50)

        score, d = pattern_d(sym, k1d, k1h, oi_d, oi_1h)
        return score, d
    except Exception as e:
        log.debug(f"Ошибка {sym}: {e}")
        return 0, {}

def run_scan():
    log.info("=" * 55)
    log.info(f"PRE-BREAKOUT SCANNER  {datetime.now().strftime('%d.%m %H:%M')}")
    log.info("=" * 55)

    symbols = get_symbols()
    if not symbols:
        log.error("Нет символов")
        return
    log.info(f"Всего символов: {len(symbols)}")

    # ── ШАГ 1: БЫСТРЫЙ ПРЕДФИЛЬТР (1 запрос) ──────────────────
    log.info("Предфильтр — загружаем все тикеры...")
    prefilter = get_prefilter()

    if prefilter:
        # Оставляем только пары где:
        # - цена изменилась менее чем на PRICE_CHG_MAX% за 24ч
        # - есть хоть какой-то объём
        candidates = [
            s for s in symbols
            if s in prefilter
            and prefilter[s]['price_chg_24h'] < PRICE_CHG_MAX
            and prefilter[s]['volume_24h'] > 50000
        ]
        log.info(f"После предфильтра: {len(candidates)} кандидатов (было {len(symbols)})")
    else:
        candidates = symbols
        log.info("Предфильтр недоступен — сканируем все")

    # ── ШАГ 2: ПАРАЛЛЕЛЬНЫЙ ДЕТАЛЬНЫЙ СКАН ────────────────────
    results = []
    errors  = 0
    done    = 0

    log.info(f"Запускаем {WORKERS} параллельных потоков...")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(scan_symbol, sym): sym for sym in candidates}

        for future in as_completed(futures):
            sym = futures[future]
            done += 1
            try:
                score, d = future.result()
                if score >= MIN_SCORE and d:
                    results.append(d)
                    log.info(
                        f"  ⚡ {sym:15s}  score={score:3d}  "
                        f"NATR x{d.get('natr_awakening','?')}  "
                        f"OI +{d.get('oi_24h_growth_pct','?')}%  "
                        f"угол {d.get('oi_angle_4h','?')}°"
                    )
            except Exception as e:
                errors += 1
                log.debug(f"Ошибка {sym}: {e}")

            if done % 50 == 0:
                log.info(f"Прогресс {done}/{len(candidates)} | сигналов: {len(results)}")
            # Отправляем промежуточно если много найдено и скан на полпути
            if done == 300 and len(results) >= 3:
                results.sort(key=lambda x: x['score'], reverse=True)
                tg_send(fmt_alert(results[:TOP_RESULTS], len(candidates)))
                log.info("Промежуточный TG отправлен на 300")

    results.sort(key=lambda x: x['score'], reverse=True)
    top = results[:TOP_RESULTS]

    log.info(f"\nРЕЗУЛЬТАТ: {len(results)} сигналов | ошибок: {errors}")

    if top:
        for r in top:
            log.info(
                f"  → {r['symbol']:15s} score={r['score']}  "
                f"NATR x{r.get('natr_awakening','?')}  "
                f"OI +{r.get('oi_24h_growth_pct','?')}%  "
                f"угол {r.get('oi_angle_6h','?')}°"
            )
        # Отправляем общий текстовый алерт
        tg_send(fmt_alert(top, len(candidates)))

        # Отправляем чарт для каждого топ сигнала
        for r in top[:3]:  # максимум 3 чарта
            sym     = r['symbol']
            score   = r['score']
            oi_g    = r.get('oi_24h_growth_pct', '?')
            angle   = r.get('oi_angle_6h', '?')
            caption = (
                f"⚡ *{sym}*  Score: {score}\n"
                f"OI +{oi_g}%  Угол {angle}°"
            )
            sent = tg_send_chart(sym, caption)
            if sent:
                log.info(f"  📊 Chart sent: {sym}")
            time.sleep(1)  # пауза между фото

        log.info("Telegram отправлен")
    else:
        log.info("Сигналов нет")
        tg_send(
            f"ℹ️ *Pre-Breakout Scanner*\n"
            f"_{datetime.now().strftime('%d.%m %H:%M')} · нет сигналов_\n"
            f"_Проверено: {len(candidates)} пар_"
        )

    log.info("=" * 55)

# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════

def debug_symbol(sym):
    """
    Режим отладки — показывает все значения для одного символа.
    Помогает понять почему символ не проходит фильтры.
    """
    log.info(f"\n{'='*55}")
    log.info(f"DEBUG: {sym}")
    log.info(f"{'='*55}")

    k1d  = klines(sym, "1d",  200)
    k1h  = klines(sym, "1h",   50)
    oi_d = oi_hist(sym, "1d",  60)
    oi_1h= oi_hist(sym, "1h",  50)

    # Предфильтр цены
    if k1h is not None and len(k1h) >= 6:
        p_now = k1h['close'].iloc[-1]
        p_6h  = k1h['close'].iloc[-6]
        chg   = abs(round((p_now - p_6h) / p_6h * 100, 2))
        log.info(f"Цена 6ч изменение: ±{chg}% (макс {PRICE_CHG_MAX}%) → {'OK' if chg < PRICE_CHG_MAX else 'ОТСЕВ'}")

    # Pre-trend
    if k1d is not None and len(k1d) >= 90:
        pre_s = k1d['close'].iloc[-90]
        pre_e = k1d['close'].iloc[-20]
        pt    = round((pre_e - pre_s) / pre_s * 100, 1)
        log.info(f"Тренд перед базой: {pt}% → {'OK' if pt >= -20 else 'ОТСЕВ (даунтренд)'}")

    # NATR
    if k1d is not None and len(k1d) >= 30:
        natr_d = calc_natr(k1d, 14).dropna()
        if len(natr_d) >= 20:
            nb = round(natr_d.iloc[-30:-3].mean(), 2)
            nn = round(natr_d.iloc[-1], 2)
            log.info(f"NATR дневной база: {nb} (макс 8) → {'OK' if nb < 8 else 'ОТСЕВ'}")
            log.info(f"NATR дневной сейчас: {nn}")

    if k1h is not None and len(k1h) >= 12:
        natr_h = calc_natr(k1h, 7).dropna()
        if len(natr_h) >= 12:
            nb = round(natr_h.iloc[-12:-3].mean(), 2)
            nn = round(natr_h.iloc[-3:].mean(), 2)
            aw = round(nn / nb, 2) if nb > 0 else 0
            log.info(f"NATR 1H база: {nb} → сейчас: {nn} → пробуждение: x{aw} (макс x3)")

    # OI
    if oi_1h is not None and len(oi_1h) >= 6:
        oi_now = oi_1h['oi'].iloc[-1]
        for h in [3, 6, 12]:
            if len(oi_1h) >= h:
                oi_ago = oi_1h['oi'].iloc[-h]
                g = round((oi_now - oi_ago) / oi_ago * 100, 1) if oi_ago > 0 else 0
                log.info(f"OI рост {h}ч: +{g}% (мин {OI_24H_MIN}%) → {'OK' if g >= OI_24H_MIN else 'МАЛО'}")

    if oi_d is not None and len(oi_d) >= 7:
        oi_g, oi_ok = oi_growth(oi_d, days=7)
        log.info(f"OI дневной рост 7д: +{oi_g}% → {'OK' if oi_ok else 'МАЛО'}")

    # Итоговый score
    score, d = pattern_d(sym, k1d, k1h, oi_d, oi_1h)
    log.info(f"\nИТОГ: score={score} (порог {MIN_SCORE}) → {'СИГНАЛ ✅' if score >= MIN_SCORE else 'НЕ ПРОШЁЛ ❌'}")
    if d:
        for k, v in d.items():
            if k not in ('pattern', 'symbol'):
                log.info(f"  {k}: {v}")
    log.info("="*55)

if __name__ == "__main__":
    import sys

    # Режим отладки: python scanner1_accumulation.py DEBUG INUSDT
    if len(sys.argv) == 3 and sys.argv[1] == "DEBUG":
        debug_symbol(sys.argv[2].upper())
    else:
        log.info("⚡ PRE-BREAKOUT SCANNER v5 запущен")
        log.info(f"Порог: score>={MIN_SCORE} | OI рост>={OI_24H_MIN}% | Цена<={PRICE_CHG_MAX}%")
        log.info(f"Интервал: каждые 30 минут")

        run_scan()

        scheduler = BlockingScheduler()
        scheduler.add_job(run_scan, 'interval', hours=SCAN_HOURS)
        log.info(f"Следующий скан через {SCAN_HOURS}ч")

        try:
            scheduler.start()
        except KeyboardInterrupt:
            log.info("Остановлено (Ctrl+C)")
