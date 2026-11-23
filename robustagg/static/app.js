'use strict';

/* -------------------------------------------------------------------------
   state + tiny helpers
   ------------------------------------------------------------------------- */

const state = {
  datasetId: null,
  analysis: null,
  panel: null,
  alpha: 0.10,
  cap: 0.10,
  estimator: 'trimmed',
  metric: null,
  selectedUnit: null,
  traceTargets: null,
  simulation: null,
  busy: 0,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === 'string' || typeof child === 'number'
      ? document.createTextNode(String(child)) : child);
  }
  return node;
};

const fmt = (v, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v) || !Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  const d = abs >= 1000 ? 0 : abs >= 100 ? 1 : digits;
  return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
};
const pct = (v, d = 1) => (v === null || v === undefined || !Number.isFinite(v)) ? '—' : `${(v * 100).toFixed(d)}%`;
const signed = (v, d = 2) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v >= 0 ? '+' : '−'}${fmt(Math.abs(v), d)}`;
};

function toast(msg, ms = 3200) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove('show'), ms);
}

function errorBox(msg) {
  const box = $('#app-error');
  if (!msg) { box.hidden = true; box.textContent = ''; return; }
  box.hidden = false;
  box.textContent = msg;
}

async function api(path, { method = 'GET', body = null, quiet = false } = {}) {
  state.busy += 1;
  document.getElementById('main').classList.add('loading');
  try {
    const res = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({ error: '响应不是 JSON' }));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    if (!quiet) errorBox(null);
    return data;
  } catch (err) {
    // Validation errors belong next to the form that caused them; the generic
    // banner would just repeat the dialog's own message.
    if (!quiet) errorBox(err.message || String(err));
    throw err;
  } finally {
    state.busy -= 1;
    if (state.busy === 0) document.getElementById('main').classList.remove('loading');
  }
}

/* -------------------------------------------------------------------------
   tooltip
   ------------------------------------------------------------------------- */

const tooltip = $('#tooltip');
function showTip(ev, title, rows, note) {
  tooltip.textContent = '';
  tooltip.appendChild(el('div', { class: 'tt-title', text: title }));
  for (const [k, v] of rows) {
    tooltip.appendChild(el('div', { class: 'tt-row' }, [el('span', { text: k }), el('strong', { text: v })]));
  }
  if (note) tooltip.appendChild(el('div', { class: 'tt-note', text: note }));
  tooltip.classList.add('show');
  positionTip(ev);
}
function positionTip(ev) {
  const pad = 14;
  const rect = tooltip.getBoundingClientRect();
  let x = (ev.clientX ?? 0) + pad;
  let y = (ev.clientY ?? 0) + pad;
  if (x + rect.width > window.innerWidth - 8) x = window.innerWidth - rect.width - 8;
  if (y + rect.height > window.innerHeight - 8) y = (ev.clientY ?? 0) - rect.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { tooltip.classList.remove('show'); }

/* -------------------------------------------------------------------------
   SVG helpers
   ------------------------------------------------------------------------- */

const NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    node.setAttribute(k, v);
  }
  return node;
}
function svgText(x, y, text, cls, extra = {}) {
  const t = svgEl('text', { x, y, class: cls, ...extra });
  t.textContent = text;
  return t;
}
function niceTicks(min, max, count = 5) {
  if (!(Number.isFinite(min) && Number.isFinite(max))) return [0, 1];
  if (min === max) return [min];
  const span = max - min;
  const raw = span / Math.max(1, count - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 1e-9; v += step) ticks.push(Math.round(v / step) * step);
  if (ticks.length < 2) ticks.push(min, max);
  return ticks;
}
function makeSvg(width, height) {
  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' });
  return svg;
}
function roundedTopRect(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w / 2, Math.abs(h)));
  if (h <= 0) return `M${x},${y}h${w}`;
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}
function roundedRightRect(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, h / 2, Math.abs(w)));
  if (w <= 0) return `M${x},${y}v${h}`;
  return `M${x},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h - rr}Q${x + w},${y + h} ${x + w - rr},${y + h}H${x}Z`;
}

/* dark-mode aware colors for SVG (CSS vars do not apply inside JS-created attrs
   for fills we compute, so read computed style once per render) */
function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* texture pattern (the accessibility / capped encoding) */
function ensurePatterns(svg) {
  const defs = svgEl('defs');
  for (const [id, color] of [['hatch1', token('--s1')], ['hatch2', token('--s2')], ['hatch3', token('--s3')]]) {
    const p = svgEl('pattern', { id, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
    p.appendChild(svgEl('rect', { width: 6, height: 6, fill: color, opacity: 0.18 }));
    p.appendChild(svgEl('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: color, 'stroke-width': 2 }));
    defs.appendChild(p);
  }
  svg.appendChild(defs);
  return svg;
}

/* -------------------------------------------------------------------------
   charts
   ------------------------------------------------------------------------- */

const EST_ORDER = ['naive_uncapped', 'naive', 'trimmed', 'huber', 'median'];
// Four validated categorical hues; the fifth is a neutral, not a recycled hue
// (a repeated colour would make two estimators look identical).
const EST_COLOR = { naive_uncapped: '--s4', naive: '--s2', trimmed: '--s1', huber: '--s3', median: '--ink-2' };
const EST_LABELS = {
  naive_uncapped: '直接按上报量加权', naive: '封顶加权', trimmed: '截尾汇总',
  huber: 'Huber M 估计', median: '加权中位数',
};

function renderEstimateChart(a) {
  const host = $('#est-chart');
  host.textContent = '';
  const est = a.estimates;
  const chosen = a.chosen;
  const vals = EST_ORDER.map((k) => ({ key: k, v: est[k].center, label: EST_LABELS[k] }));
  const finite = vals.filter((x) => Number.isFinite(x.v));
  if (!finite.length) { host.appendChild(el('p', { class: 'empty', text: '没有可比较的估计量。' })); return; }
  const width = 560, rowH = 38, padTop = 44, padBottom = 34, padLeft = 12, padRight = 24;
  const labelW = 96;
  const plotX0 = padLeft + labelW;
  const plotW = width - padRight - plotX0;
  const height = padTop + rowH * vals.length + padBottom;
  const svg = makeSvg(width, height);
  const valsAll = finite.map((x) => x.v);
  const truth = a.truth;
  const bounds = a.bound && a.bound.interval ? a.bound.interval : null;
  let lo = Math.min(...valsAll), hi = Math.max(...valsAll);
  if (truth !== null && truth !== undefined) { lo = Math.min(lo, truth); hi = Math.max(hi, truth); }
  if (bounds) { lo = Math.min(lo, bounds[0]); hi = Math.max(hi, bounds[1]); }
  const span = hi - lo || Math.abs(hi) || 1;
  lo -= span * 0.10; hi += span * 0.10;
  // ONE scale for the band, every mark and the truth line.  A second scale
  // for the marks (as an earlier version had) is the classic dual-axis error.
  const x = (v) => plotX0 + ((v - lo) / (hi - lo)) * plotW;

  // grid + x axis, restricted to the plot area
  const ticks = niceTicks(lo, hi, 5);
  for (const t of ticks) {
    svg.appendChild(svgEl('line', { x1: x(t), y1: padTop - 6, x2: x(t), y2: height - padBottom + 2, class: 'grid-line' }));
    svg.appendChild(svgText(x(t), height - padBottom + 17, fmt(t, 1), 'axis-text', { 'text-anchor': 'middle' }));
  }
  if (bounds) {
    const bx0 = Math.min(x(bounds[0]), x(bounds[1]));
    const bw = Math.max(1, Math.abs(x(bounds[1]) - x(bounds[0])));
    svg.appendChild(svgEl('rect', {
      x: bx0, y: padTop - 6, width: bw, height: rowH * vals.length + 2,
      fill: token('--wash'), stroke: token('--s1'), 'stroke-opacity': 0.30, rx: 4,
    }));
    svg.appendChild(svgText(Math.min(bx0 + 5, width - padRight - 4), 15,
      `误差范围 ±${fmt(a.bound.offset, 1)}（${boundStatusText(a.bound.status)}）`, 'label-text'));
  }

  vals.forEach((row, i) => {
    const cy = padTop + i * rowH + rowH / 2;
    const isChosen = row.key === chosen;
    const color = token(EST_COLOR[row.key]);
    svg.appendChild(svgText(padLeft, cy + 4, EST_LABELS[row.key], 'label-text', {
      'dominant-baseline': 'middle',
    }));
    if (Number.isFinite(row.v)) {
      const cx = x(row.v);
      const barW = isChosen ? 16 : 10;
      svg.appendChild(svgEl('rect', {
        x: cx - barW / 2, y: cy - (isChosen ? 9 : 5),
        width: barW, height: isChosen ? 18 : 10,
        rx: 4, fill: color, opacity: isChosen ? 1 : 0.55,
      }));
      if (isChosen) {
        svg.appendChild(svgEl('rect', {
          x: cx - barW / 2 - 2, y: cy - 11, width: barW + 4, height: 22,
          rx: 6, fill: 'none', stroke: color, 'stroke-opacity': 0.35,
        }));
      }
      const above = cx > width - padRight - 46;
      svg.appendChild(svgText(
        above ? cx - 12 : cx + 12, cy + 4,
        fmt(row.v, 2), isChosen ? 'value-text' : 'label-text',
        { 'text-anchor': above ? 'end' : 'start', 'dominant-baseline': 'middle' },
      ));
    }
    const hit = svgEl('rect', {
      x: 0, y: cy - rowH / 2, width, height: rowH, fill: 'transparent', class: 'hit', tabindex: 0,
      role: 'button', 'aria-label': `${EST_LABELS[row.key]} ${fmt(row.v, 2)}`,
    });
    const on = (ev) => showTip(ev, EST_LABELS[row.key], [
      ['中心估计', fmt(row.v, 2)],
      ['与真值差', a.truth === null ? '—' : fmt(Math.abs(row.v - a.truth), 2)],
    ], isChosen ? '当前选用' : '');
    hit.addEventListener('pointermove', on);
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('focus', on);
    hit.addEventListener('blur', hideTip);
    svg.appendChild(hit);
  });

  if (truth !== null && truth !== undefined) {
    const tx = x(truth);
    svg.appendChild(svgEl('line', {
      x1: tx, y1: padTop - 6, x2: tx, y2: height - padBottom + 2,
      stroke: token('--ink'), 'stroke-width': 2,
    }));
    // The truth label sits on its own line above the plot, clamped inside the
    // frame so it cannot collide with the axis ticks.
    const anchor = tx > width - padRight - 70 ? 'end' : tx < plotX0 + 70 ? 'start' : 'middle';
    const lx = anchor === 'end' ? Math.min(tx + 4, width - padRight) : anchor === 'start' ? Math.max(tx - 4, plotX0) : tx;
    svg.appendChild(svgText(lx, 32, `真实值 ${fmt(truth, 2)}`, 'label-text', {
      'text-anchor': anchor, 'font-weight': 600,
    }));
  }
  host.appendChild(svg);

  const legend = $('#est-legend');
  legend.textContent = '';
  legend.appendChild(el('span', { class: 'item' }, [
    el('span', { class: 'swatch', style: `background:${token('--s1')}` }),
    document.createTextNode('当前选用'),
  ]));
  // Only key a mark that is actually drawn: a void bound has no band, and a
  // legend entry for an absent band invites reading one in.
  if (bounds) {
    legend.appendChild(el('span', { class: 'item' }, [
      el('span', { class: 'bound-band-key' }), document.createTextNode('误差范围（前提成立时）'),
    ]));
  }
  if (truth !== null && truth !== undefined) {
    legend.appendChild(el('span', { class: 'item' }, [
      el('span', { class: 'line-key', style: `background:${token('--ink')}` }), document.createTextNode('已知真值'),
    ]));
  }

  const table = $('#est-table');
  table.textContent = '';
  const t = el('table');
  t.appendChild(el('thead', {}, el('tr', {}, [
    el('th', { text: '估计量' }), el('th', { class: 'num', text: '中心' }),
    el('th', { class: 'num', text: '与真值差' }), el('th', { text: '使用权重' }),
  ])));
  const tb = el('tbody');
  for (const row of vals) {
    tb.appendChild(el('tr', {}, [
      el('td', { text: EST_LABELS[row.key] + (row.key === chosen ? ' · 当前' : '') }),
      el('td', { class: 'num', text: fmt(row.v, 2) }),
      el('td', { class: 'num', text: a.truth === null || !Number.isFinite(row.v) ? '—' : fmt(Math.abs(row.v - a.truth), 2) }),
      el('td', { text: est[row.key].partial_trim ? '部分单位被削权' : '全部单位' }),
    ]));
  }
  t.appendChild(tb);
  table.appendChild(t);
}

function boundStatusText(status) {
  return status === 'guaranteed' ? '已验证前提' : status === 'conditional' ? '条件性' : '未成立';
}

function renderWeightChart(a) {
  const host = $('#weight-chart');
  host.textContent = '';
  const rows = a.contributions.slice().sort((p, q) => p.weight === q.weight ? p.unit_id.localeCompare(q.unit_id) : q.weight - p.weight);
  if (!rows.length) { host.appendChild(el('p', { class: 'empty', text: '没有单位。' })); return; }
  const maxW = Math.max(...rows.map((r) => r.weight)) || 1;
  // padRight reserves room for the "weight · share" label that starts 6px past
  // the longest bar end; without it the label ran to the viewBox edge and the
  // last glyph clipped at narrow widths.
  const width = 560, rowH = 19, padTop = 14, padBottom = 22, padLeft = 8, padRight = 92;
  const height = padTop + rows.length * rowH + padBottom;
  const svg = makeSvg(width, height);
  ensurePatterns(svg);
  const barMax = width - padLeft - padRight;
  const capX = padLeft + (a.weighting.cap / maxW) * barMax;
  if (capX > padLeft + 4 && capX < width - padRight) {
    svg.appendChild(svgEl('line', {
      x1: capX, y1: padTop - 4, x2: capX, y2: height - padBottom + 2,
      stroke: token('--s2'), 'stroke-width': 1.5, 'stroke-dasharray': '4 3',
    }));
    svg.appendChild(svgText(capX + 4, padTop - 5, '单单位上限', 'axis-text'));
  }
  rows.forEach((r, i) => {
    const y = padTop + i * rowH;
    const w = Math.max(1, (r.weight / maxW) * barMax);
    const isSel = state.selectedUnit === r.unit_id;
    const fill = r.isolated ? token('--muted') : token('--s1');
    svg.appendChild(svgEl('rect', {
      x: padLeft, y: y + 4, width: w, height: Math.max(8, rowH - 8), rx: 3,
      fill, opacity: r.isolated ? 0.35 : 1,
      stroke: isSel ? token('--ink') : null, 'stroke-width': isSel ? 2 : null,
    }));
    if (r.capped && !r.isolated) {
      // Hatch marks capped weight; it changes texture, not length, so the bar
      // still reads as the capped value it actually contributes.
      svg.appendChild(svgEl('rect', {
        x: padLeft, y: y + 4, width: w, height: Math.max(8, rowH - 8), rx: 3, fill: 'url(#hatch2)',
      }));
    }
    // Labels start just past the bar end; the right margin is wide enough
    // (padRight) for the longest one to stay inside the viewBox at any width.
    svg.appendChild(svgText(padLeft + w + 6, y + rowH / 2 + 4,
      `${fmt(r.weight, 0)} · ${pct(r.weight / a.weighting.total_weight, 1)}`, 'axis-text'));
    const hit = svgEl('rect', {
      x: 0, y, width, height: rowH, fill: 'transparent', class: 'hit', tabindex: 0, role: 'button',
      'aria-label': `${r.unit_id} 权重 ${fmt(r.weight, 1)}`,
    });
    const on = (ev) => showTip(ev, r.unit_id, [
      ['封顶权重', fmt(r.weight, 1)],
      ['上报权重', fmt(r.claimed_weight, 1)],
      ['占有效权重', pct(r.weight / a.weighting.total_weight, 1)],
      ['均值', fmt(r.mean, 1)],
      ['所在批次', r.batch_id],
    ], r.capped ? '权重被上限截断（斜纹）' : r.isolated ? '已隔离' : '');
    hit.addEventListener('pointermove', on);
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('focus', on);
    hit.addEventListener('blur', hideTip);
    hit.addEventListener('click', () => selectUnit(r.unit_id));
    hit.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') selectUnit(r.unit_id); });
    svg.appendChild(hit);
  });
  // The list is long by design; cap the visible height and keep every unit
  // reachable by scrolling (the table view below lists all of them too).
  const scroller = el('div', { class: 'chart-scroll', tabindex: 0, 'aria-label': '单位权重列表，可滚动' });
  scroller.appendChild(svg);
  host.appendChild(scroller);
  const more = $('#weight-more');
  if (more) {
    more.textContent = rows.length > 14
      ? `共 ${rows.length} 个单位，列表可滚动；点“表格”可一次看全。`
      : '';
  }

  const legend = $('#weight-legend');
  legend.textContent = '';
  legend.appendChild(el('span', { class: 'item' }, [
    el('span', { class: 'swatch', style: `background:${token('--s1')}` }), document.createTextNode('未封顶'),
  ]));
  legend.appendChild(el('span', { class: 'item' }, [
    el('span', { class: 'swatch', style: `background:repeating-linear-gradient(45deg, ${token('--s2')} 0 2px, transparent 2px 5px)` }),
    document.createTextNode('封顶'),
  ]));
  legend.appendChild(el('span', { class: 'item' }, [
    el('span', { class: 'swatch', style: `background:${token('--muted')}; opacity:.4` }), document.createTextNode('已隔离'),
  ]));
  legend.appendChild(el('span', { class: 'item' }, [
    el('span', { class: 'line-key', style: `background:${token('--s2')}` }), document.createTextNode('单单位上限'),
  ]));

  const table = $('#weight-table');
  table.textContent = '';
  const t = el('table');
  t.appendChild(el('thead', {}, el('tr', {}, [
    el('th', { text: '单位' }), el('th', { class: 'num', text: '均值' }), el('th', { class: 'num', text: '上报权重' }),
    el('th', { class: 'num', text: '封顶权重' }), el('th', { class: 'num', text: '占有效权重' }),
    el('th', { text: '批次' }), el('th', { text: '状态' }),
  ])));
  const tb = el('tbody');
  for (const r of rows) {
    const tr = el('tr', { class: `clickable${state.selectedUnit === r.unit_id ? ' selected' : ''}${r.isolated ? ' isolated' : ''}` }, [
      el('td', { text: r.unit_id }),
      el('td', { class: 'num', text: fmt(r.mean, 1) }),
      el('td', { class: 'num', text: fmt(r.claimed_weight, 0) }),
      el('td', { class: 'num', text: fmt(r.weight, 0) }),
      el('td', { class: 'num', text: pct(r.weight / a.weighting.total_weight, 1) }),
      el('td', { text: r.batch_id }),
      el('td', { text: r.isolated ? '已隔离' : r.capped ? '封顶' : r.split ? '拆报' : '正常' }),
    ]);
    tr.addEventListener('click', () => selectUnit(r.unit_id));
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  table.appendChild(t);
}

function renderCurve(a) {
  const host = $('#curve-chart');
  host.textContent = '';
  const curve = a.curve.curve;
  if (!curve.length) { host.appendChild(el('p', { class: 'empty', text: '没有单位。' })); return; }
  const width = 760, height = 250, padLeft = 46, padRight = 18, padTop = 28, padBottom = 34;
  const svg = makeSvg(width, height);
  ensurePatterns(svg);
  const xs = curve.map((c) => c.value);
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const x = (v) => padLeft + ((v - lo) / (hi - lo || 1)) * (width - padLeft - padRight);
  const y = (s) => padTop + (1 - s) * (height - padTop - padBottom);
  const plot = { x0: padLeft, x1: width - padRight, y0: padTop, y1: height - padBottom };
  const clip = (v, a, b) => Math.max(a, Math.min(b, v));
  // grid
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    svg.appendChild(svgEl('line', { x1: padLeft, y1: y(t), x2: width - padRight, y2: y(t), class: 'grid-line' }));
    svg.appendChild(svgText(padLeft - 6, y(t) + 4, `${Math.round(t * 100)}%`, 'axis-text', { 'text-anchor': 'end' }));
  }
  for (const t of niceTicks(lo, hi, 6)) {
    svg.appendChild(svgText(x(t), height - 12, fmt(t, 1), 'axis-text', { 'text-anchor': 'middle' }));
  }
  // error band, clipped to the plot area so it can never bleed past the axes
  const alpha = a.curve.alpha;
  const aLow = a.bound && a.bound.interval ? a.bound.interval[0] : null;
  const aHigh = a.bound && a.bound.interval ? a.bound.interval[1] : null;
  if (aLow !== null) {
    const bx0 = clip(x(aLow), plot.x0, plot.x1);
    const bx1 = clip(x(aHigh), plot.x0, plot.x1);
    svg.appendChild(svgEl('rect', {
      x: bx0, y: plot.y0, width: Math.max(1, bx1 - bx0), height: plot.y1 - plot.y0,
      fill: token('--wash'),
    }));
    // The band is identified by the legend's swatch; an in-plot caption here
    // would sit on the 100% gridline and add nothing.
  }
  // step curve
  let d = `M${x(curve[0].value)},${y(0)}`;
  let prev = 0;
  for (const c of curve) {
    d += `L${x(c.value)},${y(prev)}L${x(c.value)},${y(c.cum_share)}`;
    prev = c.cum_share;
  }
  d += `L${x(curve[curve.length - 1].value)},${y(1)}`;
  svg.appendChild(svgEl('path', { d, fill: 'none', stroke: token('--s1'), 'stroke-width': 2, 'stroke-linejoin': 'round' }));
  // trim lines
  for (const m of a.curve.marks) {
    if (m.q === 0.5) continue;
    svg.appendChild(svgEl('line', {
      x1: x(m.value), y1: padTop, x2: x(m.value), y2: height - padBottom,
      stroke: token('--s2'), 'stroke-width': 1.5,
    }));
    svg.appendChild(svgText(x(m.value), padTop - 2, m.label, 'label-text', { 'text-anchor': 'middle' }));
  }
  const med = a.curve.marks.find((m) => m.q === 0.5);
  if (med) {
    svg.appendChild(svgEl('line', {
      x1: x(med.value), y1: padTop, x2: x(med.value), y2: height - padBottom,
      stroke: token('--ink'), 'stroke-width': 1, 'stroke-opacity': 0.5,
    }));
  }
  // dots, capped ones hatched, isolated excluded already
  for (const c of curve) {
    const cx = x(c.value), cy = y(c.cum_share);
    const isSel = state.selectedUnit === c.unit_id;
    svg.appendChild(svgEl('circle', {
      cx, cy, r: isSel ? 6 : 4,
      fill: c.capped ? 'url(#hatch2)' : token('--s1'),
      stroke: token('--surface'), 'stroke-width': 2,
      style: 'cursor:pointer',
    }));
    const hit = svgEl('circle', { cx, cy, r: 13, fill: 'transparent', class: 'hit', tabindex: 0, role: 'button',
      'aria-label': `${c.unit_id} 取值 ${fmt(c.value, 1)}` });
    const on = (ev) => showTip(ev, c.unit_id, [
      ['取值', fmt(c.value, 1)],
      ['封顶权重', fmt(c.weight, 1)],
      ['累计份额', pct(c.cum_share, 1)],
      ['批次', c.batch_id || '—'],
    ], c.capped ? '权重已封顶' : '');
    hit.addEventListener('pointermove', on);
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('focus', on);
    hit.addEventListener('blur', hideTip);
    hit.addEventListener('click', () => selectUnit(c.unit_id));
    svg.appendChild(hit);
  }
  svg.appendChild(svgEl('line', { x1: padLeft, y1: height - padBottom, x2: width - padRight, y2: height - padBottom, class: 'axis-line' }));
  host.appendChild(svg);

  const legend = $('#curve-legend');
  legend.textContent = '';
  legend.appendChild(el('span', { class: 'item' }, [el('span', { class: 'line-key', style: `background:${token('--s1')}` }), document.createTextNode('加权经验分布')]));
  legend.appendChild(el('span', { class: 'item' }, [el('span', { class: 'line-key', style: `background:${token('--s2')}` }), document.createTextNode(`截尾线（各 ${pct(alpha, 0)}）`)]));
  legend.appendChild(el('span', { class: 'item' }, [el('span', { class: 'swatch', style: `background:repeating-linear-gradient(45deg, ${token('--s2')} 0 2px, transparent 2px 5px)` }), document.createTextNode('权重已封顶')]));
  if (aLow !== null) {
    // Key only the band that is actually drawn: a void bound draws none, and a
    // legend swatch for an absent band invites reading one in.
    legend.appendChild(el('span', { class: 'item' }, [el('span', { class: 'bound-band-key' }), document.createTextNode('误差范围')]));
  }

  const table = $('#curve-table');
  table.textContent = '';
  const t = el('table');
  t.appendChild(el('thead', {}, el('tr', {}, [
    el('th', { text: '单位' }), el('th', { class: 'num', text: '取值' }), el('th', { class: 'num', text: '封顶权重' }),
    el('th', { class: 'num', text: '累计份额' }), el('th', { text: '批次' }),
  ])));
  const tb = el('tbody');
  for (const c of curve) {
    const tr = el('tr', { class: `clickable${state.selectedUnit === c.unit_id ? ' selected' : ''}` }, [
      el('td', { text: c.unit_id }),
      el('td', { class: 'num', text: fmt(c.value, 1) }),
      el('td', { class: 'num', text: fmt(c.weight, 0) }),
      el('td', { class: 'num', text: pct(c.cum_share, 1) }),
      el('td', { text: c.batch_id || '—' }),
    ]);
    tr.addEventListener('click', () => selectUnit(c.unit_id));
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  table.appendChild(t);
}

/* -------------------------------------------------------------------------
   panels
   ------------------------------------------------------------------------- */

function renderHero(a) {
  const est = a.estimates[a.chosen];
  const c = est.center;
  const host = $('#hero-center');
  host.textContent = fmt(c, 2);
  const small = el('small', { text: `  ${a.config.metric}` });
  host.appendChild(small);
  $('#chosen-label').textContent = `估计量：${est.label}`;
  const b = a.bound;
  const chip = $('#bound-chip');
  chip.className = `chip ${b.status === 'guaranteed' ? 'good' : b.status === 'conditional' ? 'warn' : 'bad'}`;
  chip.textContent = b.status === 'guaranteed' ? '保证成立' : b.status === 'conditional' ? '条件性保证' : '保证已撤销';
  $('#hero-sub').textContent = b.interval
    ? `误差范围 ${fmt(b.interval[0], 1)} – ${fmt(b.interval[1], 1)}（±${fmt(b.offset, 2)}）`
    : '没有可用的误差范围';
  $('#hero-truth').textContent = a.truth !== null && a.truth !== undefined
    ? `已知真值 ${fmt(a.truth, 2)} · 本估计偏差 ${signed(c - a.truth, 2)}`
    : '本数据集无真值（接收数据）';
  $('#hero-truth').className = a.truth !== null && a.truth !== undefined ? 'line' : 'line muted';

  const facts = $('#hero-facts');
  facts.textContent = '';
  const w = a.weighting;
  const items = [
    ['参与单位', `${w.units_reporting}`, `共 ${w.fragments} 条有效报文`],
    ['单单位上限', pct(w.cap_share_actual, 1), `名义 ${pct(w.cap_share, 0)}`],
    ['被截断权重', fmt(w.removed_by_cap, 0), w.cap_binding ? '有单位触顶' : '无单位触顶'],
    ['隔离单位', `${w.isolated_units.length}`, w.isolated_units.length ? '已从估计中移除' : '无'],
    ['拆报单位', `${(w.split_units || []).length}`, (w.split_units || []).length ? '按单位汇总后封顶' : '无'],
  ];
  for (const [k, v, sub] of items) {
    facts.appendChild(el('div', { class: 'fact' }, [
      el('div', { class: 'k', text: k }),
      el('div', { class: 'v' }, [document.createTextNode(v), el('small', { text: '  ' + sub })]),
    ]));
  }
  // One-line consequence only; the full derivation lives in the assumptions
  // card directly beside this one.
  const foot = $('#hero-note');
  foot.textContent = '';
  if (b.status === 'void') {
    foot.textContent = '前提未满足：该区间已撤销，下面的中心估计不再带有误差保证。';
  } else if (b.status === 'conditional') {
    foot.textContent = `误差范围按“实际污染 ≤ ${pct(a.config.contamination_rate, 0)}”成立；该前提由报送方声明，服务端无法核验。`;
  } else {
    foot.textContent = '全部前提已验证，误差范围按最坏放置成立。';
  }
}

function renderAssumptions(a) {
  const ul = $('#assumptions');
  ul.textContent = '';
  const b = a.bound;
  $('#bound-status-hint').textContent = b.status === 'guaranteed'
    ? '全部前提已验证' : b.status === 'conditional' ? '有一个前提无法核验' : '前提已失效';
  for (const item of b.assumptions) {
    const mark = item.status === 'met' ? '✓' : item.status === 'unmet' ? '✕' : '?';
    const statusText = item.status === 'met' ? '成立' : item.status === 'unmet' ? '不成立' : '无法核验';
    ul.appendChild(el('li', { class: item.status }, [
      el('span', { class: 'mark', text: mark, 'aria-hidden': 'true' }),
      el('span', {}, [document.createTextNode(item.text), el('span', { class: 'status', text: ` · ${statusText}` })]),
    ]));
  }
  $('#rationale').textContent = b.rationale || '—';
}

function renderCompare(a) {
  const body = $('#compare-body');
  body.textContent = '';
  const cmp = a.compare;
  const note = el('div', {
    class: `notice ${cmp.explained_by_assumption === false ? 'bad' : ''}`,
  });
  const which = cmp.gap === null ? '—' : cmp.gap >= 0 ? '加权平均更高' : '截尾汇总更高';
  note.appendChild(el('strong', { text: `${which}，差 ${fmt(Math.abs(cmp.gap ?? NaN), 2)}` }));
  note.appendChild(el('div', {
    text: cmp.explained_by_assumption === null
      ? (cmp.bound_status === 'void'
        ? '误差范围的保证已撤销，不再判断差异是否可由污染解释。'
        : '没有可用的误差范围，无法判断差异是否可由污染解释。')
      : cmp.explained_by_assumption
        ? `差异在 ±${fmt(cmp.offset, 2)} 的误差范围内，与“污染不超过 α”的假设相容。`
        : `差异超过 ±${fmt(cmp.offset, 2)}，超出假设能解释的范围——应按失去保证处理。`,
  }));
  body.appendChild(note);

  const kv = el('dl', { class: 'kv' });
  const rows = [
    ['直接按上报量加权', fmt(cmp.naive_uncapped, 2)],
    ['封顶加权', fmt(cmp.naive, 2)],
    ['截尾汇总', fmt(cmp.trimmed, 2)],
    ['Huber M 估计', fmt(cmp.huber, 2)],
    ['加权中位数', fmt(cmp.median, 2)],
  ];
  if (a.truth !== null && a.truth !== undefined) rows.push(['已知真值', fmt(a.truth, 2)]);
  for (const [k, v] of rows) {
    kv.appendChild(el('dt', { text: k }));
    kv.appendChild(el('dd', { text: v }));
  }
  body.appendChild(kv);

  const btns = el('div', { class: 'row' });
  for (const key of ['naive', 'trimmed']) {
    if (a.estimates[key]) {
      btns.appendChild(el('button', {
        class: a.chosen === key ? 'primary' : '',
        text: `以${a.estimates[key].label}为中心`,
        onclick: () => { state.estimator = key; refresh(); },
      }));
    }
  }
  btns.appendChild(el('button', {
    text: '用截尾结果 → 追溯参与批次',
    onclick: () => openTrace(a.estimates.trimmed.support.map((s) => s.summary_id)),
  }));
  body.appendChild(btns);
}

async function selectUnit(unitId) {
  state.selectedUnit = unitId;
  if (state.analysis) {
    renderWeightChart(state.analysis);
    renderCurve(state.analysis);
  }
  $('#influence-hint').textContent = `单位 ${unitId}`;
  const host = $('#influence-body');
  host.textContent = '';
  host.appendChild(el('p', { class: 'empty', text: '计算中…' }));
  try {
    const data = await api(`/api/dataset/${state.datasetId}/unit/${encodeURIComponent(unitId)}?${params()}`);
    renderInfluence(data);
  } catch (err) {
    host.textContent = '';
    host.appendChild(el('p', { class: 'empty', text: `无法读取单位影响：${err.message}` }));
  }
}

function renderInfluence(data) {
  const host = $('#influence-body');
  host.textContent = '';
  const inf = data.influence;
  const head = el('div', { class: 'unit-head' }, [
    el('span', { class: 'big', text: data.unit_id }),
    data.truth && data.truth.is_bad
      ? el('span', { class: 'chip bad', text: '样例中的损坏单位' })
      : data.truth ? el('span', { class: 'chip good', text: '样例中的合法单位' }) : null,
    data.isolation ? el('span', { class: 'chip warn', text: '已隔离' }) : null,
  ]);
  host.appendChild(head);

  const truth = data.truth;
  if (truth) {
    const kv = el('dl', { class: 'kv' });
    kv.appendChild(el('dt', { text: '真实均值' }));
    kv.appendChild(el('dd', { text: fmt(truth.true_mean, 2) }));
    kv.appendChild(el('dt', { text: '真实数量' }));
    kv.appendChild(el('dd', { text: fmt(truth.true_n, 0) }));
    host.appendChild(kv);
  }

  const summary = data.summary;
  if (summary) {
    const list = el('ul', { class: 'fragment-list' });
    for (const f of summary.fragments) {
      const tr = el('li', { class: f.isolated ? 'isolated' : '' }, [
        el('div', {}, [
          el('div', { text: `${f.summary_id}` }),
          el('div', { class: 'meta', text: `${f.batch_id} · 均值 ${fmt(f.mean, 1)} · 上报权重 ${fmt(f.claimed_weight, 0)} → 封顶 ${fmt(f.weight, 0)}${f.capped ? '（触顶）' : ''}` }),
        ]),
        el('button', { text: '追溯', onclick: () => openTrace([f.summary_id]) }),
      ]);
      list.appendChild(tr);
    }
    host.appendChild(list);
  }

  const deltaTxt = inf.delta === null ? '—' : signed(inf.delta, 2);
  const kv = el('dl', { class: 'kv' });
  kv.appendChild(el('dt', { text: '若隔离本单位，中心变化' }));
  kv.appendChild(el('dd', { text: `${deltaTxt}（${fmt(inf.center_with, 1)} → ${fmt(inf.center_without, 1)}）` }));
  kv.appendChild(el('dt', { text: '权重份额' }));
  kv.appendChild(el('dd', { text: `${pct(inf.weight_share, 1)} ${inf.at_cap ? '· 已在上限' : ''}` }));
  kv.appendChild(el('dt', { text: '两个估计之差' }));
  kv.appendChild(el('dd', { text: `${signed(inf.gap_with, 2)} → ${signed(inf.gap_without, 2)}（变化 ${signed(inf.gap_delta, 2)}）` }));
  kv.appendChild(el('dt', { text: '是否在误差范围内' }));
  kv.appendChild(el('dd', {
    text: inf.within_bound === null
      ? (inf.bound_with && inf.bound_with.status === 'void' ? '—（保证已撤销，不做判断）' : '—')
      : inf.within_bound ? '是，在范围内变化' : '否，超出假设范围',
  }));
  host.appendChild(kv);

  const note = el('div', { class: `notice ${inf.within_bound === false ? 'warn' : ''}` });
  note.textContent = inf.is_isolated
    ? '该单位当前已被隔离；下面的记录保留原因与前后影响，可释放。'
    : '隔离只是记录决定，不删除报文；释放后权重与中心恢复原状。';
  host.appendChild(note);

  if (!inf.is_isolated) {
    const form = el('form', { class: 'iso-form' });
    const reason = el('textarea', { placeholder: '隔离原因（必填，会写入记录）', maxlength: 400 });
    const row = el('div', { class: 'row' }, [
      reason,
      el('button', { type: 'submit', class: 'primary', text: '隔离该单位' }),
    ]);
    form.appendChild(row);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (!reason.value.trim()) { toast('隔离必须填写原因'); return; }
      try {
        const res = await api(`/api/dataset/${state.datasetId}/isolate`, {
          method: 'POST',
          body: { unit_id: data.unit_id, reason: reason.value.trim(), metric: state.analysis.config.metric, period: state.analysis.config.period },
        });
        state.analysis = res.analysis;
        await refresh(false);
        toast('已记录隔离决定');
      } catch (err) { toast(err.message); }
    });
    host.appendChild(form);
  }
}

function renderIsolations(a) {
  const host = $('#isolation-list');
  host.textContent = '';
  const list = a.isolations || [];
  if (!list.length) {
    host.appendChild(el('p', { class: 'empty', text: '没有隔离记录。' }));
    return;
  }
  for (const iso of list) {
    const active = iso.released_at === null;
    const card = el('div', { class: `notice ${active ? 'warn' : ''}` }, [
      el('div', { class: 'row' }, [
        el('strong', { text: iso.unit_id }),
        el('span', { class: `chip ${active ? 'warn' : 'info'}`, text: active ? '隔离中' : '已释放' }),
        el('span', { class: 'muted small', text: `判定人 ${iso.decided_by}` }),
      ]),
      el('div', { class: 'small', text: `原因：${iso.reason}` }),
      el('div', { class: 'small muted', text: `前后影响：中心 ${fmt(iso.before_center, 2)} → ${fmt(iso.after_center, 2)}；截尾 ${fmt(iso.before_trimmed, 2)} → ${fmt(iso.after_trimmed, 2)}；权重 ${fmt(iso.before_weight, 0)} → ${fmt(iso.after_weight, 0)}` }),
    ]);
    if (!active) {
      card.appendChild(el('div', { class: 'small muted', text: `释放说明：${iso.release_note || '—'}` }));
    } else {
      const form = el('form', { class: 'row' });
      const note = el('input', { type: 'text', placeholder: '释放说明', style: 'flex:1' });
      form.appendChild(note);
      form.appendChild(el('button', { type: 'submit', text: '释放' }));
      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        if (!note.value.trim()) { toast('释放隔离需要说明'); return; }
        try {
          const res = await api(`/api/dataset/${state.datasetId}/release`, {
            method: 'POST',
            body: { isolation_id: iso.id, note: note.value.trim(), metric: state.analysis.config.metric, period: state.analysis.config.period },
          });
          state.analysis = res.analysis;
          await refresh(false);
          toast('已释放隔离');
        } catch (err) { toast(err.message); }
      });
      card.appendChild(form);
    }
    host.appendChild(card);
  }
}

function renderBatches(a, panel) {
  const host = $('#batch-list');
  host.textContent = '';
  const counts = new Map();
  for (const r of a.receipts) {
    counts.set(r.batch_id, (counts.get(r.batch_id) || 0) + 1);
  }
  for (const b of (panel?.batches || [])) {
    const li = el('li', {
      role: 'button', tabindex: 0,
      onclick: () => openBatch(b.batch_id),
    }, [
      el('div', {}, [
        el('div', { text: b.batch_id }),
        el('div', { class: 'meta', text: `${b.receipts} 条报文 · ${b.units} 个单位 · 来源 ${b.source === 'demo' ? '虚构样例' : b.source === 'intake' ? '录入' : '更正/注入'}` }),
      ]),
      el('span', { class: 'chip', text: `${counts.get(b.batch_id) || 0} 条参与` }),
    ]);
    li.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') openBatch(b.batch_id); });
    host.appendChild(li);
  }
}

/* -------------------------------------------------------------------------
   dialog: intake / trace / simulation
   ------------------------------------------------------------------------- */

function openDialog(id) { const d = $(id); if (typeof d.showModal === 'function') d.showModal(); }
function closeDialog(id) { const d = $(id); d.close(); }

async function openBatch(batchId) {
  try {
    const data = await api(`/api/dataset/${state.datasetId}/batch?batch_id=${encodeURIComponent(batchId)}`);
    const body = $('#trace-body');
    body.textContent = '';
    body.appendChild(el('h2', { text: batchId }));
    body.appendChild(receiptTable(data.receipts));
    openDialog('#trace-dialog');
  } catch (err) { toast(err.message); }
}

async function openTrace(summaryIds) {
  if (!summaryIds || !summaryIds.length) { toast('没有可追溯的报文'); return; }
  try {
    const ids = summaryIds.slice(0, 60).join(',');
    const data = await api(`/api/dataset/${state.datasetId}/trace?summary_ids=${encodeURIComponent(ids)}`);
    const body = $('#trace-body');
    body.textContent = '';
    body.appendChild(el('p', { class: 'small muted', text: `参与本次估计的 ${data.receipts.length} 条报文（按请求顺序），下方按批次列出全部报文。` }));
    body.appendChild(receiptTable(data.receipts));
    for (const b of data.batches) {
      body.appendChild(el('h2', { text: `批次 ${b.batch_id}` }));
      body.appendChild(receiptTable(b.receipts));
    }
    openDialog('#trace-dialog');
  } catch (err) { toast(err.message); }
}

function receiptTable(rows) {
  const wrap = el('div', { class: 'table-view', style: 'max-height:none' });
  const t = el('table');
  t.appendChild(el('thead', {}, el('tr', {}, [
    el('th', { text: '报文' }), el('th', { text: '单位' }), el('th', { class: 'num', text: '均值' }),
    el('th', { class: 'num', text: '数量' }), el('th', { class: 'num', text: '范围' }),
    el('th', { text: '来源' }), el('th', { text: '状态' }),
  ])));
  const tb = el('tbody');
  for (const r of rows) {
    tb.appendChild(el('tr', { class: r.superseded || r.active === false ? 'isolated' : '' }, [
      el('td', { class: 'mono', text: r.summary_id }),
      el('td', { text: r.unit_id }),
      el('td', { class: 'num', text: fmt(r.mean, 2) }),
      el('td', { class: 'num', text: fmt(r.n_effective, 0) }),
      el('td', { class: 'num', text: `${fmt(r.range_low, 1)}–${fmt(r.range_high, 1)}` }),
      el('td', { text: r.source === 'demo' ? '样例' : r.source === 'intake' ? '录入' : r.source === 'correction' ? '更正' : '注入' }),
      el('td', { text: r.superseded ? '已被替换' : r.active === false ? '未参与' : r.injected ? '演示注入' : '有效' }),
    ]));
  }
  t.appendChild(tb);
  wrap.appendChild(t);
  return wrap;
}

/* -------------------------------------------------------------------------
   simulation
   ------------------------------------------------------------------------- */

function renderSimulation(sim) {
  const host = $('#sim-body');
  host.textContent = '';
  if (!sim || !sim.order || !sim.order.length) {
    host.appendChild(el('p', { class: 'empty', text: '尚未运行。' }));
    return;
  }
  const wrap = el('div', { class: 'stack' });
  const p = sim.params || {};
  wrap.appendChild(el('p', { class: 'small muted', text: `参数：${p.runs} 轮，α=${pct(p.alpha ?? 0, 0)}，上限 ${pct(p.max_share ?? 0, 0)}，种子 ${p.seed}。误差为 |估计 − 真值|；覆盖率为误差范围盖住真值的比例（前提成立时）。` }));

  const table = el('table');
  table.appendChild(el('thead', {}, el('tr', {}, [
    el('th', { text: '样例' }),
    el('th', { class: 'num', text: '直接加权 p90' }),
    el('th', { class: 'num', text: '截尾 p90' }),
    el('th', { class: 'num', text: 'Huber p90' }),
    el('th', { class: 'num', text: '中位数 p90' }),
    el('th', { class: 'num', text: '截尾收益' }),
    el('th', { class: 'num', text: '范围覆盖率' }),
    el('th', { text: '保证状态' }),
  ])));
  const tb = el('tbody');
  for (const key of sim.order) {
    const row = sim.scenarios[key];
    if (!row) continue;
    const cov = row.bound_coverage;
    tb.appendChild(el('tr', {}, [
      el('td', {}, [
        el('div', { text: row.label || key }),
        el('div', { class: 'muted small', text: key }),
      ]),
      el('td', { class: 'num', text: fmt(row.results.naive_uncapped.p90_abs_err, 2) }),
      el('td', { class: 'num', text: fmt(row.results.trimmed.p90_abs_err, 2) }),
      el('td', { class: 'num', text: fmt(row.results.huber.p90_abs_err, 2) }),
      el('td', { class: 'num', text: fmt(row.results.median.p90_abs_err, 2) }),
      el('td', { class: 'num', text: row.gain_uncapped_vs_trimmed_p90 ? `${row.gain_uncapped_vs_trimmed_p90.toFixed(2)}×` : '—' }),
      el('td', { class: 'num', text: cov.graded ? pct(cov.rate, 0) : '—' }),
      el('td', {}, [el('span', {
        class: `chip ${cov.graded ? 'good' : 'bad'}`,
        text: cov.graded ? `${cov.graded}/${row.runs} 已评级` : `全部撤销 (${cov.void})`,
      })]),
    ]));
  }
  table.appendChild(tb);
  const tv = el('div', { class: 'table-view', style: 'max-height:none' });
  tv.appendChild(table);
  wrap.appendChild(tv);

  if (sim.null) {
    wrap.appendChild(el('h3', { text: '误伤边界：干净面板在改变可信假设后的中心漂移' }));
    const t2 = el('table');
    t2.appendChild(el('thead', {}, el('tr', {}, [
      el('th', { text: '设定（相对 α=10%、上限 25%）' }),
      el('th', { class: 'num', text: '截尾漂移 p90' }),
      el('th', { class: 'num', text: 'Huber 漂移 p90' }),
      el('th', { class: 'num', text: '中位数漂移 p90' }),
    ])));
    const tb2 = el('tbody');
    for (const [key, val] of Object.entries(sim.null.by_setting)) {
      tb2.appendChild(el('tr', {}, [
        el('td', { text: key }),
        el('td', { class: 'num', text: fmt(val.results.trimmed.p90_shift, 3) }),
        el('td', { class: 'num', text: fmt(val.results.huber.p90_shift, 3) }),
        el('td', { class: 'num', text: fmt(val.results.median.p90_shift, 3) }),
      ]));
    }
    t2.appendChild(tb2);
    const tv2 = el('div', { class: 'table-view', style: 'max-height:none' });
    tv2.appendChild(t2);
    wrap.appendChild(tv2);
    wrap.appendChild(el('p', { class: 'small muted', text: '模型：基线样例（无损坏单位），同一批取值在不同 α / 上限下重算。数值越小，说明改动可信假设对合法数据的扰动越小。' }));
  }
  host.appendChild(wrap);
}

/* -------------------------------------------------------------------------
   data flow
   ------------------------------------------------------------------------- */

function params() {
  const p = {
    alpha: state.alpha, max_share: state.cap, estimator: state.estimator,
    period: state.analysis?.config?.period || '2026-W46',
  };
  const metric = state.metric || state.analysis?.config?.metric;
  if (metric) p.metric = metric;
  return new URLSearchParams(p).toString();
}

function syncMetricSelect(a) {
  const sel = $('#metric-select');
  const metrics = a?.metrics || [];
  if (!metrics.length) { sel.disabled = true; return; }
  sel.disabled = false;
  const current = a.config.metric;
  const options = metrics.map((m) => ({ value: m, text: m + (a.metric_truth?.[m] !== undefined ? ' · 有真值' : '') }));
  sel.textContent = '';
  for (const o of options) sel.appendChild(el('option', o));
  sel.value = current;
  state.metric = current;
}

async function loadDatasets() {
  const data = await api('/api/datasets');
  const sel = $('#dataset-select');
  sel.textContent = '';
  for (const ds of data.datasets) {
    sel.appendChild(el('option', { value: ds.id, text: `${ds.name}（${ds.receipts} 条）` }));
  }
  if (!data.datasets.length) {
    const res = await api('/api/datasets/demo', { method: 'POST', body: { scenario: 'coordinate', seed: 20261124 } });
    state.datasetId = res.dataset_id;
    await loadDatasets();
    return;
  }
  state.datasetId = state.datasetId || data.datasets[0].id;
  sel.value = state.datasetId;
}

async function refresh(reloadPanel = true) {
  if (!state.datasetId) return;
  const a = await api(`/api/dataset/${state.datasetId}/analysis?${params()}`);
  state.analysis = a;
  if (reloadPanel || !state.panel || state.panel.dataset.id !== state.datasetId) {
    state.panel = await api(`/api/dataset/${state.datasetId}/panel`);
    state.simulation = await api(`/api/dataset/${state.datasetId}/simulation`);
    if (state.simulation?.order?.length) {
      renderSimulation(state.simulation);
      $('#sim-status').textContent = `${state.simulation.params.runs} 轮 · α ${pct(state.simulation.params.alpha, 0)}`;
    } else {
      renderSimulation(null);
      $('#sim-status').textContent = '';
    }
  }
  syncMetricSelect(a);
  renderHero(a);
  renderAssumptions(a);
  renderEstimateChart(a);
  renderWeightChart(a);
  renderCompare(a);
  renderIsolations(a);
  renderBatches(a, state.panel);
  renderCurve(a);
  if (state.selectedUnit && a.contributions.some((c) => c.unit_id === state.selectedUnit)) {
    selectUnit(state.selectedUnit);
  }
}

function bindControls() {
  $('#dataset-select').addEventListener('change', async (ev) => {
    state.datasetId = ev.target.value;
    state.selectedUnit = null;
    state.metric = null;
    await refresh(true);
  });

  $('#metric-select').addEventListener('change', async (ev) => {
    state.metric = ev.target.value;
    state.selectedUnit = null;
    await refresh(false);
  });

  $('#btn-load-demo').addEventListener('click', async () => {
    const scenario = prompt(
      '选择虚构样例：\n' + Object.entries(state.analysis?.scenarios || {}).map(([k, v]) => `${k} — ${v.label}`).join('\n'),
      'coordinate',
    );
    if (!scenario) return;
    try {
      const res = await api('/api/datasets/demo', { method: 'POST', body: { scenario, seed: Math.floor(Math.random() * 1e6) } });
      state.datasetId = res.dataset_id;
      state.selectedUnit = null;
      state.metric = null;
      await loadDatasets();
      $('#dataset-select').value = state.datasetId;
      await refresh(true);
      toast('已载入新样例（旧数据集保留）');
    } catch (err) { toast(err.message); }
  });

  $('#btn-theme').addEventListener('click', () => {
    const root = document.documentElement;
    const cur = root.getAttribute('data-theme');
    root.setAttribute('data-theme', cur === 'dark' ? 'light' : 'dark');
    if (state.analysis) {
      renderEstimateChart(state.analysis);
      renderWeightChart(state.analysis);
      renderCurve(state.analysis);
    }
  });

  $$('[data-toggle-table]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = document.getElementById(btn.dataset.toggleTable);
      const show = target.hidden;
      target.hidden = !show;
      btn.setAttribute('aria-pressed', String(show));
      btn.textContent = show ? '隐藏表格' : '表格';
    });
  });

  // intake dialog
  $('#btn-intake').addEventListener('click', () => {
    $('#intake-result').textContent = '';
    openDialog('#intake-dialog');
  });
  $$('#intake-dialog [data-close]').forEach((b) => b.addEventListener('click', () => closeDialog('#intake-dialog')));
  $('#intake-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const json = $('#f-json').value.trim();
    let receipts = [];
    if (json) {
      try {
        const parsed = JSON.parse(json);
        // Accept a bare receipt, an array, or the API's {"receipts": [...]}
        // envelope — the last is what people copy from the API docs.
        const rows = Array.isArray(parsed) ? parsed : [parsed];
        receipts = rows.flatMap((r) => (r && Array.isArray(r.receipts) ? r.receipts : [r]));
      } catch (err) {
        $('#intake-result').textContent = `JSON 解析失败：${err.message}`;
        return;
      }
    } else {
      const base = {
        unit_id: $('#f-unit').value.trim(),
        batch_id: $('#f-batch').value.trim(),
        metric: $('#f-metric').value.trim(),
        period: $('#f-period').value.trim(),
        mean: Number($('#f-mean').value),
        std: Number($('#f-std').value),
        n_effective: Number($('#f-n').value),
        range_low: Number($('#f-lo').value),
        range_high: Number($('#f-hi').value),
      };
      const missing = ['unit_id', 'batch_id', 'mean', 'n_effective', 'range_low', 'range_high']
        .filter((k) => base[k] === '' || base[k] === null || Number.isNaN(base[k]));
      if (missing.length) {
        $('#intake-result').textContent = `请填写：${missing.join(', ')}；或改用下方 JSON。`;
        return;
      }
      base.summary_id = `${base.unit_id}-${base.period}`;
      receipts = [base];
    }
    try {
      const res = await api(`/api/dataset/${state.datasetId}/intake`, {
        method: 'POST',
        body: { receipts, note: $('#f-note').value.trim() || undefined },
        quiet: true,
      });
      state.analysis = res.analysis;
      const conflicts = res.conflicts || [];
      $('#intake-result').textContent = `已保存 ${res.saved.length} 条` + (conflicts.length ? `；${conflicts.length} 条冲突：${conflicts.map((c) => c.summary_id + ' ' + c.error).join('；')}` : '');
      await refresh(true);
      toast(`已保存 ${res.saved.length} 条摘要`);
    } catch (err) {
      $('#intake-result').textContent = err.message;
    }
  });

  // simulation dialog
  $('#btn-simulate').addEventListener('click', () => openDialog('#sim-dialog'));
  bindInjectDialog();
  $('#btn-run-sim').addEventListener('click', () => runSimulation());
  $$('#sim-dialog [data-close-sim]').forEach((b) => b.addEventListener('click', () => closeDialog('#sim-dialog')));
  $('#s-run').addEventListener('click', () => runSimulation());
  $$('#trace-dialog [data-close-trace]').forEach((b) => b.addEventListener('click', () => closeDialog('#trace-dialog')));
}

/* corruption-injection dialog (demo only) */
function bindInjectDialog() {
  $('#btn-inject').addEventListener('click', openInjectDialog);
  $$('#inject-dialog [data-close-inject]').forEach((b) => b.addEventListener('click', () => closeDialog('#inject-dialog')));
  $('#j-unit').addEventListener('change', () => fillInjectUnit());
  $('#j-metric').addEventListener('change', () => fillInjectUnit());
  $('#j-preset-shift').addEventListener('click', () => {
    const u = injectUnit();
    if (!u) return;
    const means = state.analysis.contributions.map((c) => c.mean);
    const span = Math.max(...means) - Math.min(...means);
    $('#j-mean').value = (u.mean + 3 * span).toFixed(2);
    $('#j-n').value = u.n_effective;
    $('#j-std').value = Math.max(1, u.std * 0.2).toFixed(2);
    $('#j-hint').textContent = '已填入“均值上移 3 倍跨度”；提交方式由上面的下拉决定';
  });
  $('#j-preset-inflate').addEventListener('click', () => {
    const u = injectUnit();
    if (!u) return;
    $('#j-mean').value = u.mean.toFixed(2);
    $('#j-n').value = Math.round(u.n_effective * 20);
    $('#j-std').value = Math.max(0.1, u.std * 0.2).toFixed(2);
    $('#j-hint').textContent = '已填入“数量放大 20 倍”；提交方式由上面的下拉决定';
  });
  $('#inject-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      unit_id: $('#j-unit').value,
      metric: $('#j-metric').value,
      mode: $('#j-mode').value,
      mean: Number($('#j-mean').value),
      n_effective: Number($('#j-n').value),
      std: Number($('#j-std').value || 1),
    };
    try {
      await api(`/api/dataset/${state.datasetId}/inject`, { method: 'POST', body, quiet: true });
      await refresh(false);
      $('#inject-result').textContent = '已追加注入报文；原报文保留在库中。';
      toast(body.mode === 'split' ? '已拆报注入：看该单位权重是否仍被封顶' : '已更正注入：原贡献已被替换');
    } catch (err) {
      $('#inject-result').textContent = err.message;
    }
  });
}

function openInjectDialog() {
  const a = state.analysis;
  if (!a) return;
  const unitSel = $('#j-unit');
  unitSel.textContent = '';
  const units = Array.from(new Set(a.contributions.map((c) => c.unit_id))).sort();
  for (const u of units) unitSel.appendChild(el('option', { value: u, text: u }));
  const metricSel = $('#j-metric');
  metricSel.textContent = '';
  for (const m of (a.metrics || [a.config.metric])) {
    metricSel.appendChild(el('option', { value: m, text: m }));
  }
  metricSel.value = a.config.metric;
  $('#j-hint').textContent = '';
  $('#inject-result').textContent = '';
  fillInjectUnit();
  openDialog('#inject-dialog');
}

function injectUnit() {
  const a = state.analysis;
  if (!a) return null;
  const uid = $('#j-unit').value;
  const metric = $('#j-metric').value || a.config.metric;
  const active = a.contributions.filter((c) => c.unit_id === uid);
  if (a.config.metric === metric && active.length) {
    // Pool the active fragments so a split report is injected as one unit.
    return {
      summary_id: active.map((c) => c.summary_id).join(' + '),
      batch_id: active[0].batch_id,
      mean: active.reduce((s, c) => s + c.mean * c.claimed_weight, 0)
        / (active.reduce((s, c) => s + c.claimed_weight, 0) || 1),
      std: Math.sqrt(active.reduce((s, c) => s + c.std ** 2, 0) / active.length),
      n_effective: active.reduce((s, c) => s + c.claimed_weight, 0),
      known: true,
    };
  }
  const receipts = (state.panel?.receipts || []).filter((r) => r.unit_id === uid && r.metric === metric);
  if (!receipts.length) return null;
  const last = receipts[receipts.length - 1];
  return {
    summary_id: last.summary_id, batch_id: last.batch_id, mean: last.mean,
    std: last.std, n_effective: last.n_effective, known: false,
  };
}

function fillInjectUnit() {
  const u = injectUnit();
  if (!u) {
    $('#j-hint').textContent = '该单位在此指标下没有有效摘要';
    return;
  }
  $('#j-mean').value = u.mean.toFixed(2);
  $('#j-n').value = Math.round(u.n_effective);
  $('#j-std').value = u.std.toFixed(2);
  $('#j-hint').textContent = `基准：${u.summary_id} · ${u.batch_id}`;
}

async function runSimulation() {
  const body = {
    runs: Number($('#s-runs').value || 100),
    seed: Number($('#s-seed').value || 7),
    alpha: Number($('#s-alpha').value || 0.12),
    max_share: Number($('#s-cap').value || 0.10),
    scenario: 'all',
  };
  $('#s-progress').textContent = '运行中…';
  try {
    const res = await api(`/api/dataset/${state.datasetId}/simulate`, { method: 'POST', body });
    state.simulation = res;
    renderSimulation(res);
    $('#sim-status').textContent = `${res.params.runs} 轮 · α ${pct(res.params.alpha, 0)}`;
    $('#s-progress').textContent = '完成';
    closeDialog('#sim-dialog');
    toast('模拟完成');
  } catch (err) {
    $('#s-progress').textContent = `失败：${err.message}`;
  }
}

/* alpha / cap / estimator controls live in the hero strip */
function bindAnalysisControls() {
  const strip = document.createElement('div');
  strip.className = 'row';
  strip.style.marginTop = '16px';
  strip.style.paddingTop = '14px';
  strip.style.borderTop = '1px solid var(--grid)';
  strip.innerHTML = `
    <label class="control"><span>污染上限 α</span>
      <div class="row"><input type="range" id="ctl-alpha" min="0.01" max="0.45" step="0.01" value="0.10">
      <span class="value-pill" id="ctl-alpha-val">10%</span></div>
    </label>
    <label class="control"><span>单单位权重上限</span>
      <div class="row"><input type="range" id="ctl-cap" min="0.01" max="0.50" step="0.01" value="0.10">
      <span class="value-pill" id="ctl-cap-val">10%</span></div>
    </label>
    <label class="control"><span>选用估计量</span>
      <select id="ctl-estimator"></select>
    </label>
    <span class="spacer"></span>
    <span class="small muted" id="ctl-hint"></span>
  `;
  $('#hero-facts').after(strip);

  const estSel = $('#ctl-estimator');
  estSel.textContent = '';
  for (const [k, label] of Object.entries(EST_LABELS)) {
    estSel.appendChild(el('option', { value: k, text: label }));
  }
  estSel.value = state.estimator;

  let timer = null;
  const schedule = (delay = 120) => {
    clearTimeout(timer);
    timer = setTimeout(() => refresh(false).catch(() => {}), delay);
  };
  $('#ctl-alpha').addEventListener('input', (ev) => {
    state.alpha = Number(ev.target.value);
    $('#ctl-alpha-val').textContent = pct(state.alpha, 0);
    schedule();
  });
  $('#ctl-cap').addEventListener('input', (ev) => {
    state.cap = Number(ev.target.value);
    $('#ctl-cap-val').textContent = pct(state.cap, 0);
    schedule();
  });
  estSel.addEventListener('change', (ev) => {
    state.estimator = ev.target.value;
    schedule(0);
  });
}

/* -------------------------------------------------------------------------
   boot
   ------------------------------------------------------------------------- */

async function boot() {
  bindControls();
  bindAnalysisControls();
  try {
    await loadDatasets();
    await refresh(true);
  } catch (err) {
    errorBox(`启动失败：${err.message}`);
  }
}

document.addEventListener('DOMContentLoaded', boot);
