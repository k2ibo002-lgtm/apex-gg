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
        out.append({"time": ts, "open": round(float(r["o"]), 10),
                    "high": round(float(r["h"]), 10), "low": round(float(r["l"]), 10),
                    "close": round(float(r["c"]), 10)})
    return out


def _supertrend(df, atr_period=10, mult=3.0):
    """Supertrend: list of (value, trend) with trend=1 (up/green) or -1 (down/red)."""
    h = [float(x) for x in df["h"]]
    l = [float(x) for x in df["l"]]
    c = [float(x) for x in df["c"]]
    n = len(df)
    # True Range + Wilder ATR
    tr = [h[0] - l[0]]
    for i in range(1, n):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    atr = [tr[0]]
    for i in range(1, n):
        atr.append((atr[-1] * (atr_period - 1) + tr[i]) / atr_period)
    hl2 = [(h[i] + l[i]) / 2 for i in range(n)]
    upper = [hl2[i] + mult * atr[i] for i in range(n)]
    lower = [hl2[i] - mult * atr[i] for i in range(n)]
    f_up, f_lo = [upper[0]], [lower[0]]
    trend = [1]
    st = [lower[0]]
    for i in range(1, n):
        f_up.append(upper[i] if (upper[i] < f_up[-1] or c[i-1] > f_up[-1]) else f_up[-1])
        f_lo.append(lower[i] if (lower[i] > f_lo[-1] or c[i-1] < f_lo[-1]) else f_lo[-1])
        if trend[-1] == 1:
            t = 1 if c[i] > f_lo[-1] else -1
        else:
            t = -1 if c[i] < f_up[-1] else 1
        trend.append(t)
        st.append(f_lo[-1] if t == 1 else f_up[-1])
    return list(zip(st, trend))


def _px_precision(px):
    import math
    try:
        px = float(px)
    except Exception:
        return 2
    if px <= 0:
        return 2
    return max(2, min(10, -math.floor(math.log10(px)) + 2))


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
    entry, sl = float(sig["entry"]), float(sig["sl"])
    pxp = _px_precision(entry)
    min_move = round(10 ** (-pxp), pxp)
    is_long = sig["side"] == "LONG"
    col = UP if is_long else DN
    tps = [float(sig[f"tp{i}"]) for i in range(1, 5)]
    icon = coin_icon_b64(sig["symbol"])
    icon_html = (f'<img src="data:image/png;base64,{icon}" '
                 'style="width:18px;height:18px;vertical-align:-3px;border-radius:50%"> '
                 if icon else '<span style="color:#7b3ff2">\u25cf</span> ')

    # Supertrend trailing line (GGShot style): green in uptrend, red in downtrend.
    times = [c["time"] for c in candles]
    if sig_time is None:
        sig_time = times[-1]
    else:
        # snap to closest candle at/after the signal time
        sig_time = min([t for t in times if t >= sig_time] or [times[-1]])
    _st = _supertrend(df.tail(90))
    tr = []
    for c_, (sv, td) in zip(candles, _st):
        tr.append({"time": c_["time"], "value": round(sv, 10), "trend": td})
    st_last = tr[-1]["value"] if tr else sl

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

    # Supertrend fill: green band in uptrend, red band in downtrend (GGShot style).
    # Built as a plain string (NOT f-string) to avoid brace-escaping issues.
    # Deferred via setTimeout so the time scale layout is complete before
    # calling timeToCoordinate (otherwise x coords are wrong).
    _svg_js = """
setTimeout(() => {
(function(){
  const svgNS = 'http://www.w3.org/2000/svg';
  const _svg = document.createElementNS(svgNS, 'svg');
  _svg.setAttribute('width', __W__);
  _svg.setAttribute('height', __H__);
  _svg.style.cssText = 'position:absolute;left:0;top:0;z-index:3;pointer-events:none';
  const _trMap = {}, _tdMap = {};
  TRD.forEach(p => { _trMap[p.time] = p.value; _tdMap[p.time] = p.trend; });
  const segs = [];
  let cur = null;
  CANDLES.forEach(c_ => {
    const sv = _trMap[c_.time], td = _tdMap[c_.time];
    if (sv === undefined || td === undefined) return;
    const x = chart.timeScale().timeToCoordinate(c_.time);
    let yT, yB;
    if (td === 1) {
      yT = cs.priceToCoordinate(c_.low);
      yB = stUp.priceToCoordinate(sv);
    } else {
      yT = stDn.priceToCoordinate(sv);
      yB = cs.priceToCoordinate(c_.high);
    }
    if (x == null || yT == null || yB == null) return;
    if (!cur || cur.trend !== td) { cur = {trend: td, pts: []}; segs.push(cur); }
    cur.pts.push([x, yT, yB]);
  });
  segs.forEach(sg => {
    if (sg.pts.length < 2) return;
    const _top = sg.pts.map(p => p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ');
    const _bot = sg.pts.slice().reverse().map(p => p[0].toFixed(1)+','+p[2].toFixed(1)).join(' ');
    const _poly = document.createElementNS(svgNS, 'polygon');
    _poly.setAttribute('points', _top + ' ' + _bot);
    _poly.setAttribute('fill', sg.trend === 1 ? 'rgba(38,166,154,0.16)' : 'rgba(239,83,80,0.14)');
    _svg.appendChild(_poly);
  });
  document.getElementById('tv').appendChild(_svg);
  // Trailing Stop Loss label (GGShot style, gray pill)
  const _st = TRD[TRD.length-1];
  const _sy = ( _st.trend === 1 ? stUp : stDn ).priceToCoordinate(_st.value);
  if (_sy != null) {
    const _d = document.createElement('div');
    _d.className = 'plabel st';
    _d.style.top = _sy + 'px';
    _d.innerHTML = 'GG KURD:Trailing Stop Loss <b>' + _st.value.toFixed(__PXP__) + '</b>';
    document.getElementById('tv').appendChild(_d);
  }
})();
  window.__done = true;
}, 120);
""".replace("__W__", str(width)).replace("__H__", str(height)).replace("__PXP__", str(pxp))

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#000;overflow:hidden}}
#tv{{width:{width}px;height:{height}px;position:relative}}
#title{{position:absolute;top:8px;left:12px;color:#d1d4dc;font:13px -apple-system,system-ui,sans-serif;z-index:5;pointer-events:none}}
#sub{{position:absolute;top:30px;left:12px;color:#787b86;font:11px -apple-system,system-ui,sans-serif;z-index:5;pointer-events:none}}
#callout{{position:absolute;top:64px;left:56%;transform:translateX(-50%);background:{col};color:#fff;
 font:bold 13px -apple-system,system-ui,sans-serif;text-align:center;padding:10px 18px;border-radius:8px;z-index:5;pointer-events:none;line-height:1.4}}
