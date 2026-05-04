
// ─── state ────────────────────────────────────────────────────────────────────
let DATA = null;
let TEAMS = [];
let MAX_GW = 1;
let CUR_GW = 1;
let CHART_OBJ = null;
const HIDDEN = new Set();

// ─── bootstrap ────────────────────────────────────────────────────────────────
async function loadData() {
  const r = await fetch('data.json?' + Date.now());
  if (!r.ok) throw new Error('data.json returned HTTP ' + r.status);
  return r.json();
}

async function init() {
  try {
    DATA   = await loadData();
    TEAMS  = DATA.teams;
    MAX_GW = DATA.metadata.current_gw;
    CUR_GW = MAX_GW;

    document.getElementById('loading-state').style.display = 'none';
    document.getElementById('gw-badge').textContent = 'GW ' + MAX_GW + ' ✓';
    document.getElementById('header-sub').textContent =
      '2025/26 Season \xB7 The worst team wins \xB7 Through GW' + MAX_GW +
      ' \xB7 Updated ' + fmtDate(DATA.metadata.last_updated);

    showTab('standings', document.querySelector('.tab.active'));
    renderAll();
  } catch(e) {
    document.getElementById('loading-state').style.display = 'none';
    const el = document.getElementById('error-state');
    el.style.display = 'block';
    el.textContent = 'Failed to load data.json: ' + e.message +
      '. Run fetch_data.py first, then open this file via a local server or file://.';
  }
}

async function reloadData(btn) {
  btn.disabled = true;
  const spin = btn.querySelector('.spin');
  spin.style.animation = 'spin 1s linear infinite';
  try {
    DATA   = await loadData();
    TEAMS  = DATA.teams;
    MAX_GW = DATA.metadata.current_gw;
    CUR_GW = MAX_GW;
    document.getElementById('gw-badge').textContent = 'GW ' + MAX_GW + ' ✓';
    document.getElementById('header-sub').textContent =
      '2025/26 Season \xB7 The worst team wins \xB7 Through GW' + MAX_GW +
      ' \xB7 Updated ' + fmtDate(DATA.metadata.last_updated);
    if (CHART_OBJ) { CHART_OBJ.destroy(); CHART_OBJ = null; }
    renderAll();
  } catch(e) {
    alert('Reload failed: ' + e.message);
  } finally {
    btn.disabled = false;
    spin.style.animation = '';
  }
}

function renderAll() {
  renderStandings();
  renderGWButtons();
  renderGWScores(CUR_GW);
  renderChart();
  renderStats();
}

// ─── helpers ──────────────────────────────────────────────────────────────────
function gwEntry(team, gw) {
  return team.gws.find(g => g.gw === gw) || null;
}

function lastNPts(team, n) {
  return team.gws.slice(-n).map(g => g.pts).filter(p => p != null);
}

function mean(arr) {
  return arr.length ? arr.reduce((a,b)=>a+b,0)/arr.length : null;
}

function fmtDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
  } catch { return iso; }
}

const CHIP_META = {
  wildcard: {cls:'wc', label:'WC', full:'Wildcard'},
  freehit:  {cls:'fh', label:'FH', full:'Free Hit'},
  bboost:   {cls:'bb', label:'BB', full:'Bench Boost'},
  '3xc':    {cls:'tc', label:'TC', full:'Triple Cap'},
};
function chipMeta(chip) { return CHIP_META[chip?.toLowerCase()] || null; }

function ptsColor(pts) {
  if (pts == null) return 'var(--muted)';
  if (pts < 20)  return '#4ade80';
  if (pts < 28)  return 'var(--green)';
  if (pts < 38)  return 'var(--text)';
  if (pts < 50)  return 'var(--gold)';
  return 'var(--red)';
}

// ─── tab switching ────────────────────────────────────────────────────────────
function showTab(name, btn) {
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display='none');
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  const panel = document.getElementById('tab-'+name);
  if (panel) panel.style.display = 'block';
  if (btn) btn.classList.add('active');
}

