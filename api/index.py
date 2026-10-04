"""APEX all-in-one backend for Vercel (single FastAPI function)."""


import os, sys, json, time, hashlib, hmac, urllib.parse, urllib.request, mimetypes


from datetime import datetime, date


from contextlib import asynccontextmanager


import matplotlib
matplotlib.use('Agg')


import matplotlib.pyplot as plt


import pandas as pd
import numpy as np
import requests


import psycopg2, psycopg2.extras


from fastapi import FastAPI, Request, HTTPException


from fastapi.responses import JSONResponse, FileResponse


"""Env-based config for the Vercel deployment."""
import os

BOT_TOKEN = os.environ.get("APEX_BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
WEBAPP_URL = os.environ.get("APEX_WEBAPP_URL", "")

TIMEFRAME = "1h"
HTF = "4h"
KLIMIT = 300
BUY_LEVEL = 80
SELL_LEVEL = 80
SIGNAL_COOLDOWN_H = 12

SL_ATR = 2.0
TP_ATRS = (1.5, 3.0, 4.5, 6.0)
ENTRY_ZONE_ATR = 0.15

FREE_SIGNALS_PER_DAY = 3
REFS_FOR_VIP = 3
VIP_DAYS_PER_REF_MILESTONE = 30

PINNED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "NEARUSDT", "AUCTIONUSDT"]
TOP_N_BY_VOLUME = 25

import types as _t
config = _t.SimpleNamespace(**{k: v for k, v in list(globals().items()) if k.isupper()})



"""Postgres persistence (Neon / Vercel Postgres). Same interface as the SQLite version."""
import json
import time
from datetime import date

import psycopg2
import psycopg2.extras



def _conn():
    c = psycopg2.connect(config.DATABASE_URL)
    return c