#callout:after{{content:'';position:absolute;left:50%;bottom:-9px;transform:translateX(-50%);
 border-left:9px solid transparent;border-right:9px solid transparent;border-top:10px solid {col}}}
.plabel{{position:absolute;right:78px;transform:translateY(-50%);color:#fff;
 font:600 12px -apple-system,system-ui,sans-serif;padding:4px 10px;border-radius:5px;
 z-index:6;pointer-events:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.5)}}
.plabel b{{font-weight:800;margin-left:8px}}
.plabel.tp{{background:#26a69a}}
.plabel.sl{{background:#ef5350}}
.plabel.st{{background:#787b86}}
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
  timeScale: {{ timeVisible: true, borderColor: '#1e222d', rightOffset: 28 }},
  rightPriceScale: {{ borderColor: '#1e222d' }},
}});
const cs = chart.addCandlestickSeries({{ upColor: 'transparent', downColor: 'transparent',
  borderVisible: true, borderUpColor: '{UP}', borderDownColor: '{DN}',
  wickUpColor: '{UP}', wickDownColor: '{DN}',
  priceFormat: {{ type: 'price', precision: {pxp}, minMove: {min_move} }} }});
const CANDLES = {json.dumps(candles)};
cs.setData(CANDLES);
const TRD = {json.dumps(tr)};
const DASH = LightweightCharts.LineStyle.Dashed;
const TPS = {json.dumps([round(t, 10) for t in tps])};
const NAMES = ['APEX:Take Profit','APEX:Take Profit 2','APEX:Take Profit 3','APEX:Take Profit 4'];
TPS.forEach((p) => cs.createPriceLine({{ price: p, color: '{col}', lineWidth: 1,
  lineStyle: DASH, axisLabelVisible: false, title: '' }}));
cs.createPriceLine({{ price: {sl:.10f}, color: '{DN}', lineWidth: 1,
  lineStyle: DASH, axisLabelVisible: false, title: '' }});
const stUp = chart.addLineSeries({{ color: '#26a69a', lineWidth: 2,
  priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }});
const stDn = chart.addLineSeries({{ color: '#ef5350', lineWidth: 2,
  priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }});
const upPts = [], dnPts = [];
TRD.forEach((p) => {{
  const pt = {{time: p.time, value: p.value}};
  if (p.trend === 1) upPts.push(pt); else dnPts.push(pt);
}});
stUp.setData(upPts);
stDn.setData(dnPts);
{_svg_js}
cs.setMarkers({json.dumps(markers)});
chart.timeScale().fitContent();
// ---- GGShot-style spaced TP/SL labels (custom overlay, collision-free) ----
const LEVELS = [
  {{ price: TPS[3], name: 'Take Profit 4', cls: 'tp' }},
  {{ price: TPS[2], name: 'Take Profit 3', cls: 'tp' }},
  {{ price: TPS[1], name: 'Take Profit 2', cls: 'tp' }},
  {{ price: TPS[0], name: 'Take Profit',   cls: 'tp' }},
  {{ price: {sl:.10f}, name: 'Stop Loss',   cls: 'sl' }},
];
const MIN_GAP = 55;
let _ys = LEVELS.map(l => cs.priceToCoordinate(l.price));
for (let _i = 0; _i < _ys.length; _i++) if (_ys[_i] === null) _ys[_i] = 0;
for (let _i = 1; _i < _ys.length; _i++) {{
  if (_ys[_i] - _ys[_i-1] < MIN_GAP) _ys[_i] = _ys[_i-1] + MIN_GAP;
}}
const _maxY = {height} - 16;
if (_ys[_ys.length-1] > _maxY) {{
  const _sh = _ys[_ys.length-1] - _maxY;
  _ys = _ys.map(y => y - _sh);
}}
const _tv = document.getElementById('tv');
LEVELS.forEach((l, _i) => {{
  const _d = document.createElement('div');
  _d.className = 'plabel ' + l.cls;
  _d.style.top = _ys[_i] + 'px';
  _d.innerHTML = 'GG KURD:' + l.name + ' <b>' + l.price.toFixed({pxp}) + '</b>';
  _tv.appendChild(_d);
}});
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