// ─── STANDINGS ────────────────────────────────────────────────────────────────
function renderStandings() {
  document.getElementById('standings-title').textContent =
    'League Table — GW' + MAX_GW + ' \xB7 Ranked by Total (lowest = best)';

  const sorted = [...TEAMS].sort((a,b) => {
    const ta = a.summary?.total_points ?? 99999;
    const tb = b.summary?.total_points ?? 99999;
    return ta - tb;
  });

  const tbody = document.getElementById('standings-body');
  tbody.innerHTML = sorted.map((team, idx) => {
    const cur  = gwEntry(team, MAX_GW);
    const prev = gwEntry(team, MAX_GW - 1);

    const standing = cur?.standing ?? idx + 1;
    const prevStanding = prev?.standing ?? standing;
    const diff = prevStanding - standing; // positive = improved

    const rankCls = idx===0?'top1':idx===1?'top2':idx===2?'top3':'';
    const movHtml = diff===0
      ? `<span class="move-badge move-eq">–</span>`
      : diff>0
        ? `<span class="move-badge move-up">▲${diff}</span>`
        : `<span class="move-badge move-dn">▼${Math.abs(diff)}</span>`;

    const total  = team.summary?.total_points ?? '?';
    const gwPts  = cur?.pts ?? '—';
    const gwRank = cur?.mini_rank ?? '—';
    const avg3   = mean(lastNPts(team,3));
    const avg5   = mean(lastNPts(team,5));
    const form   = team.gws.slice(-5).map(g=>g.pts).filter(p=>p!=null);

    const formHtml = form.map(p => {
      const h = Math.max(8, Math.min(24, 24 - (p-10)/2));
      return `<div style="width:7px;height:${h}px;background:${ptsColor(p)};border-radius:2px;opacity:.85" title="${p} pts"></div>`;
    }).join('');

    // Chips remaining
    const used = team.summary?.chips_used || [];
    const count = k => used.filter(c=>c.chip===k).length;
    const chipPips = [
      {k:'wildcard',max:2,cl:'wc',lb:'WC'},
      {k:'freehit', max:2,cl:'fh',lb:'FH'},
      {k:'bboost',  max:2,cl:'bb',lb:'BB'},
      {k:'3xc',     max:2,cl:'tc',lb:'TC'},
    ].map(({k,max,cl,lb}) =>
      `<div class="chip-row">${Array.from({length:max},(_,i)=>
        `<span class="chip-pip ${i<count(k)?'used':'avail-'+cl}" title="${lb}">${lb}</span>`
      ).join('')}</div>`
    ).join('');

    return `<tr>
      <td><div class="rank-cell">
        <span class="rank-num ${rankCls}">${standing}</span>${movHtml}
      </div></td>
      <td><div class="team-row">
        <span class="dot" style="background:${team.color}"></span>
        <div class="team-info">
          <div class="team-name">${esc(team.team_name)}</div>
          <div class="manager">${esc(team.manager)}</div>
        </div>
      </div></td>
      <td><div class="total-pts">${total}</div></td>
      <td class="num">${gwPts}</td>
      <td class="num">${gwRank}</td>
      <td class="num">${avg3!=null?avg3.toFixed(1):'—'}</td>
      <td class="num">${avg5!=null?avg5.toFixed(1):'—'}</td>
      <td><div class="chips-cell">${chipPips}</div></td>
      <td><div style="display:flex;align-items:flex-end;gap:2px;height:24px">${formHtml}</div></td>
    </tr>`;
  }).join('');
}

// ─── GW SCORES ────────────────────────────────────────────────────────────────
function renderGWButtons() {
  const wrap = document.getElementById('gw-buttons');
  wrap.innerHTML = Array.from({length:MAX_GW},(_,i)=>i+1).map(gw =>
    `<button class="gw-btn${gw===CUR_GW?' active':''}" onclick="selectGW(${gw})">${gw}</button>`
  ).join('');
}

