"""APEX TradingView-style charts via lightweight-charts + headless Chromium.

Used by the GitHub Actions scanner (Telegram signal + TP-reply charts).
Falls back gracefully when playwright/Chromium is unavailable.
"""
import base64
import io
import json
import os
import time
import urllib.request

_CACHE_DIR = "/tmp/apex_tv"
_LWC_URL = "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"

UP = "#26a69a"
DN = "#ef5350"
GRY = "#787b86"


def _ensure_lwc():
    os.makedirs(_CACHE_DIR, exist_ok=True)
    p = os.path.join(_CACHE_DIR, "lwc.js")
    if not os.path.exists(p) or os.path.getsize(p) < 50000:
        req = urllib.request.Request(_LWC_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(p, "wb") as f:
            f.write(data)
    return p


def coin_icon_b64(symbol):
    """Fetch coin icon (base64 PNG data URI) with local cache. None on failure."""
    base = symbol.upper().replace("USDT", "").replace("BUSD", "").lower()
    cp = os.path.join(_CACHE_DIR, f"icon_{base}.b64")
    if os.path.exists(cp):
        try:
            return open(cp).read().strip() or None
        except Exception:
            pass
    urls = [
        f"https://assets.coincap.io/assets/icons/{base}@2x.png",
    ]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) > 500 and r.status == 200:
                b64 = base64.b64encode(data).decode()
                try:
                    open(cp, "w").write(b64)
                except Exception:
                    pass
                return b64
        except Exception:
            continue
    return None


def _candles_json(df):
    """DataFrame with o/h/l/c/ct(ms) -> lightweight-charts candle list."""
    out = []
    for _, r in df.iterrows():
        ts = int(r["ct"]) // 1000
        out.append({"time": ts, "open": round(float(r["o"]), 6),
                    "high": round(float(r["h"]), 6), "low": round(float(r["l"]), 6),
                    "close": round(float(r["c"]), 6)})
    return out


def render_tv_chart(df, sig, out_path, hits=None, callout=None, width=1280, height=720,
                      sig_time=None):
    """Render a TradingView chart PNG.

    df: candles DataFrame (o,h,l,c,ct in ms), sig: signal dict with
        symbol/tf/side/entry/sl/tp1..tp4/score.
    hits: list of hit TP numbers, e.g. [1,2] (for reply charts).
    callout: e.g. "Long Printed\\n+3.62%" (for reply charts).
    """
    from playwright.sync_api import sync_playwright

    lwc_path = _ensure_lwc()
    with open(lwc_path) as f:
        lwc_js = f.read()

    candles = _candles_json(df.tail(90))
    is_long = sig["side"] == "LONG"
    col = UP if is_long else DN
    entry, sl = float(sig["entry"]), float(sig["sl"])
    tps = [float(sig[f"tp{i}"]) for i in range(1, 5)]
    icon = coin_icon_b64(sig["symbol"])
    icon_html = (f'<img src="data:image/png;base64,{icon}" '
                 'style="width:18px;height:18px;vertical-align:-3px;border-radius:50%"> '
                 if icon else '<span style="color:#7b3ff2">\u25cf</span> ')

    # trailing stop series (running min low / max high from signal candle)
    times = [c["time"] for c in candles]
    if sig_time is None:
        sig_time = times[-1]
    else:
        # snap to closest candle at/after the signal time
        sig_time = min([t for t in times if t >= sig_time] or [times[-1]])
    tr = []
    started = False
    if is_long:
        rm = float("inf")
        for c_ in candles:
            if c_["time"] >= sig_time:
                started = True
            if started:
                rm = min(rm, c_["low"])
            tr.append({"time": c_["time"],
                       "value": round(rm if rm != float("inf") else c_["low"], 6)})
    else:
        rm = float("-inf")
        for c_ in candles:
            if c_["time"] >= sig_time:
                started = True
            if started:
                rm = max(rm, c_["high"])
            tr.append({"time": c_["time"],
                       "value": round(rm if rm != float("-inf") else c_["high"], 6)})

    # markers
    markers = [{"time": sig_time, "position": "belowBar" if is_long else "aboveBar",
                "color": col, "shape": "arrowUp" if is_long else "arrowDown",
                "text": sig["side"]}]
    if hits:
        for i in hits:
            tp = tps[i - 1]
            hx = None
            for c_ in candles:
                if c_["time"] < sig_time:
                    continue
                if (is_long and c_["high"] >= tp) or (not is_long and c_["low"] <= tp):
                    hx = c_["time"]; break
            if hx:
                markers.append({"time": hx, "position": "aboveBar" if is_long else "belowBar",
                                "color": col, "shape": "arrowDown" if is_long else "arrowUp",
                                "text": f"TP{i}"})

    title2 = f"APEX {sig['symbol']} - {sig['tf']} | VALIDATOR {sig['score']}/100"
    if callout:
        title2 = f"APEX {sig['symbol']} - {sig['tf']} | {callout.split(chr(10))[0]}"
    callout_html = ""
    if callout:
        txt = callout.replace("\n", "<br>")
        callout_html = (f'<div id="callout">{txt}</div>')

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#000;overflow:hidden}}
#tv{{width:{width}px;height:{height}px;position:relative}}
#title{{position:absolute;top:8px;left:12px;color:#d1d4dc;font:13px -apple-system,system-ui,sans-serif;z-index:5;pointer-events:none}}
#sub{{position:absolute;top:30px;left:12px;color:#787b86;font:11px -apple-system,system-ui,sans-serif;z-index:5;pointer-events:none}}
#callout{{position:absolute;top:64px;left:56%;transform:translateX(-50%);background:{col};color:#fff;
 font:bold 13px -apple-system,system-ui,sans-serif;text-align:center;padding:10px 18px;border-radius:8px;z-index:5;pointer-events:none;line-height:1.4}}
