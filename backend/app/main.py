"""nomad-api — read-only API + chart UI over the bars in Postgres.

Single service: serves the JSON bar API and the HTML chart page. Read-only,
never touches IBKR. Exposed (only this) at nomad.securegion.com via Traefik.
"""
import datetime as dt
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, ORJSONResponse

from .config import settings

_pool: asyncpg.Pool | None = None

# timeframe -> (bucket seconds | None=daily | 0=raw 1s, default window days). The
# window is the INITIAL load size; the UI lazy-loads older chunks (same size) as
# you pan left. 1s reads raw bars_1s, the rest read the bars_1m rollup.
TF = {
    "1s":  (0,     1),     # raw 1-second: ONE session (a year of 1s = millions of pts);
                           # pan left lazy-loads older sessions.
    "1m":  (60,    3650),  # 1m and coarser load the FULL history up front (cheap enough:
    "5m":  (300,   3650),  # ~98k / 20k / 6.5k / 1.6k / 250 bars) so you can zoom out to
    "15m": (900,   3650),  # see the whole year instantly. Initial view is a recent slice.
    "1h":  (3600,  3650),
    "1d":  (None,  3650),
}
RTH = "(ts AT TIME ZONE 'America/New_York')::time >= '09:30' " \
      "AND (ts AT TIME ZONE 'America/New_York')::time < '16:00'"
# Emit epoch + rounded numerics straight from SQL so Python just passes rows
# through (list(record)) — no per-row .timestamp()/round() over ~100k rows.
COLS = ("extract(epoch from ts)::bigint AS t, "
        "round(open::numeric,4)::float8 AS o, round(high::numeric,4)::float8 AS h, "
        "round(low::numeric,4)::float8 AS l, round(close::numeric,4)::float8 AS c, "
        "round(volume)::bigint AS v")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5,
                                      server_settings={"work_mem": "96MB"})
    yield
    await _pool.close()


app = FastAPI(title="nomad", lifespan=lifespan, default_response_class=ORJSONResponse)
app.add_middleware(GZipMiddleware, minimum_size=2000)   # shrink large bar payloads


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/symbols")
async def symbols():
    rows = await _pool.fetch("SELECT DISTINCT symbol FROM bars_1m ORDER BY symbol")
    return [r["symbol"] for r in rows]


@app.get("/api/bars")
async def bars(symbol: str = Query(...), tf: str = Query("1m"),
               to: float | None = Query(None),
               frm: float | None = Query(None, alias="from")):
    """Bars in [from, to) — both epoch seconds, both optional. Defaults: to = latest
    available bar, from = to - the timeframe's window. The UI pages older data by
    re-calling with to = earliest bar it currently holds."""
    symbol = symbol.upper()
    bucket, win_days = TF.get(tf, TF["1m"])
    anchor = await _pool.fetchval("SELECT max(ts) FROM bars_1m WHERE symbol = $1", symbol)
    if anchor is None:
        return {"symbol": symbol, "tf": tf, "bars": []}
    to_ts = dt.datetime.fromtimestamp(to, dt.timezone.utc) if to else anchor + dt.timedelta(seconds=1)
    frm_ts = dt.datetime.fromtimestamp(frm, dt.timezone.utc) if frm else to_ts - dt.timedelta(days=win_days)

    if bucket == 0:                              # raw 1-second from bars_1s
        q = f"SELECT {COLS} FROM bars_1s WHERE symbol=$1 AND ts >= $2 AND ts < $3 AND {RTH} ORDER BY ts"
        rows = await _pool.fetch(q, symbol, frm_ts, to_ts)
        data = [list(r) for r in rows]
    elif bucket == 60:                           # 1-minute: bars_1m IS already 1-min
        q = f"SELECT {COLS} FROM bars_1m WHERE symbol=$1 AND ts >= $2 AND ts < $3 AND is_rth ORDER BY ts"
        rows = await _pool.fetch(q, symbol, frm_ts, to_ts)   # no aggregation needed
        data = [list(r) for r in rows]
    elif bucket is None:                         # daily, by NY calendar date
        q = f"""SELECT (ts AT TIME ZONE 'America/New_York')::date AS t,
                       (array_agg(open ORDER BY ts))[1] AS o, max(high) AS h, min(low) AS l,
                       (array_agg(close ORDER BY ts DESC))[1] AS c, sum(volume) AS v
                FROM bars_1m WHERE symbol=$1 AND ts >= $2 AND ts < $3 AND is_rth
                GROUP BY t ORDER BY t"""
        rows = await _pool.fetch(q, symbol, frm_ts, to_ts)
        data = [_bar(r["t"].isoformat(), r) for r in rows]
    else:                                        # bucketed from bars_1m
        q = f"""SELECT to_timestamp(floor(extract(epoch from ts)/$2)*$2) AS t,
                       (array_agg(open ORDER BY ts))[1] AS o, max(high) AS h, min(low) AS l,
                       (array_agg(close ORDER BY ts DESC))[1] AS c, sum(volume) AS v
                FROM bars_1m WHERE symbol=$1 AND ts >= $3 AND ts < $4 AND is_rth
                GROUP BY t ORDER BY t"""
        rows = await _pool.fetch(q, symbol, bucket, frm_ts, to_ts)
        data = [_bar(int(r["t"].timestamp()), r) for r in rows]
    return {"symbol": symbol, "tf": tf, "bars": data}