function selectGW(gw) {
  CUR_GW = gw;
  document.querySelectorAll('.gw-btn').forEach((btn,i)=>{
    btn.classList.toggle('active', i+1===gw);
  });
  document.getElementById('prev-gw').disabled = gw <= 1;
  document.getElementById('next-gw').disabled = gw >= MAX_GW;
  renderGWScores(gw);
}

function changeGW(delta) { selectGW(Math.max(1,Math.min(MAX_GW,CUR_GW+delta))); }

function renderGWScores(gw) {
  document.getElementById('prev-gw').disabled = gw <= 1;
  document.getElementById('next-gw').disabled = gw >= MAX_GW;

  // Sort by pts ascending (lowest = best)
  const entries = TEAMS.map(team => ({team, g: gwEntry(team, gw)}))
    .filter(({g})=>g!=null)
    .sort((a,b)=>(a.g.pts??999)-(b.g.pts??999));

  const grid = document.getElementById('score-grid');
  grid.innerHTML = entries.map(({team, g}, rankIdx) => {
    const chipKey = g.chip?.toLowerCase() || '';
    const cm = chipMeta(chipKey);
    const chipHtml = cm ? `<span class="chip-badge chip-${cm.cls}">${cm.full}</span>` : '';

    const pens = [];
    if (g.cvc_pens)      pens.push(`<div class="sc-pen-row"><span class="sc-pen-label">C/VC Pen</span><span class="sc-pen-val">+${g.cvc_pens}</span></div>`);
    if (g.inactive_pens) pens.push(`<div class="sc-pen-row"><span class="sc-pen-label">Inactive</span><span class="sc-pen-val">+${g.inactive_pens}</span></div>`);
    if (g.xfer_cost_pens) pens.push(`<div class="sc-pen-row"><span class="sc-pen-label">Xfer Cost</span><span class="sc-pen-val">+${g.xfer_cost_pens}</span></div>`);
    const pensHtml = pens.length ? `<div class="sc-pens">${pens.join('')}</div>` : '';

    const gwRankLabel = g.mini_rank
      ? `<span style="font-size:10px;color:var(--muted);font-family:'DM Mono',monospace">rank ${g.mini_rank}/${TEAMS.length}</span>`
      : '';

    return `<div class="score-card">
      <div class="sc-rank">#${rankIdx+1} this GW ${gwRankLabel}</div>
      <div class="sc-team">
        <span class="dot" style="background:${team.color}"></span>
        ${esc(team.team_name)} ${chipHtml}
      </div>
      <div class="sc-manager">${esc(team.manager)}</div>
      <div class="sc-pts" style="color:${ptsColor(g.pts)}">${g.pts??'—'}</div>
      <div class="sc-total">Total: ${g.total??'—'} pts &middot; Standing: ${g.standing??'—'}</div>
      ${pensHtml}
    </div>`;
  }).join('');
}