#callout:after{{content:'';position:absolute;left:50%;bottom:-9px;transform:translateX(-50%);
 border-left:9px solid transparent;border-right:9px solid transparent;border-top:10px solid {col}}}
</style></head><body><div id="tv"></div>
<div id="title">{icon_html}{sig['symbol'][:-4] if sig['symbol'].endswith('USDT') else sig['symbol']} / TetherUS SPOT &middot; {sig['tf']} &middot; Binance</div>
<div id="sub">{title2}</div>
{callout_html}
<script>{lwc_js}</script>
<script>
const chart = LightweightCharts.createChart(document.getElementById('tv'), {{
  width: {width}, height: {height},
  layout: {{ background: {{ type: 'solid', color: '#000000' }}, textColor: '#d1d4dc',
            fontFamily: "-apple-system, system-ui, sans-serif" }},
  grid: {{ vertLines: {{ visible: false }}, horzLines: {{ visible: false }} }},
  timeScale: {{ timeVisible: true, borderColor: '#1e222d' }},
  rightPriceScale: {{ borderColor: '#1e222d' }},
}});
const cs = chart.addCandlestickSeries({{ upColor: '{UP}', downColor: '{DN}',
  borderVisible: false, wickUpColor: '{UP}', wickDownColor: '{DN}' }});
cs.setData({json.dumps(candles)});
const DASH = LightweightCharts.LineStyle.Dashed;
const TPS = {json.dumps([round(t, 6) for t in tps])};
const NAMES = ['APEX:Take Profit','APEX:Take Profit 2','APEX:Take Profit 3','APEX:Take Profit 4'];
TPS.forEach((p, i) => cs.createPriceLine({{ price: p, color: '{col}', lineWidth: 1,
  lineStyle: DASH, axisLabelVisible: true, title: NAMES[i] }}));
cs.createPriceLine({{ price: {sl:.6f}, color: '{DN}', lineWidth: 1,
  lineStyle: DASH, axisLabelVisible: true, title: 'APEX:Stop Loss' }});
const tr = chart.addLineSeries({{ color: '{col}', lineWidth: 2,
  priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }});
tr.setData({json.dumps(tr)});
cs.setMarkers({json.dumps(markers)});
chart.timeScale().fitContent();
window.__done = true;
</script></body></html>"""
    hp = os.path.join(_CACHE_DIR, "chart.html")
    with open(hp, "w") as f:
        f.write(html)

    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        pg = b.new_page(viewport={"width": width, "height": height})
        pg.goto("file://" + hp)
        pg.wait_for_function("window.__done === true", timeout=20000)
        pg.wait_for_timeout(800)
        pg.screenshot(path=out_path)
        b.close()
    return out_path
