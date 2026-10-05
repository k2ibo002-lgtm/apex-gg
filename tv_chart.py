"""APEX TradingView-style charts via lightweight-charts + headless Chromium.

Used by the GitHub Actions scanner (Telegram signal + TP-reply charts).
Falls back gracefully when playwright/Chromium is unavailable.

VERSION: 2026-10-05-1505-SVGDOTTED (pale limited-extent TP lines via SVG)
If chart shows full-width dotted TP lines, this version is NOT deployed.
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
        f"https://cryptoicons.org/api/icon/{base}/200",
        f"https://s2.coinmarketcap.com/static/img/coins/64x64/{base}.png",
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
    """Supertrend matching api/index.py exactly (same values as the scorer uses).
    Returns list of (value, trend) with trend=1 (up/green) or -1 (down/red)."""
    h = [float(x) for x in df["h"]]
    l = [float(x) for x in df["l"]]
    c = [float(x) for x in df["c"]]
    n = len(df)
    # TR + Wilder ATR (ewm alpha=1/n, adjust=False)
    tr = [h[0] - l[0]]
    for i in range(1, n):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    atr = [tr[0]]
    for i in range(1, n):
        atr.append((atr[-1] * (atr_period - 1) + tr[i]) / atr_period)
    hl2 = [(h[i] + l[i]) / 2 for i in range(n)]
    up = [hl2[i] + mult * atr[i] for i in range(n)]
    lo = [hl2[i] - mult * atr[i] for i in range(n)]
    # d: 1 = down, -1 = up (Pine convention, same as api/index.py)
    d = [1] * n
    st = [0.0] * n
    for i in range(1, n):
        if d[i-1] == -1:
            lo[i] = max(lo[i], lo[i-1])
        else:
            up[i] = min(up[i], up[i-1])
        if c[i] > up[i-1]:
            d[i] = -1
        elif c[i] < lo[i-1]:
            d[i] = 1
        else:
            d[i] = d[i-1]
            if d[i] == -1 and lo[i] < lo[i-1]:
                lo[i] = lo[i-1]
            if d[i] == 1 and up[i] > up[i-1]:
                up[i] = up[i-1]
        st[i] = lo[i] if d[i] == -1 else up[i]
    st[0] = lo[0]
    # convert to trend=1 (up) / -1 (down)
    return [(st[i], 1 if d[i] == -1 else -1) for i in range(n)]


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
                      sig_time=None, show_get_ready=False):
    """Render a TradingView chart PNG.

    df: candles DataFrame (o,h,l,c,ct in ms), sig: signal dict with
        symbol/tf/side/entry/sl/tp1..tp4/score.
    hits: list of hit TP numbers, e.g. [1,2] (for reply charts).
    callout: e.g. "Long Printed\\n+3.62%" (for reply charts).
    show_get_ready: if True, show "Get ready to long/short" pill before signal.
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
    # Pre-calculate label Y positions in Python (linear price->pixel mapping)
    # Use ONLY candle range (like lightweight-charts auto-scale, price lines don't affect it)
    _df90 = df.tail(90)
    _pmin = float(_df90["l"].min())
    _pmax = float(_df90["h"].max())
    _prange = _pmax - _pmin if _pmax > _pmin else 1.0
    def _py(price):
        return 60 + (_pmax - float(price)) / _prange * (height - 120)
    # Label levels with precomputed Y (top-to-bottom order)
    if is_long:
        _lvls = [(tps[3], 'Take Profit 4'), (tps[2], 'Take Profit 3'),
                 (tps[1], 'Take Profit 2'), (tps[0], 'Take Profit'), (sl, 'Stop Loss')]
    else:
        _lvls = [(sl, 'Stop Loss'), (tps[0], 'Take Profit'),
                 (tps[1], 'Take Profit 2'), (tps[2], 'Take Profit 3'), (tps[3], 'Take Profit 4')]
    _lvls_y = []
    for _pr, _nm in _lvls:
        _lvls_y.append({"price": round(float(_pr), 10), "name": _nm,
                        "cls": "sl" if "Stop" in _nm else "tp",
                        "y": round(_py(_pr), 1)})
    # Apply MIN_GAP collision avoidance in Python
    _MIN_GAP = 55
    for _i in range(1, len(_lvls_y)):
        if _lvls_y[_i]["y"] - _lvls_y[_i-1]["y"] < _MIN_GAP:
            _lvls_y[_i]["y"] = round(_lvls_y[_i-1]["y"] + _MIN_GAP, 1)
    # Keep within chart
    if _lvls_y[-1]["y"] > height - 16:
        _sh = _lvls_y[-1]["y"] - (height - 16)
        for _l in _lvls_y:
            _l["y"] = round(_l["y"] - _sh, 1)
    _levels_json = json.dumps(_lvls_y)
    icon = coin_icon_b64(sig["symbol"])
    _sym_short = sig['symbol'][:-4] if sig['symbol'].endswith('USDT') else sig['symbol']
    icon_html = (f'<img src="data:image/png;base64,{icon}" '
                 'style="width:18px;height:18px;vertical-align:-3px;border-radius:50%"> '
                 if icon else f'<span style="display:inline-block;width:18px;height:18px;vertical-align:-3px;border-radius:50%;background:#7b3ff2;color:#fff;font:700 10px Inter,sans-serif;text-align:center;line-height:18px;margin-right:4px">{_sym_short[0]}</span> ')

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
    # LONG/SHORT marker: custom HTML triangle + text (GGShot style)
    # (removed from lightweight-charts markers, drawn as HTML below)
    markers = []
    sig_marker_data = {"time": int(sig_time), "side": sig["side"]}
    # TP hit markers: stored for custom HTML overlay (TradingView plotshape style)
    tp_hit_data = []
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
                tp_hit_data.append({"time": hx, "level": float(tp), "idx": i})

    title2 = f"APEX {sig['symbol']} - {sig['tf']} | VALIDATOR {sig['score']}/100"
    if callout:
        title2 = f"APEX {sig['symbol']} - {sig['tf']} | {callout.split(chr(10))[0]}"
    callout_html = ""
    if callout:
        txt = callout.replace("\n", "<br>")
        icon = "\U0001F4C8\U0001F480 " if is_long else "\U0001F4C9 "
        callout_html = (f'<div id="callout">{icon}{txt}</div>')

    # Supertrend: fill bands + single continuous color-changing line (GGShot/TradingView style).
    # Drawn as SVG (not lightweight-charts series) for full control over color changes.
    # Built as a plain string (NOT f-string) to avoid brace-escaping issues.
    # Deferred via setTimeout so the time scale layout is complete.
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
  // Group into trend segments for fill
  const segs = [];
  let cur = null;
  const linePts = [];
  CANDLES.forEach(c_ => {
    const sv = _trMap[c_.time], td = _tdMap[c_.time];
    if (sv === undefined || td === undefined) return;
    const x = chart.timeScale().timeToCoordinate(c_.time);
    const yS = cs.priceToCoordinate(sv);
    if (x == null || yS == null) return;
    linePts.push({x: x, y: yS, trend: td});
    let yT, yB;
    if (td === 1) {
      yT = cs.priceToCoordinate(c_.low);
      yB = yS;
    } else {
      yT = yS;
      yB = cs.priceToCoordinate(c_.high);
    }
    if (yT == null || yB == null) return;
    if (!cur || cur.trend !== td) { cur = {trend: td, pts: []}; segs.push(cur); }
    cur.pts.push([x, yT, yB]);
  });
  // Fill bands
  segs.forEach(sg => {
    if (sg.pts.length < 2) return;
    const _top = sg.pts.map(p => p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ');
    const _bot = sg.pts.slice().reverse().map(p => p[0].toFixed(1)+','+p[2].toFixed(1)).join(' ');
    const _poly = document.createElementNS(svgNS, 'polygon');
    _poly.setAttribute('points', _top + ' ' + _bot);
    _poly.setAttribute('fill', sg.trend === 1 ? 'rgba(38,166,154,0.16)' : 'rgba(239,83,80,0.14)');
    _svg.appendChild(_poly);
  });
  // Single continuous line, color changes at trend flips (TradingView style)
  let segPts = [];
  let segTrend = null;
  const flushSeg = () => {
    if (segPts.length < 2) { segPts = []; return; }
    const _pl = document.createElementNS(svgNS, 'polyline');
    _pl.setAttribute('points', segPts.map(p => p.x.toFixed(1)+','+p.y.toFixed(1)).join(' '));
    _pl.setAttribute('fill', 'none');
    _pl.setAttribute('stroke', segTrend === 1 ? '#26a69a' : '#ef5350');
    _pl.setAttribute('stroke-width', '2');
    _pl.setAttribute('stroke-linejoin', 'round');
    _pl.setAttribute('stroke-linecap', 'round');
    _svg.appendChild(_pl);
    segPts = [];
  };
  linePts.forEach((pt, i) => {
    if (segTrend === null) segTrend = pt.trend;
    if (pt.trend !== segTrend) {
      segPts.push(pt);
      flushSeg();
      segTrend = pt.trend;
      segPts.push(pt);
    } else {
      segPts.push(pt);
    }
  });
  flushSeg();
  document.getElementById('tv').appendChild(_svg);
  // Trailing Stop Loss label (GGShot style, gray pill)
  const _st = TRD[TRD.length-1];
  const _sy = cs.priceToCoordinate(_st.value);
  if (_sy != null) {
    const _d = document.createElement('div');
    _d.className = 'plabel st';
    _d.style.top = _sy + 'px';
    _d.innerHTML = 'GG KURD:Trailing Stop Loss <b>' + _st.value.toFixed(__PXP__) + '</b>';
    document.getElementById('tv').appendChild(_d);
  }
  // TP/SL dotted lines as SVG (pale, limited extent, green for LONG / red for SHORT)
  const _sigX = chart.timeScale().timeToCoordinate(__SIGTIME__);
  if (_sigX != null) {{
    const _lineEndX = Math.min(_sigX + 500, __W__ - 80);  // Limited extent, not full width
    _TPSL.forEach((p, _li) => {{
      const _ly2 = cs.priceToCoordinate(p);
      if (_ly2 == null) return;
      const _ln = document.createElementNS(svgNS, 'line');
      _ln.setAttribute('x1', _sigX);
      _ln.setAttribute('y1', _ly2);
      _ln.setAttribute('x2', _lineEndX);
      _ln.setAttribute('y2', _ly2);
      _ln.setAttribute('stroke', _TPSL_COLS[_li]);
      _ln.setAttribute('stroke-width', '1');
      _ln.setAttribute('stroke-dasharray', '4,4');
      _ln.setAttribute('opacity', '0.7');  // Pale but visible
      _svg.appendChild(_ln);
    }});
  }}
  // TP/SL labels as divs (same pattern as Trailing Stop which works)
  // Show ALL labels, clamping off-chart prices to edges
  const _LVLS = __LEVELS_JSON__;
  const _bgc2 = '__COL__';
  // First pass: get Y for each, clamp nulls to edges
  let _litems = [];
  _LVLS.forEach(l => {
    let _ly = cs.priceToCoordinate(l.price);
    if (_ly == null) {
      // Off-chart: clamp to top (if above) or bottom (if below)
      // Compare price to chart's visible center
      _ly = 0; // Will be fixed in second pass
      _litems.push({l: l, y: _ly, offchart: true});
    } else {
      _litems.push({l: l, y: _ly, offchart: false});
    }
  });
  // For off-chart items, place at edges with stacking
  // SHORT: TPs below -> stack at bottom. LONG: TPs above -> stack at top.
  // Order in _LVLS is top-to-bottom, so off-chart-below items go to bottom.
  let _topY = 30, _botY = __H__ - 30;
  // Count off-chart at top vs bottom by price relative to visible ones
  _litems.forEach(it => {
    if (!it.offchart) return;
    // If price is higher than all visible, it's above; else below
    // Simple heuristic: use LVLS order - first items are top, last are bottom
    const _idx = _litems.indexOf(it);
    // For SHORT, LVLS = [SL, TP1, TP2, TP3, TP4], SL is top
    // For LONG, LVLS = [TP4, TP3, TP2, TP1, SL], TP4 is top
    // Off-chart items at start -> top, at end -> bottom
    if (_idx < _litems.length / 2) {
      it.y = _topY;
      _topY += 32;
    } else {
      it.y = _botY;
      _botY -= 32;
    }
    it.offchart = false;
  });
  // Apply gap to avoid overlap
  for (let _i = 1; _i < _litems.length; _i++) {
    if (_litems[_i].y - _litems[_i-1].y < 28) _litems[_i].y = _litems[_i-1].y + 28;
  }
  _litems.forEach(it => {
    const l = it.l, _ly = it.y;
    const _ld = document.createElement('div');
    _ld.className = 'plabel ' + (l.cls === 'tp' ? 'tp' : 'sl');
    _ld.style.top = _ly + 'px';
    _ld.style.background = l.cls === 'tp' ? _bgc2 : '#ef5350';
    _ld.textContent = 'GG KURD:' + l.name + '  ' + l.price.toFixed(__PXP__);
    document.getElementById('tv').appendChild(_ld);
  });
})();
}, 120);
""".replace("__W__", str(width)).replace("__H__", str(height)).replace("__PXP__", str(pxp)).replace("__LEVELS_JSON__", _levels_json).replace("__COL__", col).replace("__PMAX__", str(_pmax)).replace("__PMIN__", str(_pmin)).replace("__SIGTIME__", str(int(sig_time)))

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#000;overflow:hidden}}
#tv{{width:{width}px;height:{height}px;position:relative}}
#title{{position:absolute;top:8px;left:12px;color:#d1d4dc;font:13px -apple-system,system-ui,sans-serif;z-index:5;pointer-events:none}}
#sub{{position:absolute;top:30px;left:12px;color:#787b86;font:11px -apple-system,system-ui,sans-serif;z-index:5;pointer-events:none}}
#callout{{position:absolute;{"top:64px" if is_long else "bottom:64px"};left:56%;transform:translateX(-50%);background:{col};color:#fff;
 font:bold 13px -apple-system,system-ui,sans-serif;text-align:center;padding:10px 18px;border-radius:8px;z-index:5;pointer-events:none;line-height:1.4}}
#callout:after{{content:'';position:absolute;left:50%;{"bottom:-9px" if is_long else "top:-9px"};transform:translateX(-50%);
 border-left:9px solid transparent;border-right:9px solid transparent;{"border-top:10px solid " + col if is_long else "border-bottom:10px solid " + col}}}
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
const cs = chart.addCandlestickSeries({{ upColor: 'transparent', downColor: '#c62828',
  borderVisible: true, borderUpColor: '{UP}', borderDownColor: '#c62828',
  wickUpColor: '{UP}', wickDownColor: '#c62828',
  priceFormat: {{ type: 'price', precision: {pxp}, minMove: {min_move} }} }});
const CANDLES = {json.dumps(candles)};
cs.setData(CANDLES);
// Hidden series to force price scale to include all TP/SL levels (so all labels visible)
// Note: must be visible:true (but transparent) to affect auto-scale
const _rangeSeries = chart.addLineSeries({{
  color: 'rgba(0,0,0,0)', lineWidth: 1, visible: true,
  priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
}});
const _sigT = {int(sig_time)};
const _allLvls = {json.dumps([round(t, 10) for t in tps] + [round(float(sl), 10)])};
_rangeSeries.setData(_allLvls.map(p => ({{ time: _sigT, value: p }})));
const TRD = {json.dumps(tr)};
const DASH = LightweightCharts.LineStyle.Dashed;
const TPS = {json.dumps([round(t, 10) for t in tps])};
const NAMES = ['APEX:Take Profit','APEX:Take Profit 2','APEX:Take Profit 3','APEX:Take Profit 4'];
// TP/SL dotted lines drawn as SVG (for control over extent and opacity)
// Instead of createPriceLine (which spans full width)
const _TPSL = {json.dumps([round(t, 10) for t in tps] + [round(float(sl), 10)])};
const _TPSL_COLS = {json.dumps(['{col}']*4 + ['{DN}'])};
{_svg_js}
cs.setMarkers({json.dumps(markers)});
const TP_HITS = {json.dumps(tp_hit_data)};
const SIG_MK = {json.dumps(sig_marker_data)};
const ISL2 = {"true" if is_long else "false"};
// All custom markers run after layout (setTimeout) so timeToCoordinate is accurate
setTimeout(() => {{
(function(){{
// LONG/SHORT entry marker: big triangle + text (GGShot style)
// Positioned under/above the signal candle
  const x = chart.timeScale().timeToCoordinate(SIG_MK.time);
  const c0 = CANDLES.find(c => c.time === SIG_MK.time);
  if (x == null || !c0) return;
  const y = cs.priceToCoordinate(ISL2 ? c0.low : c0.high);
  if (y == null) return;
  const col = ISL2 ? '#0aa06e' : '#ef5350';
  const d = document.createElement('div');
  const yOff = ISL2 ? 6 : -52;
  d.style.cssText = 'position:absolute;left:' + (x - 22) + 'px;top:' + (y + yOff) + 'px;'
    + 'width:44px;z-index:5;pointer-events:none;text-align:center;';
  const tri = ISL2
    ? '<div style="width:0;height:0;border-left:9px solid transparent;border-right:9px solid transparent;border-bottom:12px solid ' + col + ';margin:0 auto;"></div>'
    : '<div style="width:0;height:0;border-left:9px solid transparent;border-right:9px solid transparent;border-top:12px solid ' + col + ';margin:0 auto;"></div>';
  const txt = '<div style="color:' + col + ';font:700 12px Inter,system-ui,sans-serif;margin-top:3px;">' + SIG_MK.side + '</div>';
  d.innerHTML = ISL2 ? tri + txt : txt + tri;
  document.getElementById('tv').appendChild(d);
}})();
// "Get ready to long/short" pill (GGShot style, before signal)
if ({"true" if show_get_ready else "false"}) {{
(function(){{
  const gx = chart.timeScale().timeToCoordinate(SIG_MK.time);
  if (gx == null) return;
  const gcol = ISL2 ? 'rgba(10,160,110,0.75)' : 'rgba(239,83,80,0.75)';
  const gtxt = ISL2 ? '▲ Get ready to long' : '▼ Get ready to short';
  // Find candle near Get Ready position (100px left of signal, closer)
  const grX = gx - 100;
  let bestC = null, bestD = 1e9;
  CANDLES.forEach(c => {{
    const cx = chart.timeScale().timeToCoordinate(c.time);
    if (cx == null) return;
    const d = Math.abs(cx - grX);
    if (d < bestD) {{ bestD = d; bestC = c; }}
  }});
  if (!bestC) return;
  // Position clearly above (SHORT) or below (LONG) the candle, not overlapping
  const cy = cs.priceToCoordinate(ISL2 ? bestC.low : bestC.high);
  if (cy == null) return;
  const gy = ISL2 ? cy + 25 : cy - 65;  // Clear gap from candle
  const gd = document.createElement('div');
  gd.style.cssText = 'position:absolute;left:' + (grX - 70) + 'px;top:' + gy + 'px;'
    + 'background:' + gcol + ';color:#fff;font:600 12px Inter,system-ui,sans-serif;'
    + 'padding:6px 12px;border-radius:6px;z-index:5;pointer-events:none;white-space:nowrap;';
  gd.textContent = gtxt;
  document.getElementById('tv').appendChild(gd);
}})();
}}
// Supertrend flip "+" markers (GGShot style: green + at up-flip, red + at down-flip)
const FLIPS = [];
for (let i = 1; i < TRD.length; i++) {{
  if (TRD[i].trend !== TRD[i-1].trend) {{
    FLIPS.push({{time: TRD[i].time, trend: TRD[i].trend}});
  }}
}}
FLIPS.forEach(ff => {{
  const x = chart.timeScale().timeToCoordinate(ff.time);
  const c_ = CANDLES.find(c => c.time === ff.time);
  if (x == null || !c_) return;
  const y = cs.priceToCoordinate(ff.trend === 1 ? c_.low : c_.high);
  if (y == null) return;
  const d = document.createElement('div');
  const yOff = ff.trend === 1 ? 8 : -20;
  d.style.cssText = 'position:absolute;left:' + (x - 8) + 'px;top:' + (y + yOff) + 'px;'
    + 'color:' + (ff.trend === 1 ? '#0aa06e' : '#ef5350') + ';font:700 16px Inter,system-ui,sans-serif;'
    + 'text-align:center;width:16px;z-index:5;pointer-events:none;';
  d.innerHTML = '+';
  document.getElementById('tv').appendChild(d);
}});
// TP hit markers (TradingView plotshape style: "TPn" + triangle at TP level)
// TP hit markers: text + CSS triangle (crisp, like TradingView)
const MK_GREEN = '#0aa06e';
const MK_RED = '#ef5350';
TP_HITS.forEach(hh => {{
  const x = chart.timeScale().timeToCoordinate(hh.time);
  const y = cs.priceToCoordinate(hh.level);
  if (x == null || y == null) return;
  const d = document.createElement('div');
  const col = ISL2 ? MK_GREEN : MK_RED;
  const yOff = -26;
  d.style.cssText = 'position:absolute;left:' + (x - 18) + 'px;top:' + (y + yOff) + 'px;'
    + 'width:36px;z-index:5;pointer-events:none;text-align:center;';
  const triStyle = ISL2
    ? 'width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-top:9px solid ' + col + ';margin:2px auto 0;'
    : 'width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;border-bottom:9px solid ' + col + ';margin:0 auto 2px;';
  const txtStyle = 'color:' + col + ';font:700 12px Inter,system-ui,sans-serif;line-height:1;';
  // LONG: text then ▼ below | SHORT: text then ▲ below (like GGShot)
  d.innerHTML = '<div style="' + txtStyle + '">TP' + hh.idx + '</div><div style="' + triStyle + '"></div>';
  document.getElementById('tv').appendChild(d);
}});
}}, 150);
chart.timeScale().fitContent();
// ---- GGShot-style spaced TP/SL labels (custom overlay, collision-free) ----
// For SHORT, reverse order so TP1 is at top (closest to entry)
// Labels drawn in SVG (120ms timeout) where priceToCoordinate works
setTimeout(() => {{
  window.__done = true;
}}, 500);
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