// ─── SEASON CHART ─────────────────────────────────────────────────────────────
function renderChart() {
  if (CHART_OBJ) { CHART_OBJ.destroy(); CHART_OBJ = null; }

  const labels = Array.from({length:MAX_GW},(_,i)=>`GW${i+1}`);

  const datasets = TEAMS.map(team => {
    const pts = Array.from({length:MAX_GW},(_,i)=>{
      const g = gwEntry(team, i+1);
      return g?.total ?? null;
    });
    return {
      label: team.manager,
      data: pts,
      borderColor: team.color,
      backgroundColor: team.color+'22',
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 5,
      tension: 0.3,
      hidden: HIDDEN.has(team.id),
      spanGaps: true,
    };
  });

  const ctx = document.getElementById('season-chart').getContext('2d');
  CHART_OBJ = new Chart(ctx, {
    type: 'line',
    data: {labels, datasets},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {mode:'index', intersect:false},
      plugins: {
        legend: {display:false},
        tooltip: {
          backgroundColor:'#0f1623',
          borderColor:'#1e2d44',
          borderWidth:1,
          titleColor:'#e2e8f0',
          bodyColor:'#94a3b8',
          padding:10,
          callbacks: {
            title: items => items[0].label,
            label: item => ` ${item.dataset.label}: ${item.raw} pts`,
            afterBody: items => {
              const sorted = [...items].sort((a,b)=>a.raw-b.raw);
              return sorted.map((it,i) => `  ${i+1}. ${it.dataset.label}`);
            },
          }
        },
      },
      scales: {
        x: {grid:{color:'rgba(30,45,68,.6)'},ticks:{color:'#475569',font:{size:10}}},
        y: {
          grid:{color:'rgba(30,45,68,.6)'},
          ticks:{color:'#475569',font:{size:10}},
          title:{display:true,text:'Cumulative Points (lower = better)',color:'#475569',font:{size:11}},
        }
      }
    }
  });

  // Custom legend
  const legEl = document.getElementById('chart-legend');
  legEl.innerHTML = TEAMS.map(team => `
    <div class="legend-item${HIDDEN.has(team.id)?' hidden':''}" onclick="toggleTeam(${team.id},this)">
      <span class="legend-dot" style="background:${team.color}"></span>
      <span>${esc(team.manager)}</span>
    </div>`).join('');
}

function toggleTeam(teamId, el) {
  if (HIDDEN.has(teamId)) HIDDEN.delete(teamId);
  else HIDDEN.add(teamId);
  el.classList.toggle('hidden');
  const ds = CHART_OBJ.data.datasets.find(d => {
    const t = TEAMS.find(t=>t.id===teamId);
    return t && d.label===t.manager;
  });
  if (ds) { ds.hidden = HIDDEN.has(teamId); CHART_OBJ.update(); }
}

// ─── STATS ────────────────────────────────────────────────────────────────────
function renderStats() {
  renderDistribution();
  renderChipTable();
  renderPenalties();
  renderTopScores();
}

function renderDistribution() {
  // For each team: scores < 20, < 30, min, max, range, > 40, > 50
  const sorted = [...TEAMS].sort((a,b)=>(a.summary?.total_points??99999)-(b.summary?.total_points??99999));
  const allMin = Math.min(...sorted.map(t => t.summary?.best_gw ?? 999));
  const allMax = Math.max(...sorted.map(t => t.summary?.worst_gw ?? 0));

  document.getElementById('dist-body').innerHTML = sorted.map(team => {
    const pts = team.gws.map(g=>g.pts).filter(p=>p!=null);
    const lt20 = pts.filter(p=>p<20).length;
    const lt30 = pts.filter(p=>p<30).length;
    const gt40 = pts.filter(p=>p>40).length;
    const gt50 = pts.filter(p=>p>50).length;
    const mn   = team.summary?.best_gw ?? Math.min(...pts);
    const mx   = team.summary?.worst_gw ?? Math.max(...pts);
    const rng  = mx - mn;
    return `<tr>
      <td class="team-col"><span class="dot" style="background:${team.color}"></span>${esc(team.manager)}</td>
      <td class="${lt20===Math.max(...sorted.map(t=>t.gws.filter(g=>g.pts<20).length))?'hi-lo':''}">${lt20}</td>
      <td>${lt30}</td>
      <td class="${mn===allMin?'hi-lo':mn===allMax?'hi-hi':''}">${mn}</td>
      <td class="${mx===allMax?'hi-hi':mx===allMin?'hi-lo':''}">${mx}</td>
      <td>${rng}</td>
      <td class="${gt40===Math.max(...sorted.map(t=>t.gws.filter(g=>g.pts>40).length))?'hi-hi':''}">${gt40}</td>
      <td class="${gt50===Math.max(...sorted.map(t=>t.gws.filter(g=>g.pts>50).length))?'hi-hi':''}">${gt50}</td>
    </tr>`;
  }).join('');
}

