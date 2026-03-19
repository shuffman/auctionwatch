import asyncio
import json
import os
import queue
import sys
import threading
import webbrowser
from urllib.parse import quote_plus

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Error: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

from models import SOURCE_COLORS_HTML
from store import (
    _get_secret_key, _init_db,
    _db_get_or_create_user,
    _db_get_ignored, _db_set_ignored,
    _db_get_starred, _db_set_starred,
    _db_get_start, _db_set_start,
    _db_save_search, _db_get_searches,
)
from scrapers import HAS_RICH, _console


_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AuctionWatch — Sign in</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0d0d0d; color: #e0e0e0; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; }
    .box { width: 320px; background: #141414; border: 1px solid #222; border-radius: 12px; padding: 2rem; }
    .brand { font-size: 1.2rem; font-weight: 700; color: #00bcd4; text-align: center; margin-bottom: 0.5rem; }
    .sub { font-size: .78rem; color: #555; text-align: center; margin-bottom: 1.75rem; }
    label { display: block; font-size: .78rem; color: #888; margin-bottom: .3rem; }
    input[type=text] {
      width: 100%; background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
      padding: .5rem .75rem; color: #e0e0e0; font-size: .9rem; outline: none; margin-bottom: 1rem;
    }
    input:focus { border-color: #00bcd4; }
    button[type=submit] {
      width: 100%; background: #00bcd4; border: none; border-radius: 6px;
      padding: .55rem; color: #000; font-weight: 700; font-size: .9rem; cursor: pointer;
    }
    button[type=submit]:hover { background: #26c6da; }
    .skip { display: block; text-align: center; margin-top: 1rem; font-size: .78rem; color: #444;
            text-decoration: none; }
    .skip:hover { color: #888; }
    .error { color: #ff5252; font-size: .78rem; margin-bottom: .9rem; min-height: 1.1rem; }
  </style>
</head>
<body>
<div class="box">
  <div class="brand">AuctionWatch</div>
  <div class="sub">Sign in to save starred &amp; ignored listings</div>
  <div class="error">{{error}}</div>
  <form method="post" action="/login">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" autofocus required>
    <button type="submit">Continue</button>
  </form>
  <a class="skip" href="/">Continue as guest</a>
</div>
</body>
</html>"""

_WEB_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AuctionWatch</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
    :root {
      --bg: #0d0d0d; --bg2: #141414; --bg3: #1a1a1a;
      --border: #252525; --text: #e0e0e0; --dim: #555;
      --green: #00e676; --red: #ff5252; --yellow: #e6c84a; --accent: #00bcd4;
    }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: var(--bg); color: var(--text); }

    /* ── Header: brand + search + auth ── */
    header {
      background: var(--bg2); border-bottom: 1px solid #1e1e1e;
      padding: 0.75rem 1.5rem; display: flex; align-items: center; gap: 0.75rem;
    }
    .brand { font-size: 1.05rem; font-weight: 700; color: var(--accent); white-space: nowrap; flex-shrink: 0; }
    #sf { display: flex; align-items: center; gap: 0.5rem; flex: 1; min-width: 0; }
    #q {
      flex: 1; min-width: 0; background: #1e1e1e; border: 1px solid #2e2e2e;
      border-radius: 6px; padding: 0.42rem 0.75rem; color: var(--text); font-size: 0.9rem; outline: none;
    }
    #q:focus { border-color: var(--accent); }
    #search-wrap { position: relative; flex: 1; min-width: 0; display: flex; }
    #search-wrap #q { width: 100%; }
    #recent-searches {
      display: none; position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 100;
      background: #1e1e1e; border: 1px solid #2e2e2e; border-radius: 6px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.5); overflow: hidden;
    }
    #recent-searches.open { display: block; }
    .rs-item {
      padding: 0.45rem 0.75rem; font-size: 0.85rem; color: var(--text-dim); cursor: pointer;
      display: flex; align-items: center; gap: 0.5rem;
    }
    .rs-item:hover { background: #2a2a2a; color: var(--text); }
    .rs-item .rs-icon { font-size: 0.7rem; opacity: 0.4; }
    #search-btn {
      padding: 0.4rem 1.1rem; background: var(--accent); border: none; border-radius: 6px;
      color: #000; font-weight: 700; font-size: 0.85rem; cursor: pointer; white-space: nowrap; flex-shrink: 0;
    }
    #search-btn:hover { background: #26c6da; }
    #search-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    /* ── Filter bar ── */
    #filters {
      background: #111; border-bottom: 1px solid #1e1e1e;
      padding: 0.5rem 1.5rem; display: flex; align-items: center; gap: 0; flex-wrap: wrap; row-gap: 0.4rem;
    }
    .fg { display: flex; align-items: center; gap: 0.3rem; padding: 0 0.9rem; }
    .fg:first-child { padding-left: 0; }
    .fg:last-child  { padding-right: 0; }
    .fsep { width: 1px; height: 16px; background: #222; flex-shrink: 0; align-self: center; }
    .flabel { font-size: 0.68rem; color: #3a3a3a; white-space: nowrap; text-transform: uppercase;
              letter-spacing: 0.06em; margin-right: 0.15rem; }
    .fdash { color: #2a2a2a; font-size: 0.8rem; }
    .pills { display: flex; gap: 0.25rem; flex-wrap: wrap; }
    .pill {
      padding: 0.22rem 0.55rem; border-radius: 20px; font-size: 0.71rem; font-weight: 600;
      border: 1px solid #252525; color: #3a3a3a; cursor: pointer; user-select: none; transition: all 0.15s;
    }
    .pill:hover { border-color: #3a3a3a; color: #666; }
    .pill[data-site="cab"].on  { color: #00bcd4; border-color: rgba(0,188,212,0.5); background: rgba(0,188,212,0.07); }
    .pill[data-site="bat"].on  { color: #4caf50; border-color: rgba(76,175,80,0.5);  background: rgba(76,175,80,0.07); }
    .pill[data-site="hagerty"].on { color: #2196f3; border-color: rgba(33,150,243,0.5); background: rgba(33,150,243,0.07); }
    .pill[data-site="pcar"].on { color: #9c27b0; border-color: rgba(156,39,176,0.5); background: rgba(156,39,176,0.07); }
    .pill[data-site="cl"].on   { color: #ff9800; border-color: rgba(255,152,0,0.5);  background: rgba(255,152,0,0.07); }
    .pill[data-site].prohibit { color: var(--red); border-color: rgba(255,82,82,0.4); background: rgba(255,82,82,0.06); }
    .pill[data-filter="active"].on  { color: var(--green);  border-color: rgba(0,230,118,0.45); background: rgba(0,230,118,0.07); }
    .pill[data-filter="starred"].on { color: var(--yellow); border-color: rgba(230,200,74,0.45); background: rgba(230,200,74,0.07); }
    .pill[data-filter="ignored"].on { color: var(--red);    border-color: rgba(255,82,82,0.45);  background: rgba(255,82,82,0.07); }
    #filters input[type=number] {
      width: 60px; background: #171717; border: 1px solid #252525; border-radius: 4px;
      padding: 0.22rem 0.4rem; color: #aaa; font-size: 0.71rem; outline: none;
    }
    #filters input[type=number]:focus { border-color: var(--accent); color: var(--text); }
    #filters input[type=number]::-webkit-inner-spin-button,
    #filters input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; }
    #filters input[type=number] { -moz-appearance: textfield; }

    /* ── Status / loading bar ── */
    #infobar {
      background: var(--bg2); border-bottom: 1px solid #1a1a1a;
      padding: 0.38rem 1.5rem; display: flex; align-items: center; gap: 0.75rem;
      flex-wrap: wrap; min-height: 2rem;
    }
    #statusbar { font-size: 0.75rem; color: var(--dim); }
    .sc { color: var(--text); font-weight: 600; }
    .nb { background: rgba(0,230,118,0.15); color: var(--green); padding: 0.07rem 0.35rem; border-radius: 4px; font-weight: 700; }
    #site-status { display: flex; gap: 0.45rem; flex-wrap: wrap; margin-left: auto; }
    .spill {
      display: flex; align-items: center; gap: 0.3rem; padding: 0.18rem 0.5rem;
      border-radius: 20px; font-size: 0.68rem; font-weight: 600; border: 1px solid #222; color: var(--dim);
      transition: all 0.2s;
    }
    .spill.loading { animation: pulse 1.2s infinite; }
    .spill.done   { color: var(--green); border-color: rgba(0,230,118,0.3); }
    .spill.error  { color: var(--red);   border-color: rgba(255,82,82,0.3); }
    .spin { width: 8px; height: 8px; border: 1.5px solid #333; border-top-color: currentColor;
            border-radius: 50%; animation: spin 0.7s linear infinite; }
    @keyframes spin  { to { transform: rotate(360deg); } }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }

    /* ── Tag bar ── */
    #tag-bar {
      padding: 0.4rem 1.5rem; border-bottom: 1px solid #161616;
      display: none; flex-wrap: wrap; gap: 0.25rem; align-items: center;
      background: #0f0f0f;
    }
    .tpill {
      padding: 0.18rem 0.55rem; border-radius: 20px; font-size: 0.68rem; font-weight: 600;
      border: 1px solid #333; color: #777; cursor: pointer; user-select: none;
      transition: all 0.12s;
    }
    .tpill:hover { border-color: #555; color: #aaa; }
    .tpill.require { color: var(--green); border-color: rgba(0,230,118,0.4); background: rgba(0,230,118,0.06); }
    .tpill.prohibit { color: var(--red);  border-color: rgba(255,82,82,0.4); background: rgba(255,82,82,0.06); }

    /* ── Cards ── */
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 1rem; padding: 1.25rem 1.5rem; }
    .seen-div {
      grid-column: 1/-1; display: flex; align-items: center; gap: 0.6rem;
      color: #2a2a2a; font-size: 0.68rem; letter-spacing: 0.14em; text-transform: uppercase; padding: 0.1rem 0;
    }
    .seen-div::before, .seen-div::after { content:''; flex:1; border-top: 1px solid #1c1c1c; }
    .card {
      background: var(--bg3); border: 1px solid var(--border); border-radius: 10px;
      overflow: hidden; transition: transform .15s, box-shadow .15s, border-color .15s, opacity .15s;
      position: relative;
    }
    .card:hover { transform: translateY(-3px); box-shadow: 0 10px 28px rgba(0,0,0,.55); border-color: #333; }
    .card.seen { opacity: 0.38; }
    .card.seen:hover { opacity: 0.72; }
    .card.out { animation: fadeout .22s ease forwards; pointer-events: none; }
    .card.starred { border-color: rgba(230,200,74,.45); }
    .card.starred:hover { border-color: rgba(230,200,74,.75); box-shadow: 0 10px 28px rgba(230,200,74,.1); }
    .card.is-ignored { opacity: 0.55; }
    .card.is-ignored:hover { opacity: 0.85; }
    .card.is-ignored .abtn.ign:hover { border-color: #4a4; color: #6c6; }
    @keyframes fadeout { to { opacity:0; transform:scale(.93); } }
    .cactions { position: absolute; top: 7px; right: 7px; display: flex; gap: 5px; z-index: 5; }
    .abtn {
      width: 26px; height: 26px; background: rgba(8,8,8,.82); border: 1px solid #3a3a3a;
      border-radius: 50%; color: #555; cursor: pointer; font-size: 0.8rem;
      display: flex; align-items: center; justify-content: center; transition: all .1s; padding: 0;
    }
    .abtn:hover { background: #1c1c1c; color: #fff; border-color: #555; }
    .abtn.ign:hover { border-color: #c44; color: #e66; }
    .abtn.str.on { color: #e6c84a; border-color: #555; background: rgba(8,8,8,.82); text-shadow: 0 0 6px rgba(230,200,74,.8); }
    .abtn.str:not(.on):hover { border-color: #e6c84a; color: #e6c84a; }
    .clink { display: block; text-decoration: none; color: inherit; }
    .cimg { height: 165px; overflow: hidden; background: #111; }
    .cimg img { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform .3s; }
    .card:hover .cimg img { transform: scale(1.04); }
    .noimg { height: 100%; display: flex; align-items: center; justify-content: center;
             color: #222; font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
    .cbody { padding: .75rem .85rem .85rem; }
    .cmeta { display: flex; align-items: center; gap: .35rem; margin-bottom: .4rem; flex-wrap: wrap; }
    .sbadge { padding: .13rem .42rem; border-radius: 4px; font-size: .63rem; font-weight: 700;
              text-transform: uppercase; letter-spacing: .06em; color: #fff; }
    .lid { font-family: monospace; font-size: .65rem; color: var(--yellow);
           background: rgba(230,200,74,.1); border: 1px solid rgba(230,200,74,.2);
           border-radius: 3px; padding: .08rem .32rem; }
    .ctitle { font-size: .88rem; font-weight: 600; color: #f0f0f0; line-height: 1.35; margin-bottom: .3rem; }
    .cprice { display: block; font-size: 1.02rem; font-weight: 700; color: var(--green); margin-bottom: .18rem; }
    .tl { display: inline-block; font-size: .72rem; font-weight: 600;
          padding: .1rem .38rem; border-radius: 4px; margin-top: .12rem; }
    .tl.active { background: rgba(0,230,118,.14); color: var(--green); }
    .tl.ended  { background: rgba(255,82,82,.1);  color: #4a4a4a; }
    .empty { grid-column:1/-1; text-align:center; color:#2e2e2e; padding:4rem; font-size:.95rem; }
    @media(max-width:640px) {
      .grid { padding: 1rem; gap: .8rem; }
      header { padding: .6rem 1rem; }
      #filters { padding: .45rem 1rem; }
      .fg { padding: 0 .6rem; }
    }
  </style>
</head>
<body>

<header>
  <div class="brand">AuctionWatch</div>
  <form id="sf">
    <div id="search-wrap">
      <input id="q" type="text" placeholder="Search auctions…" autocomplete="off">
      <div id="recent-searches"></div>
    </div>
    <button type="submit" id="search-btn">Search</button>
  </form>
  {{auth_link}}
</header>

<div id="filters">
  <div class="fg">
    <div class="pills" id="spills">
      <div class="pill on" data-site="cab"     data-label="C&amp;B">C&amp;B</div>
      <div class="pill on" data-site="bat"     data-label="BaT">BaT</div>
      <div class="pill on" data-site="hagerty" data-label="Hagerty">Hagerty</div>
      <div class="pill on" data-site="pcar"    data-label="PCar">PCar</div>
      <div class="pill on" data-site="cl"      data-label="CL">CL</div>
    </div>
  </div>
  <div class="fsep"></div>
  <div class="fg">
    <div class="pills">
      <div class="pill on" data-filter="active">Active only</div>
      <div class="pill"    data-filter="starred">★ Starred</div>
      <div class="pill"    data-filter="ignored">✕ Ignored</div>
    </div>
  </div>
  <div class="fsep"></div>
  <div class="fg">
    <span class="flabel">Year</span>
    <input type="number" id="year-lo" placeholder="Min" min="1900" max="2030" step="1">
    <span class="fdash">–</span>
    <input type="number" id="year-hi" placeholder="Max" min="1900" max="2030" step="1">
  </div>
  <div class="fsep"></div>
  <div class="fg">
    <span class="flabel">Price $</span>
    <input type="number" id="price-lo" placeholder="Min" min="0" step="500">
    <span class="fdash">–</span>
    <input type="number" id="price-hi" placeholder="Max" min="0" step="500">
  </div>
</div>

<div id="infobar">
  <div id="statusbar">Ready — enter a search query above</div>
  <div id="site-status"></div>
</div>
<div id="tag-bar"></div>
<div class="grid" id="grid"></div>

<script>
const SC = {'Cars & Bids':'#00bcd4','Bring a Trailer':'#4caf50','Hagerty':'#2196f3','PCar Market':'#9c27b0','Craigslist':'#ff9800'};
const SN = {cab:'C&B', bat:'BaT', hagerty:'Hagerty', pcar:'PCar', cl:'CL'};
let st = { bysite:{}, serverStart:'', lastQ:'', lastT:'', starred:new Set(), ignored:new Set(), tagState:new Map() };

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }

function activeSites(){ return [...document.querySelectorAll('#spills .pill')].filter(p=>!p.classList.contains('prohibit')).map(p=>p.dataset.site) }

function tlMinutes(tl){
  const t=(tl||'').trim();
  if(!t||/ended|sold|closed/i.test(t)) return Infinity;
  let m=0;
  const d=t.match(/(\d+)\s*D/i); if(d) m+=parseInt(d[1])*1440;
  const h=t.match(/(\d+)\s*H/i); if(h) m+=parseInt(h[1])*60;
  const mn=t.match(/(\d+)\s*M/i); if(mn) m+=parseInt(mn[1]);
  if(!m){
    // HH:MM:SS format (C&B, BaT)
    const ts=t.match(/(\d+):(\d{2}):\d{2}/);
    if(ts) m=parseInt(ts[1])*60+parseInt(ts[2]);
  }
  return m||Infinity;
}

function isActiveOnly()  { return !!document.querySelector('[data-filter="active"].on');  }
function isStarredOnly() { return !!document.querySelector('[data-filter="starred"].on'); }
function isIgnoredOnly() { return !!document.querySelector('[data-filter="ignored"].on'); }

function extractYear(title) {
  const m = title.match(/\b(19[0-9]{2}|20[0-2][0-9])\b/);
  return m ? parseInt(m[1]) : null;
}

function parsePrice(priceStr) {
  if(!priceStr) return null;
  const n = parseInt(priceStr.replace(/[^0-9]/g, ''));
  return isNaN(n) ? null : n;
}

['year-lo','year-hi','price-lo','price-hi'].forEach(id => {
  document.getElementById(id).addEventListener('input', render);
});

const STOP = new Set([
  'a','an','the','and','or','with','for','in','on','at','by','to','of','is','as','no','not',
  'its','this','that','are','was','has','had','been','will','but','via','my','our','your',
  'their','all','both','each','from','into','over','than','then','when','where','which',
  'who','how','why','what','one','two','three','per','sale','auction','reserve','bid',
  'car','auto','vehicle','used','new','amp','very','only','just','also','well','great',
  'nice','good','clean','rare','low','high','long','time','see','more','less',
]);

function tokenizeTitle(title) {
  return [...new Set(
    title.split(/[\s\/,()\[\]&+#@!?:;'"]+/)
      .map(t => t.toLowerCase().replace(/[^a-z0-9.-]/g, '').replace(/^\.+|\.+$/g, ''))
      .filter(t => t.length >= 2)
      .filter(t => !/^(19|20)\d{2}$/.test(t))
      .filter(t => !STOP.has(t))
  )];
}

function renderTagBar(visibleListings) {
  const bar = document.getElementById('tag-bar');
  // Build counts only from non-CL listings that are currently visible.
  // This ensures tags always correspond to results the user can see, and
  // CL-only terms never pollute the tag bar.
  const nonCL = visibleListings.filter(l => l.source !== 'Craigslist');
  if(nonCL.length < 2) { bar.style.display='none'; return; }
  const counts = new Map();
  for(const l of nonCL) {
    for(const t of tokenizeTitle(l.title)) counts.set(t, (counts.get(t)||0) + 1);
  }
  const tags = [...counts.entries()]
    .filter(([,n]) => n >= 2 && n < nonCL.length)
    .sort((a,b) => a[0].localeCompare(b[0]))
    .slice(0, 60)
    .map(([t]) => t);
  if(!tags.length) { bar.style.display='none'; return; }
  bar.style.display = 'flex';
  bar.innerHTML = tags.map(t => {
    const s = st.tagState.get(t)||null;
    const cls = s ? ' '+s : '';
    const suffix = s==='require' ? ' ✓' : s==='prohibit' ? ' ✕' : '';
    return `<span class="tpill${cls}" data-tag="${esc(t)}">${esc(t)}${suffix}</span>`;
  }).join('');
}

document.getElementById('tag-bar').addEventListener('click', e => {
  const pill = e.target.closest('.tpill');
  if(!pill) return;
  const tag = pill.dataset.tag;
  const cur = st.tagState.get(tag)||null;
  const next = cur===null ? 'require' : cur==='require' ? 'prohibit' : null;
  if(next===null) st.tagState.delete(tag); else st.tagState.set(tag, next);
  render();
});

function allListings(){
  const activeOnly  = isActiveOnly();
  const starredOnly = isStarredOnly();
  const ignoredOnly = isIgnoredOnly();
  const siteKey = {'Cars & Bids':'cab','Bring a Trailer':'bat','Hagerty':'hagerty','PCar Market':'pcar','Craigslist':'cl'};
  const reqSites  = new Set([...document.querySelectorAll('#spills .pill.on')].map(p=>p.dataset.site));
  const probSites = new Set([...document.querySelectorAll('#spills .pill.prohibit')].map(p=>p.dataset.site));
  let all = ['cab','bat','hagerty','pcar','cl'].filter(k=>st.bysite[k]).flatMap(k=>st.bysite[k]);
  all = all.filter(l => {
    const k = siteKey[l.source]||'';
    if(probSites.has(k)) return false;
    if(reqSites.size > 0 && !reqSites.has(k)) return false;
    return true;
  });
  if(activeOnly)  all = all.filter(l => { const t=l.time_left||''; if(!t) return true; return /\d/.test(t) && !/ended|sold|closed/i.test(t); });
  if(ignoredOnly) all = all.filter(l =>  st.ignored.has(l.short_id));
  else            all = all.filter(l => !st.ignored.has(l.short_id));
  if(starredOnly) all = all.filter(l => st.starred.has(l.short_id));
  // Year filter
  const yloV = document.getElementById('year-lo').value;
  const yhiV = document.getElementById('year-hi').value;
  if(yloV || yhiV) {
    all = all.filter(l => {
      const y = extractYear(l.title); if(y===null) return true;
      if(yloV && y < parseInt(yloV)) return false;
      if(yhiV && y > parseInt(yhiV)) return false;
      return true;
    });
  }
  // Price filter
  const ploV = document.getElementById('price-lo').value;
  const phiV = document.getElementById('price-hi').value;
  if(ploV || phiV) {
    all = all.filter(l => {
      const p = parsePrice(l.price); if(p===null) return true;
      if(ploV && p < parseInt(ploV)) return false;
      if(phiV && p > parseInt(phiV)) return false;
      return true;
    });
  }
  // Tag filters
  for(const [tag, state] of st.tagState) {
    const re = new RegExp('\\b' + tag.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '\\b', 'i');
    if(state==='require')  all = all.filter(l => re.test(l.title));
    if(state==='prohibit') all = all.filter(l => !re.test(l.title));
  }
  return all.sort((a,b)=>tlMinutes(a.time_left)-tlMinutes(b.time_left));
}

function startIdx(listings){
  if(!st.serverStart) return null;
  const i = listings.findIndex(l=>l.short_id===st.serverStart);
  return i>=0 ? i : null;
}

function tlHtml(l){
  if(!l.time_left) return '';
  const t=l.time_left.toLowerCase(), cls=/ended|sold|closed/.test(t)?'ended':/\d/.test(t)?'active':'';
  return cls ? `<span class="tl ${cls}">${esc(l.time_left)}</span>` : '';
}

function cardHtml(l, seen){
  const c=SC[l.source]||'#888';
  const starred = st.starred.has(l.short_id);
  const ignored = st.ignored.has(l.short_id);
  const img=l.image_url
    ? `<img src="${esc(l.image_url)}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=noimg>No image</div>'">`
    : '<div class="noimg">No image</div>';
  return `<div class="card${seen?' seen':''}${starred?' starred':''}${ignored?' is-ignored':''}" data-id="${l.short_id}">
  <div class="cactions">
    <button class="abtn ign" onclick="toggleIgnore('${l.short_id}',event)" title="${ignored?'Unignore':'Ignore'}">✕</button>
    <button class="abtn str${starred?' on':''}" onclick="starCard('${l.short_id}',event)" title="Star">★</button>
  </div>
  <a class="clink" href="${esc(l.url)}" target="_blank" rel="noopener">
    <div class="cimg">${img}</div>
    <div class="cbody">
      <div class="cmeta">
        <span class="lid">${l.short_id}</span>
        <span class="sbadge" style="background:${c}">${esc(l.source)}</span>
      </div>
      <div class="ctitle">${esc(l.title)}</div>
      ${l.price?`<span class="cprice">${esc(l.price)}</span>`:''}
      ${tlHtml(l)}
    </div>
  </a>
</div>`;
}

function stateToUrl() {
  const p = new URLSearchParams();
  const q = document.getElementById('q').value.trim();
  if(q) p.set('q', q);
  // Sites: only encode if not all-required (the default)
  const req=[], proh=[];
  document.querySelectorAll('#spills .pill').forEach(pill => {
    if(pill.classList.contains('on')) req.push(pill.dataset.site);
    else if(pill.classList.contains('prohibit')) proh.push(pill.dataset.site);
  });
  const allSites = ['cab','bat','hagerty','pcar','cl'];
  if(req.length < allSites.length || proh.length > 0) {
    if(req.length)  p.set('s',  req.join(','));
    if(proh.length) p.set('xs', proh.join(','));
  }
  // Filter pills: only encode non-defaults (active defaults ON, others OFF)
  if(!document.querySelector('[data-filter="active"].on'))   p.set('active',   '0');
  if(document.querySelector('[data-filter="starred"].on'))   p.set('starred',  '1');
  if(document.querySelector('[data-filter="ignored"].on'))   p.set('ignored',  '1');
  // Ranges
  const rangeMap = {'ylo':'year-lo','yhi':'year-hi','plo':'price-lo','phi':'price-hi'};
  for(const [key,id] of Object.entries(rangeMap)) { const v=document.getElementById(id).value; if(v) p.set(key,v); }
  // Tag states
  const tr=[], tp=[];
  for(const [tag,state] of st.tagState) { if(state==='require') tr.push(tag); else if(state==='prohibit') tp.push(tag); }
  if(tr.length) p.set('tr', tr.join(','));
  if(tp.length) p.set('tp', tp.join(','));
  const qs = p.toString();
  history.replaceState(null, '', qs ? '?'+qs : location.pathname);
}

function urlToState() {
  const p = new URLSearchParams(location.search);
  // Query
  const q = p.get('q') || '';
  if(q) document.getElementById('q').value = q;
  // Sites
  const s  = p.get('s'),  xs = p.get('xs');
  if(s !== null || xs !== null) {
    const req  = new Set((s  || '').split(',').filter(Boolean));
    const proh = new Set((xs || '').split(',').filter(Boolean));
    document.querySelectorAll('#spills .pill').forEach(pill => {
      const site = pill.dataset.site;
      pill.classList.remove('on','prohibit');
      if(proh.has(site))      { pill.classList.add('prohibit'); pill.textContent = pill.dataset.label+' ✕'; }
      else if(req.has(site))  { pill.classList.add('on');       pill.textContent = pill.dataset.label+' ✓'; }
      else                    {                                  pill.textContent = pill.dataset.label; }
    });
  }
  // Filter pills
  if(p.get('active')  === '0') document.querySelector('[data-filter="active"]')?.classList.remove('on');
  if(p.get('starred') === '1') document.querySelector('[data-filter="starred"]')?.classList.add('on');
  if(p.get('ignored') === '1') document.querySelector('[data-filter="ignored"]')?.classList.add('on');
  // Ranges
  const rangeMap = {'ylo':'year-lo','yhi':'year-hi','plo':'price-lo','phi':'price-hi'};
  for(const [key,id] of Object.entries(rangeMap)) { const v=p.get(key); if(v) document.getElementById(id).value=v; }
  // Tags
  for(const tag of (p.get('tr')||'').split(',').filter(Boolean)) st.tagState.set(tag,'require');
  for(const tag of (p.get('tp')||'').split(',').filter(Boolean)) st.tagState.set(tag,'prohibit');
  return q;
}

function render(){
  const listings = allListings();
  renderTagBar(listings);
  const si = startIdx(listings);
  const grid = document.getElementById('grid');
  if(!listings.length){ grid.innerHTML=''; stateToUrl(); return; }
  let html='';
  for(let i=0;i<listings.length;i++){
    if(si!==null && i===si) html+='<div class="seen-div"><span>seen below</span></div>';
    html+=cardHtml(listings[i], si!==null && i>=si);
  }
  grid.innerHTML=html;
  const newN = si!==null ? si : listings.length;
  const bar = document.getElementById('statusbar');
  bar.innerHTML = `<span class="sc">${listings.length} result${listings.length!==1?'s':''}</span>`
    + (si!==null ? ` <span class="nb">${newN} new</span>` : '')
    + (st.lastQ ? ` &nbsp;for <em>"${esc(st.lastQ)}"</em>` : '')
    + (st.lastT ? ` &nbsp;&middot; ${st.lastT}` : '');
  stateToUrl();
}

function setSitePill(site, cls, text){
  const ss=document.getElementById('site-status');
  let el=ss.querySelector(`[data-s="${site}"]`);
  if(!el){ el=document.createElement('div'); el.dataset.s=site; ss.appendChild(el); }
  el.className=`spill ${cls}`;
  el.innerHTML=cls==='loading'?`<div class="spin"></div> ${text}`:text;
}

function doSearch(e){
  if(e) e.preventDefault();
  const q=document.getElementById('q').value.trim();
  if(!q) return;
  const sites=activeSites();
  if(!sites.length) return;
  if(st.es){ st.es.close(); st.es=null; }
  st.bysite={}; st.lastQ=q; st.lastT=''; st.tagState=new Map();
  stateToUrl();
  document.getElementById('search-btn').disabled=true;
  document.getElementById('grid').innerHTML='';
  document.getElementById('site-status').innerHTML='';
  document.getElementById('tag-bar').style.display='none';
  const activeOnly=!!document.querySelector('[data-filter="active"].on');
  const sp=sites.map(s=>`sites=${encodeURIComponent(s)}`).join('&');
  const url=`/api/search/stream?q=${encodeURIComponent(q)}&${sp}${activeOnly?'&active=1':''}`;
  sites.forEach(s=>setSitePill(s,'loading',SN[s]));
  const es=new EventSource(url);
  st.es=es;
  es.addEventListener('site',ev=>{
    const d=JSON.parse(ev.data);
    st.bysite[d.site]=d.listings||[];
    setSitePill(d.site, d.error?'error':'done', (d.error?'✕ ': d.listings.length+' · ')+SN[d.site]);
    render();
  });
  es.addEventListener('done',ev=>{
    const d=JSON.parse(ev.data);
    st.serverStart=d.start_id||'';
    st.ignored=new Set(d.ignored||[]);
    st.lastT=new Date().toLocaleTimeString();
    es.close(); st.es=null;
    document.getElementById('search-btn').disabled=false;
    render();
    fetch('/api/searches').then(r=>r.json()).then(d=>{ recentSearches = d.searches||[]; });
  });
  es.onerror=()=>{ es.close(); st.es=null; document.getElementById('search-btn').disabled=false; };
}

async function toggleIgnore(id, e){
  e.preventDefault(); e.stopPropagation();
  const nowIgnored = !st.ignored.has(id);
  if(nowIgnored) st.ignored.add(id); else st.ignored.delete(id);
  // Card disappears from current view if it no longer matches the filter
  const willDisappear = isIgnoredOnly() ? !nowIgnored : nowIgnored;
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if(card && willDisappear){
    card.classList.add('out');
    setTimeout(()=>render(), 230);
  } else {
    render();
  }
  await fetch('/api/ignore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,ignored:nowIgnored})});
}

async function setStart(id, e){
  e.preventDefault(); e.stopPropagation();
  st.serverStart=id;
  await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  render();
}

async function starCard(id, e){
  e.preventDefault(); e.stopPropagation();
  const nowStarred = !st.starred.has(id);
  if(nowStarred) st.starred.add(id); else st.starred.delete(id);
  const card=document.querySelector(`.card[data-id="${id}"]`);
  if(card){
    card.classList.toggle('starred', nowStarred);
    const btn=card.querySelector('.abtn.str');
    if(btn) btn.classList.toggle('on', nowStarred);
  }
  await fetch('/api/star',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,starred:nowStarred})});
}

document.getElementById('sf').addEventListener('submit', doSearch);

// Site pills: three-state cycle require(.on) → neutral → prohibit → require
document.querySelectorAll('#spills .pill').forEach(p=>p.addEventListener('click',()=>{
  const cur = p.classList.contains('on') ? 'require' : p.classList.contains('prohibit') ? 'prohibit' : 'neutral';
  const next = cur==='require' ? 'neutral' : cur==='neutral' ? 'prohibit' : 'require';
  p.classList.remove('on','prohibit');
  if(next==='require') p.classList.add('on');
  if(next==='prohibit') p.classList.add('prohibit');
  p.textContent = p.dataset.label + (next==='require' ? ' ✓' : next==='prohibit' ? ' ✕' : '');
  render();
}));

// Filter pills: simple toggle
document.querySelectorAll('.pill[data-filter]').forEach(p=>p.addEventListener('click',()=>{
  p.classList.toggle('on');
  // Starred and Ignored are mutually exclusive
  if(p.dataset.filter==='starred' && p.classList.contains('on'))
    document.querySelector('[data-filter="ignored"]')?.classList.remove('on');
  else if(p.dataset.filter==='ignored' && p.classList.contains('on'))
    document.querySelector('[data-filter="starred"]')?.classList.remove('on');
  render();
}));

// ── Recent searches dropdown ──────────────────────────────────────────────────
let recentSearches = [];
const rsEl = document.getElementById('recent-searches');
const qEl  = document.getElementById('q');

function showRecentSearches() {
  if(!recentSearches.length) return;
  rsEl.innerHTML = recentSearches.map(q =>
    `<div class="rs-item" data-q="${esc(q)}"><span class="rs-icon">↩</span>${esc(q)}</div>`
  ).join('');
  rsEl.classList.add('open');
}

rsEl.addEventListener('mousedown', e => {
  const item = e.target.closest('.rs-item');
  if(!item) return;
  e.preventDefault(); // prevent blur from firing before click
  qEl.value = item.dataset.q;
  rsEl.classList.remove('open');
  doSearch(null);
});

qEl.addEventListener('focus', () => { if(recentSearches.length) showRecentSearches(); });
qEl.addEventListener('blur',  () => { setTimeout(() => rsEl.classList.remove('open'), 150); });
qEl.addEventListener('input', () => { rsEl.classList.remove('open'); });

// Restore state from URL, then pre-load user data and auto-search if query present
const initQ = urlToState();
fetch('/api/store').then(r=>r.json()).then(d=>{
  st.serverStart=d.start||''; st.starred=new Set(d.starred||[]); st.ignored=new Set(d.ignored||[]);
  if(initQ) doSearch(null);
});
fetch('/api/searches').then(r=>r.json()).then(d=>{ recentSearches = d.searches||[]; });
</script>
</body>
</html>
"""


def serve_web(initial_query: str = "", port: int = 5173):
    try:
        from flask import Flask, Response, request as freq, jsonify
    except ImportError:
        if HAS_RICH:
            _console.print("flask not installed — run: pip install flask", style="red bold")
        else:
            print("  [ERROR] flask not installed — run: pip install flask", file=sys.stderr)
        sys.exit(1)

    from auctionwatch import ALL_SITES, _listing_json

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    app.secret_key = _get_secret_key()
    _init_db()

    def _uid():
        """Return current user_id from session, or None."""
        from flask import session as fsession
        return fsession.get("user_id")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        from flask import request as freq2, session as fsession, redirect
        if freq2.method == "POST":
            username = freq2.form.get("username", "").strip()
            if not username:
                return _LOGIN_HTML.replace("{{error}}", "Please enter a username")
            uid = _db_get_or_create_user(username)
            fsession["user_id"] = uid
            fsession["username"] = username
            return redirect("/")
        return _LOGIN_HTML.replace("{{error}}", "")

    @app.route("/logout")
    def logout():
        from flask import session as fsession, redirect
        fsession.clear()
        return redirect("/")

    @app.route("/")
    def index():
        uid = _uid()
        if uid:
            from flask import session as fsession
            uname = fsession.get("username", "")
            auth_link = (
                f'<span style="margin-left:auto;font-size:.75rem;color:#555;white-space:nowrap">'
                f'{uname} &nbsp;·&nbsp; '
                f'<a href="/logout" style="color:#444;text-decoration:none" '
                f'onmouseover="this.style.color=\'#888\'" onmouseout="this.style.color=\'#444\'">Sign out</a>'
                f'</span>'
            )
        else:
            auth_link = (
                '<a href="/login" style="margin-left:auto;font-size:.75rem;color:#444;text-decoration:none;white-space:nowrap"'
                ' onmouseover="this.style.color=\'#888\'" onmouseout="this.style.color=\'#444\'">Sign in</a>'
            )
        return _WEB_HTML.replace("{{auth_link}}", auth_link)

    @app.route("/api/search/stream")
    def search_stream():
        q       = freq.args.get("q", "").strip()
        sites   = freq.args.getlist("sites") or list(ALL_SITES.keys())
        act_only = freq.args.get("active") == "1"

        if not q:
            return jsonify({"error": "no query"}), 400

        uid = _uid()
        if uid:
            _db_save_search(uid, q)
        ignored  = _db_get_ignored(uid) if uid else set()
        start_id = _db_get_start(uid)   if uid else ""

        result_q: queue.Queue = queue.Queue()

        def _run():
            async def _scrape():
                active = {k: v for k, v in ALL_SITES.items() if k in sites}
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    ctx = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 900},
                    )
                    pages = await asyncio.gather(*[ctx.new_page() for _ in active])

                    async def _one(i, key, name, scraper_fn):
                        try:
                            listings = await scraper_fn(pages[i], q, False)
                            if act_only:
                                listings = [l for l in listings if l.is_active is not False]
                            result_q.put({"site": key, "listings": [_listing_json(l) for l in listings]})
                        except Exception as exc:
                            result_q.put({"site": key, "listings": [], "error": str(exc)})

                    await asyncio.gather(*[
                        _one(i, k, name, fn)
                        for i, (k, (name, _, fn)) in enumerate(active.items())
                    ])
                    await browser.close()
                result_q.put(None)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_scrape())
            finally:
                loop.close()

        threading.Thread(target=_run, daemon=True).start()

        def _generate():
            while True:
                item = result_q.get()
                if item is None:
                    done = json.dumps({"start_id": start_id, "ignored": list(ignored)})
                    yield f"event: done\ndata: {done}\n\n"
                    break
                yield f"event: site\ndata: {json.dumps(item)}\n\n"

        return Response(
            _generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/ignore", methods=["POST"])
    def api_ignore():
        uid = _uid()
        lid     = (freq.json or {}).get("id", "")
        ignored = (freq.json or {}).get("ignored", True)
        if uid and lid:
            _db_set_ignored(uid, lid, ignored)
        return jsonify({"ok": True})

    @app.route("/api/start", methods=["POST"])
    def api_start():
        uid = _uid()
        lid = (freq.json or {}).get("id", "")
        if uid and lid:
            _db_set_start(uid, lid)
        return jsonify({"ok": True})

    @app.route("/api/star", methods=["POST"])
    def api_star():
        uid = _uid()
        lid     = (freq.json or {}).get("id", "")
        starred = (freq.json or {}).get("starred", True)
        if uid and lid:
            _db_set_starred(uid, lid, starred)
        return jsonify({"ok": True})

    @app.route("/api/store")
    def api_store():
        uid = _uid()
        if uid:
            return jsonify({"ignored": list(_db_get_ignored(uid)),
                            "start":   _db_get_start(uid),
                            "starred": list(_db_get_starred(uid))})
        return jsonify({"ignored": [], "start": "", "starred": []})

    @app.route("/api/searches")
    def api_searches():
        uid = _uid()
        return jsonify({"searches": _db_get_searches(uid) if uid else []})

    # In a server environment (Railway etc.) PORT is set; bind publicly and skip browser open
    server_port = int(os.environ.get("PORT", port))
    is_server   = "PORT" in os.environ
    host        = "0.0.0.0" if is_server else "127.0.0.1"

    url = f"http://{host}:{server_port}"
    if HAS_RICH:
        _console.print(f"\n[bold cyan]AuctionWatch[/bold cyan] → [bold]{url}[/bold]   (Ctrl+C to stop)\n")
    else:
        print(f"\nServing at {url}  (Ctrl+C to stop)")

    if not is_server:
        launch_url = f"http://127.0.0.1:{server_port}" + (f"?q={quote_plus(initial_query)}" if initial_query else "")
        webbrowser.open(launch_url)

    app.run(host=host, port=server_port, debug=False, threaded=True)