def _bar(t, r):
    # compact row: [time, open, high, low, close, volume] — far smaller/faster
    # than verbose objects across ~100k bars.
    return [t, round(float(r["o"]), 4), round(float(r["h"]), 4), round(float(r["l"]), 4),
            round(float(r["c"]), 4), round(float(r["v"]))]


@app.get("/api/instances")
async def instances(symbol: str = Query(...), name: str = Query("breakout")):
    """Strategy instances to review on the chart. 'breakout' = the 1%-above-prev-5
    one-second momentum entries from feat_1s, with entry/target and outcome."""
    symbol = symbol.upper()
    if name != "breakout":
        return {"symbol": symbol, "name": name, "instances": []}
    rows = await _pool.fetch(
        """SELECT extract(epoch from ts)::bigint AS t,
                  round((p5h*1.01)::numeric, 2)::float8       AS entry,
                  round((p5h*1.01*1.005)::numeric, 2)::float8 AS target,
                  n5h, c5
           FROM feat_1s
           WHERE symbol = $1 AND p5h IS NOT NULL AND nfwd = 5 AND c5 IS NOT NULL
             AND high >= p5h * 1.01
           ORDER BY ts""", symbol)
    out = []
    for r in rows:
        entry = r["entry"]
        win = float(r["n5h"]) >= r["target"]
        pnl = 0.5 if win else round((float(r["c5"]) - entry) / entry * 100, 3)
        out.append({"t": r["t"], "entry": entry, "target": r["target"], "win": win, "pnl": pnl})
    return {"symbol": symbol, "name": name, "instances": out}