def init():
    c = _conn()
    cur = c.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
        plan TEXT DEFAULT 'free', vip_until BIGINT DEFAULT 0,
        ref_by BIGINT DEFAULT 0, ref_count INT DEFAULT 0,
        signals_today INT DEFAULT 0, day TEXT DEFAULT '',
        watchlist TEXT DEFAULT '', prefs TEXT DEFAULT '{}', created BIGINT
    );
    CREATE TABLE IF NOT EXISTS signals(
        id SERIAL PRIMARY KEY, symbol TEXT, tf TEXT, side TEXT,
        entry DOUBLE PRECISION, sl DOUBLE PRECISION,
        tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION,
        tp3 DOUBLE PRECISION, tp4 DOUBLE PRECISION,
        score INT, created BIGINT, status TEXT DEFAULT 'open', closed BIGINT DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS deliveries(
        signal_id INT, user_id BIGINT, PRIMARY KEY(signal_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS journal(
        id SERIAL PRIMARY KEY, user_id BIGINT, symbol TEXT, side TEXT,
        entry DOUBLE PRECISION, sl DOUBLE PRECISION, tp DOUBLE PRECISION,
        result_pct DOUBLE PRECISION, note TEXT, created BIGINT
    );
    CREATE TABLE IF NOT EXISTS screener(
        symbol TEXT PRIMARY KEY, bull INT, bear INT,
        price DOUBLE PRECISION, rsi DOUBLE PRECISION, updated BIGINT
    );
    CREATE TABLE IF NOT EXISTS paper_trades(
        id SERIAL PRIMARY KEY, user_id BIGINT, signal_id INT, symbol TEXT, side TEXT,
        entry DOUBLE PRECISION, sl DOUBLE PRECISION,
        tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION,
        tp3 DOUBLE PRECISION, tp4 DOUBLE PRECISION,
        qty_usd DOUBLE PRECISION DEFAULT 100, status TEXT DEFAULT 'open',
        pnl_pct DOUBLE PRECISION DEFAULT 0, created BIGINT, closed BIGINT DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    c.commit()
    cur.close()
    c.close()


def _one(sql, args=()):
    c = _conn()
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, args)
    r = cur.fetchone()
    cur.close()
    c.close()
    return r


def _all(sql, args=()):
    c = _conn()
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    c.close()
    return rows


def _exec(sql, args=()):
    c = _conn()
    cur = c.cursor()
    cur.execute(sql, args)
    c.commit()
    cur.close()
    c.close()


def user(uid):
    return _one("SELECT * FROM users WHERE id=%s", (uid,))


def register(uid, username="", first_name="", ref_by=0):
    now = int(time.time())
    if not _one("SELECT 1 FROM users WHERE id=%s", (uid,)):
        _exec("INSERT INTO users(id,username,first_name,ref_by,created) VALUES(%s,%s,%s,%s,%s)",
              (uid, username or "", first_name or "", ref_by, now))
        if ref_by and _one("SELECT 1 FROM users WHERE id=%s", (ref_by,)):
            _exec("UPDATE users SET ref_count=ref_count+1 WHERE id=%s", (ref_by,))
            rc = _one("SELECT ref_count FROM users WHERE id=%s", (ref_by,))["ref_count"]
            if rc % config.REFS_FOR_VIP == 0:
                grant_vip(ref_by, config.VIP_DAYS_PER_REF_MILESTONE)
    else:
        _exec("UPDATE users SET username=%s, first_name=%s WHERE id=%s",
              (username or "", first_name or "", uid))


def is_vip(u):
    return bool(u) and u["plan"] == "vip" and u["vip_until"] > time.time()


def grant_vip(uid, days):
    now = int(time.time())
    cur = _one("SELECT vip_until FROM users WHERE id=%s", (uid,))
    base = max(now, cur["vip_until"] if cur else 0)
    _exec("UPDATE users SET plan='vip', vip_until=%s WHERE id=%s", (base + days * 86400, uid))


def can_receive(uid):
    u = user(uid)
    if not u:
        return False
    if is_vip(u):
        return True
    today = date.today().isoformat()
    if u["day"] != today:
        _exec("UPDATE users SET signals_today=0, day=%s WHERE id=%s", (today, uid))
        return True
    return u["signals_today"] < config.FREE_SIGNALS_PER_DAY


def mark_delivered(uid):
    _exec("UPDATE users SET signals_today=signals_today+1, day=%s WHERE id=%s",
          (date.today().isoformat(), uid))


def all_user_ids():
    return [r["id"] for r in _all("SELECT id FROM users")]


def save_signal(s):
    c = _conn()
    cur = c.cursor()
    cur.execute(
        """INSERT INTO signals(symbol,tf,side,entry,sl,tp1,tp2,tp3,tp4,score,created)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (s["symbol"], s["tf"], s["side"], s["entry"], s["sl"],
         s["tp1"], s["tp2"], s["tp3"], s["tp4"], s["score"], int(time.time())))
    sid = cur.fetchone()[0]
    c.commit()
    cur.close()
    c.close()
    return sid


def recent_signal(symbol, side, hours):
    return _one("SELECT 1 FROM signals WHERE symbol=%s AND side=%s AND created>%s",
                (symbol, side, int(time.time()) - hours * 3600)) is not None


def open_signals():
    return _all("SELECT * FROM signals WHERE status='open'")


def update_signal(sid, status):
    _exec("UPDATE signals SET status=%s, closed=%s WHERE id=%s",
          (status, int(time.time()), sid))


def record_delivery(sid, uid):
    _exec("INSERT INTO deliveries(signal_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
          (sid, uid))


def signal_recipients(sid):
    return [r["user_id"] for r in _all("SELECT user_id FROM deliveries WHERE signal_id=%s", (sid,))]


def stats_overall():
    rows = _all("SELECT status, COUNT(*) n FROM signals WHERE status!='open' GROUP BY status")
    d = {r["status"]: r["n"] for r in rows}
    wins = sum(d.get(k, 0) for k in ("tp1", "tp2", "tp3", "tp4"))
    losses = d.get("sl", 0)
    total = wins + losses + d.get("expired", 0)
    return {"wins": wins, "losses": losses, "expired": d.get("expired", 0),
            "total": total, "winrate": round(100 * wins / total, 1) if total else 0.0}


def latest_signals(n=5):
    return _all("SELECT * FROM signals ORDER BY id DESC LIMIT %s", (n,))


def get_signal(sid):
    return _one("SELECT * FROM signals WHERE id=%s", (sid,))


def upsert_screen(sym, bull, bear, price, rsi):
    _exec(
        """INSERT INTO screener(symbol,bull,bear,price,rsi,updated) VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(symbol) DO UPDATE SET bull=EXCLUDED.bull, bear=EXCLUDED.bear,
           price=EXCLUDED.price, rsi=EXCLUDED.rsi, updated=EXCLUDED.updated""",
        (sym, bull, bear, price, rsi, int(time.time())))


def screener_top(n=30):
    return _all("SELECT * FROM screener ORDER BY bull DESC LIMIT %s", (n,))


def get_prefs(uid):
    u = user(uid)
    try:
        p = json.loads((u and u["prefs"]) or "{}")
    except Exception:
        p = {}
    p.setdefault("tp_sl_reports", True)
    p.setdefault("market_analysis", False)
    p.setdefault("abnormal_signals", False)
    return p


def set_pref(uid, key, val):
    p = get_prefs(uid)
    p[key] = bool(val)
    _exec("UPDATE users SET prefs=%s WHERE id=%s", (json.dumps(p), uid))


def journal_add(uid, symbol, side, entry, sl, tp, result_pct, note=""):
    _exec(
        """INSERT INTO journal(user_id,symbol,side,entry,sl,tp,result_pct,note,created)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uid, symbol.upper(), side, entry, sl, tp, result_pct, note, int(time.time())))


def journal_list(uid, n=10):
    return _all("SELECT * FROM journal WHERE user_id=%s ORDER BY id DESC LIMIT %s", (uid, n))


def journal_stats(uid):
    rows = _all("SELECT result_pct FROM journal WHERE user_id=%s", (uid,))
    if not rows:
        return None
    rs = [r["result_pct"] for r in rows]
    wins = sum(1 for x in rs if x > 0)
    return {"n": len(rs), "winrate": round(100 * wins / len(rs), 1),
            "total_pct": round(sum(rs), 2)}


def paper_open(uid, s, qty_usd=100.0):
    c = _conn()
    cur = c.cursor()
    cur.execute(
        """INSERT INTO paper_trades(user_id,signal_id,symbol,side,entry,sl,tp1,tp2,tp3,tp4,qty_usd,created)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (uid, s["id"], s["symbol"], s["side"], s["entry"], s["sl"],
         s["tp1"], s["tp2"], s["tp3"], s["tp4"], qty_usd, int(time.time())))
    pid = cur.fetchone()[0]
    c.commit()
    cur.close()
    c.close()
    return pid


def paper_list(uid, status="open"):
    return _all("SELECT * FROM paper_trades WHERE user_id=%s AND status=%s ORDER BY id DESC",
                (uid, status))


def paper_close(pid, pnl_pct):
    _exec("UPDATE paper_trades SET status='closed', pnl_pct=%s, closed=%s WHERE id=%s",
          (pnl_pct, int(time.time()), pid))



"""Minimal Telegram Bot API client (urllib, no extra deps)."""
import json
import urllib.request
import mimetypes


BASE = "https://api.telegram.org/bot"


def _post(method, payload, token=None):
    token = token or config.BOT_TOKEN
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + token + "/" + method, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
         "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return _post("sendMessage", p)


def _multipart(fields, files):
    boundary = "----apexbound"
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, data) in files.items():
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                 f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, boundary


def send_photo(chat_id, photo_path, caption=None, reply_markup=None):
    token = config.BOT_TOKEN
    with open(photo_path, "rb") as f:
        data = f.read()
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
        fields["parse_mode"] = "HTML"
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup)
    body, boundary = _multipart(fields, {"photo": ("chart.png", data)})
    req = urllib.request.Request(
        BASE + token + "/sendPhoto", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def answer_callback(callback_id, text=""):
    return _post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})



"""APEX confluence engine — Python port of the TradingView 'SMPS APEX v2' logic.

Scans Binance klines and returns a confluence score 0-100 plus all sub-signals.
No repaint: every decision uses the last CLOSED candle only.
"""
import requests
import pandas as pd
import numpy as np

BINANCE_HOSTS = [
    "https://data-api.binance.vision",   # public market data, no geo-block
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]


def _get(path, params):
    last = None
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last


# ── market data ────────────────────────────────────────────────
def klines(symbol, interval, limit=config.KLIMIT):
    data = _get("/api/v3/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    d = pd.DataFrame(data, columns=[
        "ot", "o", "h", "l", "c", "v", "ct", "qv", "n", "tb", "tq", "ig"])
    for k in ("o", "h", "l", "c", "v"):
        d[k] = d[k].astype(float)
    d["t"] = pd.to_datetime(d["ct"], unit="ms")
    return d


def top_usdt_symbols(n):
    """Top-n USDT spot pairs by 24h quote volume, minus stables/leveraged."""
    r = _get("/api/v3/ticker/24hr", {})
    skip = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    rows = [x for x in r
            if x["symbol"].endswith("USDT") and x["symbol"] not in skip
            and not any(s in x["symbol"] for s in ("FDUSD", "USDC", "TUSD", "DAI", "AEUR"))]
    rows.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    syms = [x["symbol"] for x in rows[:n]]
    for s in config.PINNED_SYMBOLS:
        if s not in syms:
            syms.append(s)
    return syms


# ── indicators ─────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    h, l, c = df["h"], df["l"], df["c"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def supertrend(df, mult=3.0, length=10):
    a = atr(df, length)
    hl2 = (df["h"] + df["l"]) / 2
    up = hl2 + mult * a
    lo = hl2 - mult * a
    st = pd.Series(np.nan, index=df.index)
    d = pd.Series(1, index=df.index)          # 1 = down, -1 = up (Pine convention)
    for i in range(1, len(df)):
        if d.iloc[i - 1] == -1:
            lo.iloc[i] = max(lo.iloc[i], lo.iloc[i - 1])
        else:
            up.iloc[i] = min(up.iloc[i], up.iloc[i - 1])
        if df["c"].iloc[i] > up.iloc[i - 1]:
            d.iloc[i] = -1
        elif df["c"].iloc[i] < lo.iloc[i - 1]:
            d.iloc[i] = 1
        else:
            d.iloc[i] = d.iloc[i - 1]
            if d.iloc[i] == -1 and lo.iloc[i] < lo.iloc[i - 1]:
                lo.iloc[i] = lo.iloc[i - 1]
            if d.iloc[i] == 1 and up.iloc[i] > up.iloc[i - 1]:
                up.iloc[i] = up.iloc[i - 1]
        st.iloc[i] = lo.iloc[i] if d.iloc[i] == -1 else up.iloc[i]
    return st, d


def dmi_adx(df, n=14):
    h, l, c = df["h"], df["l"], df["c"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    str_ = tr.ewm(alpha=1 / n, adjust=False).mean()
    sp = plus_dm.ewm(alpha=1 / n, adjust=False).mean()
    sm = minus_dm.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * sp / str_
    mdi = 100 * sm / str_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    return pdi, mdi, adx


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def macd(s, f=12, sl=26, sig=9):
    m = ema(s, f) - ema(s, sl)
    sg = ema(m, sig)
    return m - sg


def stoch_kd(df, n=14, k=3, d=3):
    ll = df["l"].rolling(n).min()
    hh = df["h"].rolling(n).max()
    raw = 100 * (df["c"] - ll) / (hh - ll).replace(0, np.nan)
    K = raw.rolling(k).mean()
    D = K.rolling(d).mean()
    return K, D


def mfi(df, n=14):
    tp = (df["h"] + df["l"] + df["c"]) / 3
    rmf = tp * df["v"]
    pos = pd.Series(np.where(tp > tp.shift(1), rmf, 0.0), index=df.index)
    neg = pd.Series(np.where(tp < tp.shift(1), rmf, 0.0), index=df.index)
    mfr = pos.rolling(n).sum() / neg.rolling(n).sum().replace(0, np.nan)
    return 100 - 100 / (1 + mfr)


# ── confluence ─────────────────────────────────────────────────
def analyze(symbol):
    """Full APEX analysis on the last CLOSED candle. Returns dict or None."""
    df = klines(symbol, config.TIMEFRAME)
    if len(df) < 220:
        return None
    df = df.iloc[:-1].copy()          # drop forming candle — no repaint
    c = df["c"]

    # HTF bias (4h)
    try:
        htf = klines(symbol, config.HTF, limit=220).iloc[:-1]
        htf_up = htf["c"].iloc[-1] > ema(htf["c"], 200).iloc[-1]
    except Exception:
        htf_up = c.iloc[-1] > ema(c, 200).iloc[-1]

    eF, eS, eL = ema(c, 21), ema(c, 50), ema(c, 200)
    bull_stack = (eF.iloc[-1] > eS.iloc[-1] > eL.iloc[-1])
    bear_stack = (eF.iloc[-1] < eS.iloc[-1] < eL.iloc[-1])

    _, st_dir = supertrend(df)
    trend_up = st_dir.iloc[-1] < 0

    pdi, mdi, adx_v = dmi_adx(df)
    adx_ok_bull = adx_v.iloc[-1] >= 18 and pdi.iloc[-1] > mdi.iloc[-1]
    adx_ok_bear = adx_v.iloc[-1] >= 18 and mdi.iloc[-1] > pdi.iloc[-1]

    r = rsi(c)
    rsi_bull = r.iloc[-1] > 55 and r.iloc[-1] > r.iloc[-2]
    rsi_bear = r.iloc[-1] < 45 and r.iloc[-1] < r.iloc[-2]

    mh = macd(c)
    macd_bull = mh.iloc[-1] > 0 and mh.iloc[-1] > mh.iloc[-2]
    macd_bear = mh.iloc[-1] < 0 and mh.iloc[-1] < mh.iloc[-2]

    K, D = stoch_kd(df)
    st_bull = K.iloc[-1] > D.iloc[-1] and K.iloc[-1] > K.iloc[-2]
    st_bear = K.iloc[-1] < D.iloc[-1] and K.iloc[-1] < K.iloc[-2]

    m = mfi(df)
    mfi_bull = m.iloc[-1] > 50 and m.iloc[-1] > m.iloc[-2]
    mfi_bear = m.iloc[-1] < 50 and m.iloc[-1] < m.iloc[-2]

    v = df["v"].fillna(0)
    vol_ma = v.rolling(20).mean()
    rel_vol = v.iloc[-1] / (vol_ma.iloc[-1] or 1)
    signed = pd.Series(np.where(c > df["o"], v, np.where(c < df["o"], -v, 0.0)), index=df.index)
    cvd = signed.cumsum()
    cvd_up = cvd.iloc[-1] > cvd.rolling(20).mean().iloc[-1]
    cvd_dn = cvd.iloc[-1] < cvd.rolling(20).mean().iloc[-1]
    flow_bull = rel_vol >= 1.5 and cvd_up
    flow_bear = rel_vol >= 1.5 and cvd_dn
    whale = rel_vol >= 2.5

    # TTM squeeze
    basis = c.rolling(20).mean()
    dev = 2.0 * c.rolling(20).std()
    tr = pd.concat([df["h"] - df["l"], (df["h"] - c.shift(1)).abs(),
                    (df["l"] - c.shift(1)).abs()], axis=1).max(axis=1)
    kc_dev = 1.5 * tr.rolling(20).mean()
    sqz = (basis - dev > basis - kc_dev) & (basis + dev < basis + kc_dev)
    sqz_on = bool(sqz.iloc[-1])
    sqz_rel = (not sqz_on) and bool(sqz.iloc[-2])

    # break of structure
    hh = df["h"].rolling(20).max().shift(1)
    ll = df["l"].rolling(20).min().shift(1)
    bos_bull = c.iloc[-1] > hh.iloc[-1] and c.iloc[-2] <= hh.iloc[-2]
    bos_bear = c.iloc[-1] < ll.iloc[-1] and c.iloc[-2] >= ll.iloc[-2]

    bull = sum([htf_up * 20, bull_stack * 15, trend_up * 10, adx_ok_bull * 10,
                rsi_bull * 8, macd_bull * 7, st_bull * 5, mfi_bull * 8,
                flow_bull * 12, sqz_rel * 5, bos_bull * 5])
    bear = sum([(not htf_up) * 20, bear_stack * 15, (not trend_up) * 10, adx_ok_bear * 10,
                rsi_bear * 8, macd_bear * 7, st_bear * 5, mfi_bear * 8,
                flow_bear * 12, sqz_rel * 5, bos_bear * 5])
    bull = min(int(bull), 100)
    bear = min(int(bear), 100)

    a = atr(df).iloc[-1]
    price = c.iloc[-1]

    side = None
    if bull >= config.BUY_LEVEL:
        side = "LONG"
    elif bear >= config.SELL_LEVEL:
        side = "SHORT"

    sig = None
    if side:
        if side == "LONG":
            entry = price
            sl = entry - config.SL_ATR * a
            tps = [entry + m_ * a for m_ in config.TP_ATRS]
        else:
            entry = price
            sl = entry + config.SL_ATR * a
            tps = [entry - m_ * a for m_ in config.TP_ATRS]
        sig = {"symbol": symbol, "tf": config.TIMEFRAME, "side": side,
               "entry": entry, "sl": sl, "tp1": tps[0], "tp2": tps[1],
               "tp3": tps[2], "tp4": tps[3], "score": bull if side == "LONG" else bear}

    return {
        "symbol": symbol, "price": price, "atr": a,
        "score_bull": bull, "score_bear": bear,
        "htf_up": bool(htf_up), "trend_up": bool(trend_up),
        "adx": float(adx_v.iloc[-1]), "rsi": float(r.iloc[-1]), "mfi": float(m.iloc[-1]),
        "rel_vol": float(rel_vol), "whale": bool(whale),
        "cvd_up": bool(cvd_up), "sqz_on": sqz_on, "sqz_rel": sqz_rel,
        "bos_bull": bool(bos_bull), "bos_bear": bool(bos_bear),
        "ema21": float(eF.iloc[-1]), "ema50": float(eS.iloc[-1]), "ema200": float(eL.iloc[-1]),
        "df": df, "signal": sig,
    }



"""Dark TradingView-style signal charts (matplotlib)."""


def _candles(ax, df):
    up = df["c"] >= df["o"]
    for col, m in (("#26a69a", up), ("#ef5350", ~up)):
        d = df[m]
        ax.bar(d.index, d["h"] - d["l"], 0.7, bottom=d["l"], color=col, zorder=2)
        ax.bar(d.index, (d["c"] - d["o"]).abs(), 0.9,
               bottom=d[["o", "c"]].min(axis=1), color=col, zorder=3)


def signal_chart(analysis, sig, path):
    df = analysis["df"].iloc[-90:].copy().reset_index(drop=True)
    a = analysis["atr"]
    entry, sl = sig["entry"], sig["sl"]
    tps = [sig["tp1"], sig["tp2"], sig["tp3"], sig["tp4"]]

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10, 6), dpi=110)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#131722")

    _candles(ax, df)
    x = df.index.to_numpy()
    ax.plot(x, ema(df["c"], 21).to_numpy(), "#2962ff", lw=1.2, label="EMA 21")
    ax.plot(x, ema(df["c"], 50).to_numpy(), "#ff6d00", lw=1.2, label="EMA 50")
    ax.plot(x, ema(df["c"], 200).to_numpy(), "#9c27b0", lw=1.2, label="EMA 200")
    st, _ = supertrend(df.reset_index(drop=True))
    ax.plot(x, st.to_numpy(), "#787b86", lw=1, ls="--", label="Supertrend")

    # entry zone
    ez = config.ENTRY_ZONE_ATR * a
    ax.axhspan(entry - ez, entry + ez, color="#2962ff", alpha=0.12)
    ax.axhline(entry, color="#2962ff", lw=1.4, ls="-.",
               label=f"Entry {entry:.4g}")
    for i, tp in enumerate(tps, 1):
        ax.axhline(tp, color="#26a69a", lw=1 if i < 4 else 1.6, ls="--",
                   label=f"TP{i} {tp:.4g}")
    ax.axhline(sl, color="#ef5350", lw=1.6, ls="--", label=f"SL {sl:.4g}")

    # signal marker on last candle
    lx = x[-1]
    ly = df["l"].iloc[-1] if sig["side"] == "LONG" else df["h"].iloc[-1]
    ax.annotate(f"{sig['side']} {sig['score']}", xy=(lx, ly),
                xytext=(0, -34 if sig["side"] == "LONG" else 30),
                textcoords="offset points", ha="center",
                fontsize=11, weight="bold", color="white",
                bbox=dict(boxstyle="round,pad=0.4",
                          fc="#26a69a" if sig["side"] == "LONG" else "#ef5350", ec="none"),
                arrowprops=dict(arrowstyle="-", color="white", lw=0.8))

    ax.set_title(f"{sig['symbol']}  {sig['tf']}  —  APEX {sig['side']}  "
                 f"score {sig['score']}/100", color="white", fontsize=13, weight="bold", loc="left")
    ax.tick_params(colors="#787b86")
    for s in ax.spines.values():
        s.set_color("#2a2e39")
    ax.legend(facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="white",
              fontsize=8, loc="upper left", ncol=2)
    fig.tight_layout()
    fig.savefig(path, facecolor="#131722")
    plt.close(fig)
    return path


def analysis_chart(analysis, path):
    """Chart without a signal — for /analyze."""
    df = analysis["df"].iloc[-90:].copy().reset_index(drop=True)
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#131722")
    _candles(ax, df)
    x = df.index.to_numpy()
    ax.plot(x, ema(df["c"], 21).to_numpy(), "#2962ff", lw=1.2, label="EMA 21")
    ax.plot(x, ema(df["c"], 50).to_numpy(), "#ff6d00", lw=1.2, label="EMA 50")
    ax.plot(x, ema(df["c"], 200).to_numpy(), "#9c27b0", lw=1.2, label="EMA 200")
    ax.set_title(f"{analysis['symbol']}  {config.TIMEFRAME}  —  "
                 f"Bull {analysis['score_bull']} / Bear {analysis['score_bear']}",
                 color="white", fontsize=13, weight="bold", loc="left")
    ax.tick_params(colors="#787b86")
    for s in ax.spines.values():
        s.set_color("#2a2e39")
    ax.legend(facecolor="#1e222d", edgecolor="#2a2e39", labelcolor="white", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, facecolor="#131722")
    plt.close(fig)
    return path


"""Telegram webhook command logic (stateless, for serverless)."""



def fmt(x):
    return f"{x:,.4g}"


def pct(a, b):
    return (b - a) / a * 100 if a else 0


def signal_text(s):
    s = dict(s)
    side_e = "🟢 LONG" if s["side"] == "LONG" else "🔴 SHORT"
    e, sl = s["entry"], s["sl"]
    tps = [s["tp1"], s["tp2"], s["tp3"], s["tp4"]]
    lines = [
        f"{side_e} | <b>{s['symbol']}</b> ({s['tf']})",
        f"✅ <b>VALIDATOR Score: {s['score']}/100</b>",
        "",
        f"🎯 Entry: <code>{fmt(e)}</code>",
        f"🛑 SL: <code>{fmt(sl)}</code> ({pct(e, sl):+.2f}%)",
        "",
    ]
    for i, tp in enumerate(tps, 1):
        lines.append(f"🎯 TP{i}: <code>{fmt(tp)}</code> ({pct(e, tp):+.2f}%)")
    lines += ["", f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    return "\n".join(lines)


def kb_main():
    rows = [
        [{"text": "📡 دوایین سیگناڵەکان", "callback_data": "signals"},
         {"text": "📊 شیکاری", "callback_data": "help_analyze"}],
        [{"text": "💎 بەشداربوون", "callback_data": "sub"},
         {"text": "🎁 ڕێفەڕاڵ", "callback_data": "ref"}],
    ]
    if config.WEBAPP_URL:
        rows.insert(0, [{"text": "🚀 کردنەوەی ئەپ", "web_app": {"url": config.WEBAPP_URL}}])
    return {"inline_keyboard": rows}


def cmd_start(chat_id, user, args):
    ref_by = 0
    if args and args[0].startswith("ref_"):
        try:
            ref_by = int(args[0][4:])
            if ref_by == user["id"]:
                ref_by = 0
        except ValueError:
            pass
    register(user["id"], user.get("username", ""), user.get("first_name", ""), ref_by)
    send_message(chat_id,
        f"بەخێربێیت {user.get('first_name','')}! 👋\n\n"
        "🤖 <b>APEX Signal Bot</b> — وەک GGShot:\n"
        "• سیگناڵی LONG/SHORT لەگەڵ Entry و 4 TP و SL\n"
        "• ✅ VALIDATOR Score بۆ هەر سیگناڵێک\n"
        "• 📈 چارت لەگەڵ هەر سیگناڵێک\n"
        "• 📊 ئاماری win-rate و ڕۆژنامەی ترەید\n\n"
        "فەرمانەکان: /signals /analyze /stats /journal /subscribe /referral",
        reply_markup=kb_main())


def cmd_signals(chat_id):
    for s in latest_signals(5):
        send_message(chat_id, signal_text(s))
    if not latest_signals(1):
        send_message(chat_id, "هێشتا سیگناڵ نییە ⏳")


def cmd_analyze(chat_id, args):
    if not args:
        send_message(chat_id, "نموونە: <code>/analyze NEARUSDT</code>")
        return
    sym = args[0].upper().replace("/", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    send_message(chat_id, f"⏳ شیکاری {sym}...")
    try:
        a = analyze(sym)
    except Exception as e:
        send_message(chat_id, f"❌ هەڵە: {e}")
        return
    if not a:
        send_message(chat_id, "❌ داتا بەش نەبوو")
        return
    path = f"/tmp/apex_{sym}.png"
    try:
        analysis_chart(a, path)
        bull, bear = a["score_bull"], a["score_bear"]
        verdict = "🟢 بەهێزە بۆ LONG" if bull >= 80 else \
                  "🔴 بەهێزە بۆ SHORT" if bear >= 80 else "⏳ چاوەڕوانبە — سیگناڵ نییە"
        cap = (f"📊 <b>{sym}</b> — <b>{fmt(a['price'])}</b>\n{verdict}\n\n"
               f"🐂 Bull: <b>{bull}/100</b> | 🐻 Bear: <b>{bear}/100</b>\n"
               f"RSI {a['rsi']:.0f} | MFI {a['mfi']:.0f} | ADX {a['adx']:.0f} | Vol ×{a['rel_vol']:.1f}")
        send_photo(chat_id, path, caption=cap)
    except Exception as e:
        send_message(chat_id, f"❌ هەڵە لە چارت: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)


def cmd_stats(chat_id):
    s = stats_overall()
    send_message(chat_id,
        "📊 <b>ئاماری گشتی بۆت</b>\n\n"
        f"🏆 Win-rate: <b>{s['winrate']}%</b>\n"
        f"✅ براوە: {s['wins']} | ❌ دۆڕاو: {s['losses']}\n"
        f"⌛ بەسەرچوو: {s['expired']} | 📡 کۆی سیگناڵ: {s['total']}")


def cmd_journal(chat_id, uid):
    rows = journal_list(uid)
    st = journal_stats(uid)
    if not rows:
        send_message(chat_id,
            "ڕۆژنامەکەت بەتاڵە 📝\nنموونە:\n<code>/log NEARUSDT LONG 4.84 4.52 5.58 12.5</code>")
        return
    lines = [f"📝 <b>ڕۆژنامەکەت</b> — win-rate: {st['winrate']}% | کۆی: {st['total_pct']:+.1f}%\n"]
    for r in rows:
        r = dict(r)
        e = "🟢" if r["side"] == "LONG" else "🔴"
        lines.append(f"{e} {r['symbol']} {r['result_pct']:+.1f}%")
    send_message(chat_id, "\n".join(lines))


def cmd_log(chat_id, uid, args):
    try:
        sym, side = args[0].upper(), args[1].upper()
        entry, sl, tp, res = map(float, args[2:6])
        note = " ".join(args[6:])
        assert side in ("LONG", "SHORT")
    except (IndexError, ValueError, AssertionError):
        send_message(chat_id, "نموونە:\n<code>/log NEARUSDT LONG 4.84 4.52 5.58 12.5 تێبینی</code>")
        return
    journal_add(uid, sym, side, entry, sl, tp, res, note)
    send_message(chat_id, "✅ تۆمارکرا")


def cmd_subscribe(chat_id, uid):
    u = user(uid)
    plan = "💎 VIP" if is_vip(u) else "🆓 Free"
    send_message(chat_id,
        f"💎 <b>بەشداربوون</b> — پلانی تۆ: {plan}\n\n"
        f"🆓 Free: {config.FREE_SIGNALS_PER_DAY} سیگناڵ/ڕۆژ\n"
        "💎 VIP: سیگناڵی بێسنوور\n\n"
        f"🎁 {config.REFS_FOR_VIP} ڕێفەڕاڵ = {config.VIP_DAYS_PER_REF_MILESTONE} ڕۆژ VIP بەخۆڕایی!")


def cmd_referral(chat_id, uid):
    u = user(uid)
    bot_un = os.environ.get("BOT_USERNAME", "ggkurdbot")
    link = f"https://t.me/{bot_un}?start=ref_{uid}"
    send_message(chat_id,
        f"🎁 <b>ڕێفەڕاڵ</b>\n\nلینکەکەت:\n<code>{link}</code>\n\n"
        f"👥 ڕێفەڕاڵەکانت: <b>{u['ref_count'] if u else 0}</b>")


def on_callback(chat_id, cq):
    data = cq.get("data", "")
    answer_callback(cq["id"])
    if data == "signals":
        cmd_signals(chat_id)
    elif data == "sub":
        cmd_subscribe(chat_id, cq["from"]["id"])
    elif data == "ref":
        cmd_referral(chat_id, cq["from"]["id"])
    elif data == "help_analyze":
        send_message(chat_id, "نموونە: <code>/analyze BTCUSDT</code>")


def handle_update(update):
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        register(cq["from"]["id"], cq["from"].get("username", ""),
                    cq["from"].get("first_name", ""))
        on_callback(chat_id, cq)
        return
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    register(user["id"], user.get("username", ""), user.get("first_name", ""))
    text = msg["text"].strip()
    if "@" in text.split()[0]:
        text = text.split()[0].split("@")[0] + " " + " ".join(text.split()[1:])
        text = text.strip()
    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "/start":
        cmd_start(chat_id, user, args)
    elif cmd == "/signals":
        cmd_signals(chat_id)
    elif cmd == "/analyze":
        cmd_analyze(chat_id, args)
    elif cmd == "/stats":
        cmd_stats(chat_id)
    elif cmd == "/journal":
        cmd_journal(chat_id, user["id"])
    elif cmd == "/log":
        cmd_log(chat_id, user["id"], args)
    elif cmd == "/subscribe":
        cmd_subscribe(chat_id, user["id"])
    elif cmd == "/referral":
        cmd_referral(chat_id, user["id"])



"""Scanner: analyze symbols, emit signals, track outcomes. Used by GitHub Actions."""



def scan_once():
    print("scan start", flush=True)
    try:
        symbols = top_usdt_symbols(config.TOP_N_BY_VOLUME)
    except Exception as e:
        print("top symbols failed:", e, flush=True)
        return
    for sym in symbols:
        try:
            a = analyze(sym)
        except Exception as e:
            print(sym, "analyze failed:", e, flush=True)
            continue
        if not a:
            continue
        try:
            upsert_screen(sym, a["score_bull"], a["score_bear"], a["price"], a["rsi"])
        except Exception as e:
            print("screener upsert failed:", e, flush=True)
        if not a["signal"]:
            continue
        s = a["signal"]
        if recent_signal(sym, s["side"], config.SIGNAL_COOLDOWN_H):
            continue
        sid = save_signal(s)
        # chart
        path = f"/tmp/apex_sig_{sid}.png"
        try:
            signal_chart(a, s, path)
        except Exception as e:
            print("chart failed:", e, flush=True)
            path = None
        sent = 0
        for uid in all_user_ids():
            if not can_receive(uid):
                continue
            try:
                if path:
                    send_photo(uid, path, caption=signal_text(s))
                else:
                    send_message(uid, signal_text(s))
                mark_delivered(uid)
                record_delivery(sid, uid)
                sent += 1
            except Exception as e:
                print("send failed:", uid, str(e)[:100], flush=True)
            time.sleep(0.4)
        print(f"signal {sym} {s['side']} -> {sent} users", flush=True)


def track_outcomes():
    print("outcome check start", flush=True)
    for row in open_signals():
        s = dict(row)
        try:
            df = klines(s["symbol"], s["tf"], 200)
        except Exception:
            continue
        df = df[df["ct"] > s["created"] * 1000]
        if df.empty:
            if time.time() - s["created"] > 7 * 86400:
                update_signal(s["id"], "expired")
            continue
        tps = [s["tp1"], s["tp2"], s["tp3"], s["tp4"]]
        hit = None
        for _, r_ in df.iterrows():
            if s["side"] == "LONG":
                ttp = next((f"tp{i}" for i, tp in enumerate(tps, 1) if r_["h"] >= tp), None)
                tsl = r_["l"] <= s["sl"]
            else:
                ttp = next((f"tp{i}" for i, tp in enumerate(tps, 1) if r_["l"] <= tp), None)
                tsl = r_["h"] >= s["sl"]
            if ttp and (hit is None or ttp > hit):
                hit = ttp
            elif tsl and hit is None:
                hit = "sl"
                break
        if hit:
            update_signal(s["id"], hit)
            print(f"signal {s['id']} -> {hit}", flush=True)
            e = "🎯" if hit.startswith("tp") else "🛑"
            label = {"tp1": "TP1 ✅", "tp2": "TP2 ✅✅", "tp3": "TP3 ✅✅✅",
                     "tp4": "TP4 🎯🎯🎯🎯", "sl": "SL ❌"}.get(hit, hit)
            for uid in signal_recipients(s["id"]):
                try:
                    if get_prefs(uid).get("tp_sl_reports", True):
                        send_message(
                            uid, f"{e} <b>{s['symbol']}</b> {s['side']} → <b>{label}</b>")
                except Exception:
                    pass
                time.sleep(0.3)



"""Vercel entrypoint: single FastAPI app mounted at /api/*.

Routes are defined WITHOUT the /api prefix — Vercel mounts this app at /api.
"""





@asynccontextmanager
async def lifespan(app):
    try:
        init()
    except Exception as e:
        print("db init failed at startup:", str(e)[:200])
    yield


app = FastAPI(lifespan=lifespan)


# ── Telegram WebApp initData auth ────────────────────────────
def validate_init_data(init_data):
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    except Exception:
        return None
    d = dict(pairs)
    received = d.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        return json.loads(d.get("user", "{}"))
    except Exception:
        return None


async def authed_user(request: Request):
    init_data = request.headers.get("x-telegram-init-data", "")
    if not init_data:
        init_data = request.query_params.get("initData", "")
    tg_user = validate_init_data(init_data) if init_data else None
    if not tg_user or "id" not in tg_user:
        raise HTTPException(401, "bad initData")
    uid = int(tg_user["id"])
    register(uid, tg_user.get("username", ""), tg_user.get("first_name", ""))
    return uid, tg_user


def sig_dict(s):
    s = dict(s)
    return {k: s[k] for k in
            ("id", "symbol", "tf", "side", "entry", "sl", "tp1", "tp2", "tp3", "tp4",
             "score", "created", "status")}


@app.get("/api/me")
async def me(request: Request):
    uid, tg = await authed_user(request)
    u = user(uid)
    return {"id": uid, "name": get("first_name", ""),
            "plan": "vip" if is_vip(u) else "free",
            "vip_until": u["vip_until"] if u else 0,
            "ref_count": u["ref_count"] if u else 0,
            "prefs": get_prefs(uid)}


@app.post("/api/prefs")
async def prefs(request: Request):
    uid, _ = await authed_user(request)
    body = await request.json()
    key, val = body.get("key"), body.get("value")
    if key in ("tp_sl_reports", "market_analysis", "abnormal_signals"):
        set_pref(uid, key, val)
    return {"prefs": get_prefs(uid)}


@app.get("/api/signals")
async def signals(request: Request):
    await authed_user(request)
    return [sig_dict(s) for s in latest_signals(20)]


@app.get("/api/stats")
async def stats(request: Request):
    await authed_user(request)
    closed = [sig_dict(dict(r)) for r in latest_signals(50) if dict(r)["status"] != "open"][:10]
    return {"overall": stats_overall(), "recent": closed}


@app.get("/api/screener")
async def screener(request: Request):
    await authed_user(request)
    return [dict(r) for r in screener_top(30)]


@app.get("/api/chart/{sid}")
async def chart(sid: int, request: Request):
    await authed_user(request)
    s = get_signal(sid)
    if not s:
        raise HTTPException(404)
    s = dict(s)
    import time
    path = f"/tmp/apex_web_{sid}.png"
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 3600:
        try:
            a = analyze(s["symbol"])
            signal_chart(a, s, path)
        except Exception:
            raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.post("/api/paper")
async def paper(request: Request):
    uid, _ = await authed_user(request)
    body = await request.json()
    s = get_signal(int(body.get("signal_id", 0)))
    if not s:
        raise HTTPException(404, "signal not found")
    pid = paper_open(uid, dict(s), float(body.get("qty_usd", 100)))
    return {"paper_id": pid}


@app.get("/api/portfolio")
async def portfolio(request: Request):
    uid, _ = await authed_user(request)
    out = []
    for t in paper_list(uid, "open"):
        t = dict(t)
        try:
            px = klines(t["symbol"], "1h", 2)["c"].iloc[-1]
        except Exception:
            px = t["entry"]
        pnl = (px - t["entry"]) / t["entry"] * 100
        if t["side"] == "SHORT":
            pnl = -pnl
        t["live_price"] = float(px)
        t["pnl_pct"] = round(pnl, 2)
        out.append(t)
    closed = [dict(t) for t in paper_list(uid, "closed")]
    return {"open": out, "closed": closed[-10:]}


@app.post("/api/paper-close")
async def paper_close(request: Request):
    uid, _ = await authed_user(request)
    body = await request.json()
    pid = int(body.get("id", 0))
    rows = [dict(t) for t in paper_list(uid, "open") if dict(t)["id"] == pid]
    if not rows:
        raise HTTPException(404)
    t = rows[0]
    try:
        px = klines(t["symbol"], "1h", 2)["c"].iloc[-1]
    except Exception:
        px = t["entry"]
    pnl = (px - t["entry"]) / t["entry"] * 100
    if t["side"] == "SHORT":
        pnl = -pnl
    paper_close(pid, round(pnl, 2))
    return {"pnl_pct": round(pnl, 2)}


@app.post("/api/telegram")
async def telegram_webhook(request: Request):
    update = await request.json()
    try:
        handle_update(update)
    except Exception as e:
        print("webhook error:", e)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True}

# --- Serve the Mini App (index.html) from the function itself ---
@app.get("/", include_in_schema=False)
def _serve_index():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.normpath(os.path.join(here, "..", "index.html")),
        os.path.normpath(os.path.join(os.getcwd(), "index.html")),
        "/var/task/index.html",
    ]
    for p in candidates:
        if os.path.exists(p):
            return FileResponse(p, media_type="text/html; charset=utf-8")
    return JSONResponse({"detail": "index.html not bundled"}, status_code=500)