function renderChipTable() {
  const TYPES = ['wildcard','freehit','bboost','3xc'];
  const sorted = [...TEAMS].sort((a,b)=>(a.summary?.total_points??99999)-(b.summary?.total_points??99999));

  // Per chip type, find best user (lowest pts = best)
  const bests = {};
  TYPES.forEach(k => {
    const vals = [];
    sorted.forEach(team => {
      (team.summary?.chips_used||[]).filter(c=>c.chip===k).forEach(({gw}) => {
        const g = gwEntry(team, gw);
        if (g?.pts!=null) vals.push({teamId:team.id, pts:g.pts, gw});
      });
    });
    bests[k] = vals.length ? vals.reduce((b,c)=>c.pts<b.pts?c:b) : null;
  });

  document.getElementById('chip-table-body').innerHTML = sorted.map(team => {
    const used = {};
    (team.summary?.chips_used||[]).forEach(({chip,gw}) => {
      const k = chip.toLowerCase();
      if (!used[k]) used[k] = [];
      used[k].push(gw);
    });

    const cells = TYPES.flatMap(k => {
      const m = chipMeta(k);
      const gwList = used[k] || [];
      return [0,1].map(slot => {
        const gw = gwList[slot];
        if (!gw) return `<td><span class="chip-val unused">—</span></td>`;
        const g = gwEntry(team, gw);
        const pts = g?.pts ?? '?';
        const isBest = bests[k]?.teamId===team.id && bests[k]?.gw===gw;
        return `<td><span class="chip-val ${m.cls}">${pts}${isBest?' &#x1F451;':''}</span><span class="chip-gw">GW${gw}</span></td>`;
      });
    });

    return `<tr>
      <td class="team-col"><span class="dot" style="background:${team.color}"></span>${esc(team.manager)}</td>
      ${cells.join('')}
    </tr>`;
  }).join('');
}

function renderPenalties() {
  const sorted = [...TEAMS].sort((a,b)=>(a.summary?.total_points??99999)-(b.summary?.total_points??99999));

  document.getElementById('penalty-body').innerHTML = sorted.map(team => {
    const cvc  = team.summary?.total_cvc_pens ?? 0;
    const inac = team.summary?.total_inactive_pens ?? 0;
    const xfer = team.gws.reduce((s,g)=>s+(g.xfer_cost_pens||0), 0);
    const total = cvc + inac + xfer;
    const hi = total > 0 ? 'hi-hi' : '';
    return `<tr>
      <td class="team-col"><span class="dot" style="background:${team.color}"></span>${esc(team.manager)}</td>
      <td class="${cvc>0?'hi-hi':''}">${cvc}</td>
      <td class="${inac>0?'hi-hi':''}">${inac}</td>
      <td>${xfer}</td>
      <td class="${hi}">${total}</td>
    </tr>`;
  }).join('');
}

function renderTopScores() {
  const allGWScores = [];
  TEAMS.forEach(team => {
    team.gws.forEach(g => {
      if (g.pts != null) allGWScores.push({team, gw:g.gw, pts:g.pts});
    });
  });

  const best  = [...allGWScores].sort((a,b)=>a.pts-b.pts).slice(0,3);
  const worst = [...allGWScores].sort((a,b)=>b.pts-a.pts).slice(0,3);

  const posClass = i => i===0?'p1':i===1?'p2':'p3';
  const html = (items, cls) => items.map((it,i)=>`
    <div class="top-score-item">
      <div class="ts-pos ${posClass(i)}">${i+1}</div>
      <span class="dot" style="background:${it.team.color}"></span>
      <div class="ts-info">
        <div class="ts-team">${esc(it.team.team_name)}</div>
        <div class="ts-meta">${esc(it.team.manager)} &middot; GW${it.gw}</div>
      </div>
      <div class="ts-pts ${cls}">${it.pts}</div>
    </div>`).join('');

  document.getElementById('best-scores').innerHTML  = html(best,  'best');
  document.getElementById('worst-scores').innerHTML = html(worst, 'worst');
}

// ─── util ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── go ───────────────────────────────────────────────────────────────────────
init();