@app.get("/", response_class=HTMLResponse)
async def index():
    # no-store so UI iterations show immediately (no stale cached page)
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>nomad</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:#0e1116; color:#d4d8de; }
  header { display:flex; gap:14px; align-items:center; padding:12px 16px; border-bottom:1px solid #20262e; }
  h1 { font-size:16px; margin:0; letter-spacing:.5px; color:#fff; }
  select, button { background:#161b22; color:#d4d8de; border:1px solid #2b333d; border-radius:6px;
                   padding:6px 10px; font-size:13px; cursor:pointer; }
  button.active { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .tfs { display:flex; gap:6px; }
  .nav { display:none; gap:8px; align-items:center; font-size:12px; color:#cbd3da; }
  .nav button { padding:4px 9px; }
  .nav b { min-width:140px; text-align:center; }
  #meta { margin-left:auto; font-size:12px; color:#8b949e; }
  #chart { width:100vw; height:calc(100vh - 53px); }
</style></head>
<body>
  <header>
    <h1>nomad</h1>
    <select id="sym"></select>
    <div class="tfs" id="tfs"></div>
    <select id="tech"></select>
    <span class="nav" id="nav">
      <button id="prev">◀ prev</button><b id="navlbl"></b><button id="next">next ▶</button>
    </span>
    <span id="meta"></span>
  </header>
  <div id="chart"></div>
<script>
const el = document.getElementById('chart');
// Display all times in US Eastern (market time). Data stays UTC underneath;
// these formatters just relabel ticks/crosshair. Intl handles EDT/EST per-date.
const ET = 'America/New_York';
const toMs = t => typeof t==='number' ? t*1000
  : typeof t==='string' ? Date.parse(t+'T00:00:00Z')
  : Date.UTC(t.year, t.month-1, t.day);
const etFmt = o => new Intl.DateTimeFormat('en-US', {timeZone:ET, hour12:false, ...o});
const chart = LightweightCharts.createChart(el, {
  layout:{ background:{color:'#0e1116'}, textColor:'#d4d8de' },
  grid:{ vertLines:{color:'#1b212a'}, horzLines:{color:'#1b212a'} },
  rightPriceScale:{ borderColor:'#2b333d' },
  crosshair:{ mode: LightweightCharts.CrosshairMode.Normal },
  localization:{ timeFormatter: t =>
    etFmt({month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(toMs(t))) },
  timeScale:{ timeVisible:true, secondsVisible:false, borderColor:'#2b333d',
    tickMarkFormatter:(t,type)=>{ const d=new Date(toMs(t));
      if(type>=4) return etFmt({hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);
      if(type>=3) return etFmt({hour:'2-digit',minute:'2-digit'}).format(d);
      if(type===2) return etFmt({month:'short',day:'numeric'}).format(d);
      if(type===1) return etFmt({month:'short'}).format(d);
      return etFmt({year:'numeric'}).format(d); } },
});
const candle = chart.addCandlestickSeries({
  upColor:'#26a641', downColor:'#f85149', borderVisible:false,
  wickUpColor:'#26a641', wickDownColor:'#f85149',
});
const vol = chart.addHistogramSeries({ priceFormat:{type:'volume'}, priceScaleId:'',
  color:'#2b333d' });
vol.priceScale().applyOptions({ scaleMargins:{ top:0.82, bottom:0 } });

const TFS = ['1s','1m','5m','15m','1h','1d'];
const WINDOW = {'1s':1,'1m':3650,'5m':3650,'15m':3650,'1h':3650,'1d':3650}; // days, matches server
const VIZ    = {'1s':null,'1m':1950,'5m':780,'15m':520,'1h':600,'1d':null}; // initial visible bars
let tf = '1m', sym = null;
let bars = [], loading = false, exhausted = false, reqId = 0;

// --- techniques: pattern detectors over the loaded bars. Add more here. ---
// bar = [t, open, high, low, close, volume]; test(d, i) sees the whole series.
let technique = '', matches = [], inst = 0;
let serverInst = [], priceLines = [];        // for server-provided strategy instances
const PAD = 20;                              // bars to show on each side of a match
const SPIKE_PCT = 0.0025;                     // spike must extend >=0.25% beyond last 5 bars
const TECHNIQUES = {
  '': { label: '— technique —' },
  'shooting_star': { label: 'Shooting Star', test: (d, i) => {
    if(i < 5) return false;
    const b=d[i], o=b[1], h=b[2], l=b[3], c=b[4], rng=h-l;
    if(rng<=0) return false;
    const uw=h-Math.max(o,c), body=Math.abs(c-o);
    if(!(uw>=0.60*rng && body<=0.30*rng && c<=l+0.35*rng)) return false;   // rejection shape
    let hi=-Infinity, lo=Infinity;                                          // prev-5-bar extremes
    for(let k=i-5;k<i;k++){ if(d[k][2]>hi) hi=d[k][2]; if(d[k][3]<lo) lo=d[k][3]; }
    return h >= hi*(1+SPIKE_PCT) || l <= lo*(1-SPIKE_PCT);   // spike >=3% beyond recent range
  }},
  // server-side: the 1%-breakout entries computed in the DB (see /api/instances)
  'breakout': { label: '1% Breakout (1s)', server: true },
};

function renderTfs(){
  const box = document.getElementById('tfs'); box.innerHTML='';
  TFS.forEach(t=>{ const b=document.createElement('button'); b.textContent=t;
    if(t===tf) b.className='active'; b.onclick=()=>{ tf=t; renderTfs(); load(); };
    box.appendChild(b); });
}
// compact bar row: [0]=time [1]=open [2]=high [3]=low [4]=close [5]=volume
function draw(){
  candle.setData(bars.map(b=>({time:b[0],open:b[1],high:b[2],low:b[3],close:b[4]})));
  vol.setData(bars.map(b=>({time:b[0],value:b[5],
    color: b[4]>=b[1] ? 'rgba(38,166,65,.4)':'rgba(248,81,73,.4)'})));
  document.getElementById('meta').textContent = `${sym} · ${tf} · ${bars.length} bars`;
}
async function fetchRange(frm, to){
  let u = `/api/bars?symbol=${sym}&tf=${tf}`;
  if(to!=null) u += `&to=${to}`;
  if(frm!=null) u += `&from=${frm}`;
  return (await (await fetch(u)).json()).bars;
}
const epochOf = b => typeof b[0]==='string' ? Math.floor(Date.parse(b[0]+'T00:00:00Z')/1000) : b[0];

function computeMatches(){
  const t = TECHNIQUES[technique] && TECHNIQUES[technique].test;
  matches = [];
  if(t) for(let i=0;i<bars.length;i++) if(t(bars, i)) matches.push(i);
}
// Hindsight "top -> down" move for the match at bars[i]: sell the candle's high,
// cover the lowest low across this bar + the next 5. Always >= 0 (best case).
function dropFor(i){
  const top = bars[i][2];
  let down = bars[i][3], downIdx = i;
  for(let k=i+1; k<=Math.min(i+5, bars.length-1); k++)
    if(bars[k][3] < down){ down = bars[k][3]; downIdx = k; }
  const abs = top - down;
  return { top, down, downIdx, abs, pct: abs/top*100 };
}
function median(a){
  if(!a.length) return 0;
  const s = [...a].sort((x,y)=>x-y), m = s.length>>1;
  return s.length%2 ? s[m] : (s[m-1]+s[m])/2;
}
function showInstance(){
  if(!matches.length){ candle.setMarkers([]); draw();
    document.getElementById('navlbl').textContent = '0 matches'; return; }
  inst = (inst % matches.length + matches.length) % matches.length;   // wrap
  const i = matches[inst], w = bars.slice(Math.max(0,i-PAD), i+PAD+1);
  candle.setData(w.map(b=>({time:b[0],open:b[1],high:b[2],low:b[3],close:b[4]})));
  vol.setData(w.map(b=>({time:b[0],value:b[5],
    color: b[4]>=b[1] ? 'rgba(38,166,65,.4)':'rgba(248,81,73,.4)'})));
  const d = dropFor(i);
  candle.setMarkers([
    {time:bars[i][0],        position:'aboveBar', color:'#f0b72f', shape:'arrowDown',
     text:'sell '+d.top.toFixed(2)},
    {time:bars[d.downIdx][0], position:'belowBar', color:'#26a641', shape:'arrowUp',
     text:'↓'+d.pct.toFixed(2)+'% ('+d.down.toFixed(2)+')'},
  ]);
  chart.timeScale().fitContent();
  const dt = etFmt({month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(toMs(bars[i][0])));
  document.getElementById('navlbl').textContent =
    `${inst+1} / ${matches.length}  ·  ${dt} ET  ·  drop ${d.pct.toFixed(2)}% ($${d.abs.toFixed(2)})`;
}
function clearPriceLines(){ priceLines.forEach(p=>candle.removePriceLine(p)); priceLines=[]; }
// Server-provided instance (e.g. a 1% breakout): jump the chart to a ~1-minute
// 1-second window around it, mark the buy + target, and show the outcome.
async function showServerInstance(){
  const lbl = document.getElementById('navlbl');
  if(!serverInst.length){ lbl.textContent = '0 found'; return; }
  inst = (inst % serverInst.length + serverInst.length) % serverInst.length;
  const ins = serverInst[inst];
  const wb = (await (await fetch(`/api/bars?symbol=${sym}&tf=1s&from=${ins.t-30}&to=${ins.t+31}`)).json()).bars;
  candle.setData(wb.map(b=>({time:b[0],open:b[1],high:b[2],low:b[3],close:b[4]})));
  vol.setData(wb.map(b=>({time:b[0],value:b[5],color:b[4]>=b[1]?'rgba(38,166,65,.4)':'rgba(248,81,73,.4)'})));
  clearPriceLines();
  priceLines.push(candle.createPriceLine({price:ins.entry,  color:'#5b8cff', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'buy'}));
  priceLines.push(candle.createPriceLine({price:ins.target, color:'#f0b72f', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'+0.5%'}));
  const bi = wb.findIndex(b=>b[0]===ins.t), mk=[];
  if(bi>=0){
    mk.push({time:ins.t, position:'belowBar', color:'#5b8cff', shape:'arrowUp', text:'BUY'});
    const ei = Math.min(bi+5, wb.length-1);
    mk.push({time:wb[ei][0], position:'aboveBar', color: ins.win?'#26a641':'#f85149', shape:'circle',
             text: ins.win?'+0.5%':((ins.pnl>0?'+':'')+ins.pnl+'%')});
  }
  candle.setMarkers(mk);
  chart.timeScale().applyOptions({ secondsVisible:true }); chart.timeScale().fitContent();
  const dt = etFmt({month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(toMs(ins.t)));
  lbl.textContent = `${inst+1} / ${serverInst.length} · ${dt} ET · buy ${ins.entry} → ${ins.win?'WIN +0.5%':((ins.pnl>0?'+':'')+ins.pnl+'%')}`;
}
async function applyView(){                  // render per the active technique
  const nav = document.getElementById('nav'), T = TECHNIQUES[technique];
  if(T && T.server){                         // server instances (1s breakout)
    nav.style.display = 'flex';
    document.getElementById('meta').textContent = `${sym} · loading ${T.label}…`;
    const j = await (await fetch(`/api/instances?symbol=${sym}&name=${technique}`)).json();
    serverInst = j.instances || []; inst = 0; await showServerInstance();
    const wins = serverInst.filter(x=>x.win).length;
    const avg = serverInst.length ? serverInst.reduce((a,x)=>a+(x.win?0.5:x.pnl),0)/serverInst.length : 0;
    document.getElementById('meta').textContent = serverInst.length
      ? `${sym} · ${T.label} · ${serverInst.length} trades · ${(100*wins/serverInst.length).toFixed(0)}% reach +0.5% · avg ${avg.toFixed(2)}%/trade (hindsight, no slippage)`
      : `${sym} · ${T.label} · none`;
  } else if(T && T.test){                     // client-side pattern scan
    nav.style.display = 'flex'; clearPriceLines(); computeMatches(); inst = 0; showInstance();
    const pcts = matches.map(i=>dropFor(i).pct);
    const avg = pcts.length ? pcts.reduce((a,b)=>a+b,0)/pcts.length : 0;
    document.getElementById('meta').textContent = matches.length
      ? `${sym} · ${tf} · ${T.label} · ${matches.length} found · `
        + `max-drop (hindsight) avg ${avg.toFixed(2)}% · median ${median(pcts).toFixed(2)}% · best ${Math.max(...pcts).toFixed(2)}%`
      : `${sym} · ${tf} · ${T.label} · no matches`;
  } else {                                    // normal chart
    nav.style.display = 'none'; clearPriceLines(); candle.setMarkers([]); draw();
    const n=bars.length, k=VIZ[tf];
    if(k && n>k) chart.timeScale().setVisibleLogicalRange({from:n-k, to:n-1});
    else chart.timeScale().fitContent();
  }
}
function step(d){
  const T = TECHNIQUES[technique];
  if(T && T.server){ if(serverInst.length){ inst += d; showServerInstance(); } }
  else if(matches.length){ inst += d; showInstance(); }
}

async function load(){                       // fresh load (symbol / tf change)
  if(!sym) return;
  const my = ++reqId; loading = true; exhausted = false;
  document.getElementById('meta').textContent = 'loading…';
  chart.timeScale().applyOptions({ secondsVisible: tf==='1s' });
  const got = await fetchRange(null, null);
  if(my !== reqId) return;                    // a newer load superseded us
  bars = got;
  applyView();                                // respects the active technique
  loading = false;
}
async function loadOlder(){                   // page older bars when panned left
  if(loading || exhausted || tf==='1d' || technique || !bars.length) return;
  const my = reqId; loading = true;
  const to = epochOf(bars[0]);
  const older = await fetchRange(to - WINDOW[tf]*86400, to);
  if(my !== reqId){ return; }                 // symbol/tf changed mid-flight
  if(!older.length){ exhausted = true; loading = false; return; }
  const vr = chart.timeScale().getVisibleLogicalRange();
  bars = older.concat(bars); draw();
  if(vr) chart.timeScale().setVisibleLogicalRange({from:vr.from+older.length, to:vr.to+older.length});
  loading = false;
}
chart.timeScale().subscribeVisibleLogicalRangeChange(r => { if(r && r.from < 12) loadOlder(); });
async function init(){
  renderTfs();
  const techSel = document.getElementById('tech');
  Object.keys(TECHNIQUES).forEach(k=>{ const o=document.createElement('option');
    o.value=k; o.textContent=TECHNIQUES[k].label; techSel.appendChild(o); });
  techSel.onchange = ()=>{ technique = techSel.value;
    const T = TECHNIQUES[technique];
    if(T && T.server){ if(tf!=='1s'){ tf='1s'; renderTfs(); } applyView(); }
    else { load(); }                         // none/client: reload current tf, then applyView
  };
  document.getElementById('prev').onclick = ()=>step(-1);
  document.getElementById('next').onclick = ()=>step(1);
  window.addEventListener('keydown', e=>{ if(technique){
    if(e.key==='ArrowRight') step(1); else if(e.key==='ArrowLeft') step(-1); }});

  const syms = await (await fetch('/api/symbols')).json();
  const sel = document.getElementById('sym');
  syms.forEach(s=>{ const o=document.createElement('option'); o.value=o.textContent=s; sel.appendChild(o); });
  sym = syms[0]; sel.value = sym;
  sel.onchange = ()=>{ sym = sel.value; load(); };
  load();
}
new ResizeObserver(()=>chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
init();
</script>
</body></html>"""
