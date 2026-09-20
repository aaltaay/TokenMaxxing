'use strict';

/* AI Usage Command Center — renderer.
   Reads from the Python engine over window.hud and paints three views:
   Overview (dial + attention + tiles), This chat, Resets. */

const POLL = {
  clock: 10000,    // minute-scale reset countdowns and snapshot freshness
  providers: 60000, // actual provider quota endpoints
  due: 5000,       // buzz check
  local: 2000,     // Cursor sqlite snapshot
  cycle: 240000,   // dashboard fetch
};

// Status colours. `maxxed` resolves to the provider's own hue at the call
// site — reaching the ceiling is identity + glow, never an alarm colour.
const SEVERITY = { ok: 'var(--ok)', soon: 'var(--soon)', now: 'var(--now)', off: 'var(--off)' };

const state = {
  cycle: null,
  cycleError: null,
  cycleAt: 0,
  fetching: false,
  local: null,
  providers: null,
  providersError: null,
  providersFetching: false,
  providersRefreshPending: false,
  links: {},
  pinned: null,
  sessionProvider: 'auto',
  sessionFollow: 'active',
  activeChatSupported: true,
  localFetching: false,
  // Auto mode's own findings: which of Codex/Claude it last saw open, and
  // whether that is still true as of the latest poll.
  autoDetected: null,
  autoActive: false,
  focus: null,
  view: 'overview',
};

const $ = (id) => document.getElementById(id);

// ── formatting ────────────────────────────────────────────────────────────

const known = (value) => typeof value === 'number' && Number.isFinite(value);
const money = (cents, decimals = 2) =>
  !known(cents) ? '—' : `$${(cents / 100).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;

function compact(n) {
  if (!known(n)) return '—';
  const v = n;
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(Math.round(v));
}

const pct = (n, digits = 0) => known(n) ? `${n.toFixed(digits)}%` : '—';

function relativeTime(epochSeconds) {
  if (!epochSeconds) return 'never';
  const secs = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

/** Build an element. `attrs.class`, `attrs.text`, `attrs.vars` ({name: value}) are special. */
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'vars') for (const [k, v] of Object.entries(value)) node.style.setProperty(k, v);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
  return node;
};

function icon(paths, size = 18) {
  const svg = svgEl('svg', {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', 'stroke-width': 2.2,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
  });
  for (const d of [].concat(paths)) svg.append(svgEl('path', { d }));
  return svg;
}

const ICONS = {
  alert: ['M12 8v5', 'M12 17h.01', 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'],
  check: ['M20 6 9 17l-5-5'],
  clock: ['M12 6v6l4 2', 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z'],
  plus: ['M12 5v14', 'M5 12h14'],
  spark: ['M12 2.6 14.3 9 20.9 11.3 14.3 13.6 12 20.1 9.7 13.6 3.1 11.3 9.7 9z'],
};

// ── tooltip ───────────────────────────────────────────────────────────────

const tooltip = {
  node: null,
  show(html, event) {
    if (!this.node) this.node = $('tooltip');
    this.node.replaceChildren(...[].concat(html));
    this.node.classList.add('is-on');
    const box = this.node.getBoundingClientRect();
    const x = Math.min(Math.max(8, event.clientX + 14), window.innerWidth - box.width - 8);
    const y = Math.min(Math.max(8, event.clientY + 16), window.innerHeight - box.height - 8);
    this.node.style.left = `${x}px`;
    this.node.style.top = `${y}px`;
  },
  hide() {
    if (this.node) this.node.classList.remove('is-on');
  },
};

function withTooltip(node, build) {
  node.addEventListener('mousemove', (e) => tooltip.show(build(), e));
  node.addEventListener('mouseleave', () => tooltip.hide());
  return node;
}

// ── overview: the dial ────────────────────────────────────────────────────

// Sized so the innermost band clears the hero figure: inner edge at r=58.
const DIAL = { cx: 112, cy: 112, outer: 100, step: 18, poolWidth: 13, windowWidth: 8 };

// Colour follows the provider, never its rank or its current severity — a
// provider keeps its hue whether it is at 4% or 104%.
const PROVIDER_HUE = {
  cursor: 'var(--brand-cursor)',
  claude: 'var(--brand-claude)',
  codex: 'var(--brand-codex)',
};
const SPARE_HUES = ['var(--brand-4)', 'var(--brand-5)', 'var(--brand-6)'];

function resetText(epoch) {
  if (!known(epoch)) return 'Reset time unavailable';
  if (epoch <= Date.now() / 1000) return 'Awaiting provider reset update';
  return `Resets ${new Date(epoch * 1000).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}`;
}

function remainingTime(epoch) {
  if (!known(epoch)) return '—';
  const seconds = Math.floor(epoch - Date.now() / 1000);
  if (seconds <= 0) return 'Awaiting update';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

// A reading is shown with its age for as long as the engine holds it, so a
// refused or slow poll no longer blanks a provider and fills it back in.
const HOLD_SECONDS = 900;

function providerCurrent(provider) {
  return provider && ['ok', 'available'].includes(provider.status) &&
    known(provider.fetched_at) && Date.now() / 1000 - provider.fetched_at >= 0 &&
    Date.now() / 1000 - provider.fetched_at <= HOLD_SECONDS && !provider.stale;
}

function windowCurrent(window) {
  return window.status !== 'expired' && (!known(window.resets_at) || window.resets_at > Date.now() / 1000);
}

function providerBands() {
  const cycle = state.cycle;
  const bands = [{
    id: 'cursor', label: 'Cursor', kind: 'pool', value: cycle?.total_pct,
    display: known(cycle?.total_pct) ? `${pct(cycle.total_pct)} used` : 'Unavailable',
    hint: cycle ? `Other models ${pct(cycle.api_pct)} · Cursor models ${pct(cycle.auto_pct)}` : 'Waiting for Cursor dashboard',
    tip: `Cursor dashboard · ${relativeTime(cycle?.fetched_at)}. ${state.cycleError ? 'Refresh failed; last snapshot shown.' : 'Reported billing-cycle usage.'}`,
  }];
  for (const [id, label] of [['claude','Claude'], ['codex','Codex']]) {
    const provider = state.providers?.providers?.find(p => p.id === id);
    const current = providerCurrent(provider);
    const windows = current ? (provider.windows || []).filter(windowCurrent) : [];
    const core = windows.filter(w => w.id?.startsWith('codex:'));
    const candidates = id === 'codex' && core.length ? core : windows;
    const selected = (id === 'codex' && candidates.find(w => w.window_minutes === 10080)) || candidates.find(w => known(w.used_percent));
    const valid = selected && known(selected.used_percent);
    const reason = state.providersError || provider?.reason ||
      (provider && !current ? 'Snapshot expired; waiting for fresh data' : 'Connecting to provider usage');
    bands.push({
      id, label, kind: 'pool', value: valid ? selected.used_percent : null,
      display: valid ? `${pct(selected.used_percent)} used` : 'Unavailable',
      hint: valid ? `${selected.label} · ${resetText(selected.resets_at)}${provider.warning ? ' · holding last reading' : ''}` : reason,
      tip: valid ? `${provider.source} · checked ${relativeTime(provider.fetched_at)}. ${provider.warning ? provider.warning + ' ' : ''}` +
        windows.map(w => `${w.label}: ${pct(w.used_percent)} used; ${resetText(w.resets_at)}`).join(' · ') : reason,
    });
  }
  return bands.map(band => ({...band, hue:PROVIDER_HUE[band.id]}));
}

/** SVG arc path from 0deg (12 o'clock) clockwise through `sweep` degrees. */
function arcPath(cx, cy, r, sweep) {
  const point = (deg) => {
    const a = ((deg - 90) * Math.PI) / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  };
  const [x1, y1] = point(0);
  const [x2, y2] = point(sweep);
  return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${r} ${r} 0 ${sweep > 180 ? 1 : 0} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

function renderDial() {
  const group = $('dialRings');
  const dial = $('dial');
  group.replaceChildren();

  const bands = providerBands();
  if (!bands.length) {
    $('dialValue').textContent = '—';
    $('dialLabel').textContent = state.cycleError ? 'no data' : 'included usage';
    return;
  }

  bands.forEach((band, i) => {
    const r = DIAL.outer - i * DIAL.step;
    const circumference = 2 * Math.PI * r;
    const hasValue = known(band.value);
    const value = hasValue ? Math.max(0, Math.min(100, band.value)) : 0;
    const isPool = band.kind === 'pool';
    const width = isPool ? DIAL.poolWidth : DIAL.windowWidth;
    const focused = state.focus === band.id;

    group.append(svgEl('circle', {
      cx: DIAL.cx, cy: DIAL.cy, r,
      class: 'dial-track',
      'stroke-width': width,
      // A just-reset window draws almost no arc, so its track has to be
      // visible enough that the provider still reads as present on the dial.
      stroke: `color-mix(in srgb, ${band.hue} ${isPool ? 20 : 34}%, transparent)`,
      'stroke-dasharray': isPool ? null : '2 4',
      'stroke-linecap': isPool ? 'butt' : 'round',
    }));

    if (!hasValue) return;
    const maxxed = isPool && value >= 100;
    const cls = `dial-arc${focused ? ' is-focus' : ''}${maxxed ? ' is-maxxed' : ''}`;
    if (isPool) {
      // At a full band the two round caps overlap and bulge, so close the ring.
      const arc = svgEl('circle', {
        cx: DIAL.cx, cy: DIAL.cy, r,
        class: cls,
        'stroke-width': width,
        stroke: band.hue,
        'stroke-linecap': maxxed ? 'butt' : 'round',
        'stroke-dasharray': maxxed ? null : `${Math.max((circumference * value) / 100, value > 0 ? 3 : 0)} ${circumference}`,
        transform: `rotate(-90 ${DIAL.cx} ${DIAL.cy})`,
      });
      if (maxxed) arc.style.setProperty('--glow', band.hue);
      group.append(arc);
    } else if (value > 0.5) {
      // Dashed texture is the secondary encoding that keeps a time window from
      // reading as a consumption percentage.
      group.append(svgEl('path', {
        class: cls,
        d: arcPath(DIAL.cx, DIAL.cy, r, Math.min(359.9, (value * 360) / 100)),
        fill: 'none',
        stroke: band.hue,
        'stroke-width': width,
        'stroke-linecap': 'round',
        'stroke-dasharray': '2 4',
      }));
    }
  });

  dial.classList.toggle('has-focus', Boolean(state.focus));
  const total = state.cycle?.total_pct;
  const value = $('dialValue');
  value.textContent = pct(total);
  value.classList.toggle('is-maxxed', total >= 100);
  value.style.setProperty('--c', PROVIDER_HUE.cursor);
  $('dialLabel').textContent = 'Cursor used';
}

/** A miniature of the dial with band `index` lit — the legend's key. */
function bandGlyph(count, index, hue, kind) {
  const svg = svgEl('svg', { class: 'legend-glyph', width: 15, height: 15, viewBox: '0 0 15 15', 'aria-hidden': 'true' });
  for (let i = 0; i < count; i += 1) {
    const on = i === index;
    svg.append(svgEl('circle', {
      cx: 7.5, cy: 7.5, r: 6.3 - i * 2.3,
      fill: 'none',
      'stroke-width': on ? 2 : 1.4,
      stroke: on ? hue : 'var(--ink-4)',
      'stroke-dasharray': on && kind === 'window' ? '1 1.6' : null,
      opacity: on ? 1 : 0.3,
    }));
  }
  return svg;
}

function renderLegend() {
  const legend = $('legend');
  legend.replaceChildren();

  const bands = providerBands();
  if (!bands.length) {
    legend.append(el('div', {
      class: 'empty',
      text: state.cycleError || 'Waiting for the first usage fetch…',
    }));
    return;
  }

  bands.forEach((band, i) => {
    const row = el('button', {
      class: `legend-row${band.kind === 'window' ? ' is-window' : ''}`,
      type: 'button',
      onmouseenter: () => { state.focus = band.id; renderDial(); },
      onmouseleave: () => { state.focus = null; renderDial(); },
      onclick: () => (band.id === 'cursor' ? openDashboard() : selectView('resets')),
    }, [
      bandGlyph(bands.length, i, band.hue, band.kind),
      el('span', {}, [
        el('div', { class: 'legend-name', text: band.label }),
        el('div', { class: 'legend-hint', text: band.hint }),
      ]),
      el('span', { class: 'legend-pct', text: band.display }),
    ]);
    withTooltip(row, () => [
      el('b', { text: `${band.label} — ${band.display}` }),
      el('div', { class: 'muted', text: band.tip }),
    ]);
    legend.append(row);
    if (band.id !== 'cursor' && !known(band.value)) legend.append(connectionControls(band.id, band.label, false));
  });

  renderPools();
}

/** Cursor's individual pools, below the dial as thin linear meters. */
function renderPools() {
  const host = $('pools');
  host.replaceChildren();
  const meters = state.cycle?.meters || [];
  if (!meters.length) return;

  for (const meter of meters) {
    const maxxed = meter.status === 'maxxed';
    const row = el('div', { class: `pool${maxxed ? ' is-maxxed' : ''}` }, [
      el('span', { class: 'pool-name', text: meter.label }),
      el('span', { class: 'pool-pct' }, [
        el('span', { text: pct(meter.pct) }),
        maxxed ? el('span', { class: 'pool-max', text: '✦' }) : null,
      ]),
    ]);
    row.style.setProperty('--c', PROVIDER_HUE.cursor);
    // These are all Cursor pools, so the bar carries Cursor's hue. Severity
    // tints only the number, and only once it is genuinely critical — three
    // red bars would shout the same thing the alert above already says.
    const bar = el('div', { class: `meter is-plain pool-bar${maxxed ? ' is-maxxed' : ''}` }, [el('i', {})]);
    bar.style.setProperty('--c', PROVIDER_HUE.cursor);
    bar.querySelector('i').style.width = known(meter.pct) ? `${Math.max(0, Math.min(100, meter.pct)).toFixed(1)}%` : '0%';
    row.append(bar);
    withTooltip(row, () => [
      el('b', { text: `${meter.label} — ${pct(meter.pct, 1)} consumed` }),
      el('div', { class: 'muted', text: meter.hint }),
    ]);
    host.append(row);
  }
}

// ── overview: attention ───────────────────────────────────────────────────

function renderAttention() {
  const host = $('attention');
  host.replaceChildren();

  if (state.cycleError) {
    host.append(el('div', { class: 'alert', vars: { '--c': SEVERITY.now } }, [
      iconWithClass(ICONS.alert, 'alert-icon'),
      el('div', { class: 'alert-title', text: 'Usage engine could not read your cycle' }),
      el('div', { class: 'alert-detail', text: state.cycleError }),
    ]));
    return;
  }

  const items = state.cycle?.attention || [];
  if (!state.cycle) return;

  if (!items.length) {
    host.append(el('div', { class: 'all-clear' }, [
      iconWithClass(ICONS.check, '', 17),
      el('span', { text: 'No high usage reported in the available Cursor meters.' }),
    ]));
    return;
  }

  let linkShown = false;
  for (const item of items) {
    const celebrate = item.tone === 'celebrate';
    const color = celebrate
      ? PROVIDER_HUE[item.provider] || PROVIDER_HUE.cursor
      : SEVERITY[item.severity] || SEVERITY.soon;
    const alert = el('div', { class: `alert${celebrate ? ' is-celebrate' : ''}`, vars: { '--c': color } }, [
      iconWithClass(celebrate ? ICONS.spark : ICONS.alert, 'alert-icon'),
      el('div', { class: 'alert-title', text: item.title }),
      el('div', { class: 'alert-detail', text: item.detail }),
    ]);
    if (item.link && !linkShown) {
      linkShown = true;
      alert.append(el('div', { class: 'alert-actions' }, [
        el('button', {
          class: 'chip chip-primary',
          type: 'button',
          text: item.link.label,
          onclick: () => window.hud.openExternal(item.link.url),
        }),
      ]));
    }
    host.append(alert);
  }
}

function iconWithClass(paths, className, size = 18) {
  const node = icon(paths, size);
  if (className) node.setAttribute('class', className);
  return node;
}

// ── overview: tiles ───────────────────────────────────────────────────────

/** Sparkline: 12 points, de-emphasis hue, current period marked in the accent. */
function sparkline(points) {
  const width = 150;
  const height = 30;
  const values = points.slice(-12).map((p) => p.cents);
  const svg = svgEl('svg', { class: 'spark', viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  if (values.length < 2) return svg;

  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const y = (v) => height - 3 - (v / max) * (height - 6);
  const coords = values.map((v, i) => [i * step, y(v)]);
  const line = coords.map(([x, yy], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${yy.toFixed(1)}`).join(' ');

  const gradientId = `sparkFade-${Math.random().toString(36).slice(2, 8)}`;
  const defs = svgEl('defs');
  const gradient = svgEl('linearGradient', { id: gradientId, x1: 0, y1: 0, x2: 0, y2: 1 });
  gradient.append(svgEl('stop', { offset: '0%', 'stop-color': '#6e6e76', 'stop-opacity': 0.30 }));
  gradient.append(svgEl('stop', { offset: '100%', 'stop-color': '#6e6e76', 'stop-opacity': 0 }));
  defs.append(gradient);
  svg.append(defs);

  svg.append(svgEl('path', { d: `${line} L${width} ${height} L0 ${height} Z`, fill: `url(#${gradientId})` }));
  svg.append(svgEl('path', { class: 'spark-line', d: line }));
  const [lx, ly] = coords[coords.length - 1];
  svg.append(svgEl('circle', { class: 'spark-dot', cx: lx, cy: ly, r: 3 }));
  return svg;
}

function deltaNode(deltaPct, { upIsBad = true } = {}) {
  if (deltaPct === null || deltaPct === undefined) return null;
  const up = deltaPct >= 0;
  const bad = up === upIsBad;
  const color = Math.abs(deltaPct) < 5 ? 'var(--ink-3)' : bad ? 'var(--soon)' : 'var(--ok)';
  return el('span', {
    class: 'tile-delta',
    vars: { color },
    text: `${up ? '↑' : '↓'} ${Math.abs(deltaPct).toFixed(0)}% vs prior week`,
  });
}

function tile(label, value, extras = []) {
  return el('div', { class: 'tile' }, [
    el('div', { class: 'tile-label', text: label }),
    el('div', { class: 'tile-value', text: value }),
    ...[].concat(extras).filter(Boolean),
  ]);
}

function renderTiles() {
  const host = $('tiles');
  host.replaceChildren();
  const report = state.cycle;
  if (!report) {
    host.append(tile('Cursor usage', 'Unavailable', [el('div', {class:'tile-note', text:state.cycleError || 'Waiting for the dashboard response.'})]));
    return;
  }
  host.append(tile('Cursor reported spend', money(report.total_spend_cents), [
    el('div', {class:'tile-note',text:`${money(report.included_cents,0)} included · ${money(report.bonus_cents,0)} bonus`}),
    el('div', {class:'tile-note',text:`Dashboard usage value, including bonus · ${relativeTime(report.fetched_at)}`}),
  ]));
  host.append(tile('Cursor reported tokens', `${compact(report.agg_output)} out`, [
    el('div',{class:'tile-note',text:`${compact(report.agg_input)} in`}),
    el('div',{class:'tile-note',text:`${compact(report.agg_cache_read)} cache read`}),
    el('div',{class:'tile-note',text:`${compact(report.agg_cache_write)} cache write`}),
    ...(report.source_errors || []).map(error => el('div',{class:'tile-note',text:error})),
  ]));
  host.append(tile('Average spend so far', known(report.burn_cents_per_day) ? `${money(report.burn_cents_per_day,0)} / day` : 'Unavailable', [
    el('div',{class:'tile-note',text:'Calculated: reported spend ÷ elapsed cycle days'}),
  ]));
  const cycleEnd = report.cycle_end ? Date.parse(report.cycle_end) / 1000 : null;
  host.append(tile('Cursor cycle reset', known(cycleEnd) ? new Date(cycleEnd*1000).toLocaleDateString([], {month:'short',day:'numeric'}) : 'Unavailable', [
    el('div',{class:'tile-note',text:known(cycleEnd) ? resetText(cycleEnd) : 'Not returned by Cursor'}),
  ]));
  const runs = (report.conversations || []).slice(0,4);
  const coverage = `${compact(report.events_fetched)} of ${compact(report.events_total)} events loaded`;
  host.append(el('div',{class:'tile tile-span-2'},[
    el('div',{class:'tile-label',text:'Recorded agents & runs'}),
    report.events_complete ? el('div',{class:'runs'},runs.map(row => el('div',{class:'run'},[
      el('span',{class:`run-dot${row.headless ? ' is-cloud' : ''}`}),
      el('span',{class:'run-name',text:row.name,title:row.name}),
      el('span',{class:'run-cost',text:`${compact(row.n)} events · ${money(row.cents)}`}),
    ]))) : el('div',{class:'empty',text:'Breakdown unavailable: event history is incomplete.'}),
    el('div',{class:'tile-note',text:coverage}),
    el('div',{class:'tile-note',text:'Amounts are event-reported usage values, not cash charges.'}),
    report.event_costs_reconciled === false ? el('div',{class:'tile-note',text:'Event values do not reconcile to the dashboard cycle total.'}) : null,
    report.events_incomplete_reason ? el('div',{class:'tile-note',text:report.events_incomplete_reason}) : null,
    report.events_complete ? el('div',{class:'tile-note',text:`${compact(report.headless?.n)} events reported as cloud/headless`}) : null,
  ]));
}

// ── this chat ─────────────────────────────────────────────────────────────

function saveSessionChoice() {
  try { localStorage.setItem('sessionChoice', JSON.stringify({provider:state.sessionProvider,pinned:state.pinned,follow:state.sessionFollow})); }
  catch { /* Persistence is optional; current selection remains active. */ }
}

// Auto mode never queries the engine with 'auto' itself — it resolves to
// whichever of Codex/Claude was last seen open, defaulting to Codex only
// for display purposes before the first detection lands.
function effectiveProvider() {
  return state.sessionProvider === 'auto' ? (state.autoDetected || 'codex') : state.sessionProvider;
}

function renderChat() {
  const local = state.local;
  const select = $('chatSelect');
  const body = $('chatBody');
  const cats = $('chatCats');
  const billed = $('chatBilled');
  const provider = effectiveProvider();
  const isAuto = state.sessionProvider === 'auto';
  body.replaceChildren();
  cats.replaceChildren();
  billed.replaceChildren();
  $('chatTax').textContent = '—';
  $('sessionCostTitle').textContent = provider === 'cursor' ? 'Recorded usage value for this session'
    : provider === 'claude' ? 'Reported session cost' : 'Estimated session cost';
  $('sessionCostScope').textContent = provider === 'cursor' ? 'this cycle' : 'USD · session log';
  const followProvider = provider === 'codex' ? state.activeChatSupported : provider === 'claude';
  const followsOpen = isAuto ? state.autoActive : (followProvider && state.sessionFollow === 'active');
  const openLabel = provider === 'claude' ? 'Open Claude Code session' : 'Open Codex chat';
  $('sessionFollowRow').hidden = isAuto || !followProvider;
  $('sessionFollowOpen').textContent = openLabel;
  $('sessionMode').textContent = state.pinned ? 'Pinned session · stays on your selected chat.'
    : isAuto ? (state.autoActive ? `Auto · following the ${provider === 'claude' ? 'open Claude Code session' : 'open Codex chat'}.`
                                  : 'Auto · no open Codex or Claude Code chat detected right now.')
    : followsOpen ? `Following the ${provider === 'claude' ? 'open Claude Code session' : 'open Codex chat'} · updates when you switch chats.`
    : 'Latest activity · follows the most recently updated session, including background tasks.';
  $('btnUnpin').textContent = followsOpen ? openLabel : 'Latest activity';
  $('sessionBreakdownTitle').textContent = provider === 'cursor' ? 'Estimated context breakdown' : 'Recorded token usage';

  const picker = document.querySelector('.chat-picker');
  if (!local?.available) {
    $('chatModel').textContent = '—';
    picker.hidden = true;
    const why = local?.reason === 'Cursor DB not found'
      ? 'Cursor’s local database was not found. Sign in to Cursor and open a chat.'
      : local?.reason || 'Local chat data is unavailable.';
    body.append(el('div', { class: 'empty', text: why }));
    cats.append(el('div', { class: 'empty', text: 'No context snapshot to break down.' }));
    billed.append(el('div', { class: 'empty', text: 'No chat selected.' }));
    return;
  }
  picker.hidden = false;

  // Chat picker — rebuilt only when the list actually changes, so the open
  // dropdown does not slam shut on every poll.
  const signature = provider + JSON.stringify(local.chats || []);
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.replaceChildren(...(local.chats || []).map((c) => el('option', { value: c.id, text: c.name })));
  }
  select.value = local.chat?.id || '';

  const chat = local.chat;
  if (!chat || chat.error) {
    $('chatModel').textContent = '—';
    body.append(el('div', {
      class: 'empty',
      text: chat ? chat.error : local.reason || 'No session selected.',
    }));
    cats.append(el('div', { class: 'empty', text: 'No context snapshot to break down.' }));
    billed.append(el('div', { class: 'empty', text: 'Nothing to attribute yet.' }));
    return;
  }

  $('chatModel').textContent = chat.model || '—';
  if (provider !== 'cursor') {
    body.append(el('div', {class:'tile-value',text:known(chat.used) ? `${compact(chat.used)} input tokens` : 'Usage unavailable'}));
    body.append(el('p', {class:'tile-note',text:chat.measurement}));
    const usageTime = chat.usage_at ? Date.parse(chat.usage_at)/1000 : null;
    body.append(el('p', {class:'tile-note',text:`${chat.source} · session updated ${relativeTime(chat.updated_at)}${known(usageTime) ? ` · usage recorded ${relativeTime(usageTime)}` : ''}`}));
    body.append(el('p', {class:'tile-note',text:known(chat.limit) ? `Reported context limit: ${compact(chat.limit)} tokens` : 'Context limit was not recorded. No context percentage is inferred.'}));
    const metrics = Object.entries(chat.metrics || {});
    for (const [label,value] of metrics) cats.append(el('div',{class:'cat'},[
      el('span',{class:'cat-name',text:label}),el('span',{class:'cat-val',text:compact(value)})]));
    if (!metrics.length) cats.append(el('p',{class:'empty',text:'No token-usage record in the recent portion of this session log.'}));
    renderSessionCost(billed, chat.cost);
    return;
  }
  body.append(el('p',{class:'tile-note',text:`Session updated ${relativeTime(chat.updated_at)} · ${chat.measurement}`}));
  const usedPct = known(chat.used) && known(chat.limit) && chat.limit > 0 ? (100 * chat.used) / chat.limit : chat.pct;
  // A context window is not a billing pool — half full is unremarkable.
  const severity = usedPct >= 90 ? 'now' : usedPct >= 70 ? 'soon' : 'ok';

  body.append(el('div', { class: 'tile-value', text: `${compact(chat.used)} / ${compact(chat.limit)}` }));
  body.append(el('div', { class: 'tile-note', text: `${pct(usedPct, 1)} of context · ${chat.name} · Cursor local snapshot` }));
  const meter = el('div', { class: 'meter', vars: { '--c': severity === 'ok' ? PROVIDER_HUE.cursor : SEVERITY[severity] } }, [el('i', {})]);
  meter.querySelector('i').style.width = known(usedPct) ? `${Math.min(100, usedPct).toFixed(1)}%` : '0%';
  meter.classList.add('gap-above');
  body.append(meter);

  // Categories
  $('chatTax').textContent = `${pct(chat.tax_pct)} non-conversation context`;
  const rows = chat.cat_rows || [];
  if (!rows.length) {
    cats.append(el('div', { class: 'empty', text: 'No category breakdown in this snapshot.' }));
  }
  let group = null;
  for (const row of rows) {
    const nextGroup = row.overhead ? 'tax' : 'conversation';
    if (nextGroup !== group) {
      group = nextGroup;
      cats.append(el('div', {
        class: 'kicker cat-group',
        text: row.overhead ? 'Non-conversation context (Cursor estimate)' : 'Conversation (Cursor estimate)',
      }));
    }
    const node = el('div', { class: 'cat' }, [
      el('span', { class: 'cat-name', text: row.label }),
      el('span', { class: 'cat-val', text: `${compact(row.tokens)} · ${pct(row.share)}` }),
    ]);
    const bar = el('div', { class: 'meter is-plain cat-bar' }, [el('i', {})]);
    bar.style.setProperty('--c', row.overhead ? 'var(--ink-4)' : PROVIDER_HUE.cursor);
    bar.querySelector('i').style.width = known(row.share) ? `${Math.min(100, row.share).toFixed(1)}%` : '0%';
    node.append(bar);
    cats.append(node);
  }

  // Event-reported usage value for this chat, distinct from billed charges.
  const match = (state.cycle?.conversations || []).find((c) => c.id === chat.id);
  if (!state.cycle?.events_complete || !match) {
    billed.append(el('div', {
      class: 'empty',
      text: !state.cycle?.events_complete ? 'Chat usage value unavailable: complete event history has not been loaded.' : 'This chat was not found in the loaded usage results.',
    }));
  } else {
    billed.append(el('div', { class: 'tile-value', text: money(match.cents) }));
    billed.append(el('div', {class:'tile-note',text:'Event-reported usage value; this is not a cash charge.'}));
    billed.append(el('div', {
      class: 'tile-note',
      text: `${compact(match.n)} recorded events · source: Cursor usage history`,
    }));
    billed.append(el('div', {
      class: 'tile-note',
      text: `${compact(match.out)} out · ${compact(match.cr)} cache read`,
    }));
  }
}

function renderSessionCost(container, cost) {
  if (known(cost?.cents) && cost.reported) {
    // A value the provider itself reports; nothing here is estimated.
    container.append(el('div',{class:'tile-value',text:`≈ ${money(cost.cents, cost.cents < 100 ? 4 : 2)}`}));
    container.append(el('p',{class:'tile-note',text:cost.basis}));
    return;
  }
  if (!known(cost?.cents)) {
    container.append(el('p',{class:'empty',text:'Cost unavailable: no recorded requests with supported model pricing and complete token counts.'}));
    if (known(cost?.last_request_cents)) container.append(el('p',{class:'tile-note',text:`Last request estimate: ${money(cost.last_request_cents, 4)}`}));
    return;
  }
  container.append(el('div',{class:'tile-value',text:`≈ ${money(cost.cents, cost.cents > 0 && cost.cents < 1 ? 4 : 2)}`}));
  container.append(el('p',{class:'tile-note',text:cost.partial
    ? `Partial estimate · ${cost.priced_requests} priced requests. Some usage could not be priced or read.`
    : `${cost.priced_requests} recorded requests · API-equivalent estimate`}));
  if (known(cost.last_request_cents)) container.append(el('p',{class:'tile-note',text:`Last request: ≈ ${money(cost.last_request_cents, 4)}`}));
  container.append(el('p',{class:'tile-note',text:cost.basis}));
  container.append(el('p',{class:'tile-note',text:`Pricing checked ${cost.pricing_date} · excludes separate subagent logs.`}));
}

// ── resets ────────────────────────────────────────────────────────────────

async function startProviderLink(id) {
  try {
    handleLinkState(await window.hud.connectProvider(id));
  } catch (error) {
    state.links[id] = {provider:id,status:'error',message:engineMessage(error)};
  }
  renderResets();
  renderLegend();
}

function connectionControls(id, label, connected) {
  const link = state.links[id] || {};
  const busy = link.status === 'connecting';
  const host = el('div',{class:'provider-connection'});
  const actions = el('div',{class:'row-actions'});
  if (!connected || link.status === 'error' || busy) {
    actions.append(el('button',{
      class:'chip chip-primary',type:'button',disabled:busy,
      text:busy ? 'Waiting for sign-in…' : `Reconnect ${label}`,
      onclick:()=>startProviderLink(id),
    }));
  }
  if (busy && link.authUrl) actions.append(el('button',{
    class:'chip',type:'button',text:'Open sign-in page',onclick:()=>window.hud.reopenProviderLogin(id),
  }));
  if (busy) actions.append(el('button',{
    class:'chip',type:'button',text:'Cancel',onclick:async()=>{
      handleLinkState(await window.hud.cancelProviderLink(id));
    },
  }));
  else actions.append(el('button',{
    class:'chip',type:'button',text:connected?'Refresh usage':'Retry usage',onclick:()=>refreshProviders(true),
  }));
  if (link.installUrl) actions.append(el('button',{
    class:'chip',type:'button',text:`Get ${label}`,onclick:()=>window.hud.openProviderInstall(id),
  }));
  host.append(actions);
  const message = busy ? 'Finish signing in on the provider’s page. Usage will reconnect automatically.' :
    link.status==='error' ? link.message : link.status==='success' && !connected ? 'Sign-in completed. Waiting for verified usage data.' :
    connected ? 'Connected through your existing sign-in.' : 'Sign in once in your browser. The app will detect the connection.';
  host.append(el('p',{class:'tile-note gap-above',text:message,role:'status'}));
  return host;
}

function handleLinkState(link) {
  if (!link?.provider) return;
  const previous = state.links[link.provider];
  if (known(previous?.revision) && known(link.revision) && link.revision < previous.revision) return;
  state.links[link.provider] = link;
  if (link.status === 'success' && previous?.status !== 'success') {
    if (state.providers) state.providers.providers = state.providers.providers.filter(p=>p.id!==link.provider);
    refreshProviders(true);
  }
  renderResets();
  renderLegend();
}

function renderResets() {
  const host = $('resetCards');
  host.replaceChildren();
  $('resetsNow').textContent = new Date().toLocaleTimeString();
  for (const [id,label] of [['claude','Claude'],['codex','Codex']]) {
    const provider = state.providers?.providers?.find(p => p.id === id);
    const current = providerCurrent(provider);
    const card = el('div',{class:'card'},[
      el('div',{class:'card-head'},[
        el('h2',{text:label}),
        el('span',{class:'meta',text:current ? `Connected · checked ${relativeTime(provider.fetched_at)}` : 'Unavailable'}),
      ]),
    ]);
    if (!current || !provider.windows?.length) {
      const reason = state.providersError || provider?.reason || (provider ? 'No current provider snapshot available.' : 'Waiting for provider data.');
      card.append(el('div',{class:'connection-reason',text:reason.replace('Open Claude Code to reconnect.', 'Use Reconnect Claude below.')}));
    } else {
      card.append(el('div',{class:'tile-note',text:`Source: ${label} account usage`}));
      if (provider.warning) card.append(el('div',{class:'tile-note',text:`${provider.warning} Showing the reading from ${relativeTime(provider.fetched_at)}.`}));
      for (const window of provider.windows) {
        const clock = el('div',{class:'clock'},[
          el('div',{},[
            el('div',{class:'clock-name',text:window.label}),
            el('div',{class:'clock-when',text:resetText(window.resets_at)}),
          ]),
          el('div',{class:'clock-left',text:windowCurrent(window) && known(window.used_percent) ? `${pct(window.used_percent,1)} used` : 'Awaiting update'}),
        ]);
        if (windowCurrent(window) && known(window.used_percent)) {
          const bar = el('div',{class:'meter is-plain clock-meter',vars:{'--c':PROVIDER_HUE[id]}},[el('i',{})]);
          bar.querySelector('i').style.width = `${Math.min(100,Math.max(0,window.used_percent))}%`;
          clock.append(bar);
        }
        if (known(window.resets_at)) clock.append(el('div',{class:'tile-note',text:`Until reported reset: ${remainingTime(window.resets_at)}`}));
        card.append(clock);
      }
    }
    card.append(connectionControls(id,label,current));
    host.append(card);
  }
}

// ── chrome ────────────────────────────────────────────────────────────────

let bannerTimer = null;
function banner(text) {
  const node = $('banner');
  node.replaceChildren(iconWithClass(ICONS.clock, '', 15), el('span', { text }));
  node.classList.add('is-on');
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => node.classList.remove('is-on'), 3600);
}

/** Strip Electron's "Error invoking remote method 'x':" IPC wrapper. */
function engineMessage(err) {
  return String(err?.message || err).replace(/^Error invoking remote method '[^']+':\s*(Error:\s*)?/, '');
}

function setStatus(kind, text) {
  $('statusDot').style.setProperty('--c', SEVERITY[kind] || SEVERITY.off);
  $('statusText').textContent = text;
}

function openDashboard() {
  window.hud.openExternal('https://cursor.com/dashboard');
}

function selectView(name) {
  state.view = name;
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const on = tab.id === `tab-${name}`;
    tab.setAttribute('aria-selected', String(on));
    $(tab.getAttribute('aria-controls')).classList.toggle('is-active', on);
  }
  positionThumb();
}

function positionThumb() {
  const active = document.querySelector('[role="tab"][aria-selected="true"]');
  const thumb = $('segThumb');
  if (!active || !thumb) return;
  thumb.style.width = `${active.offsetWidth}px`;
  thumb.style.transform = `translateX(${active.offsetLeft - 2}px)`;
}

// ── data flow ─────────────────────────────────────────────────────────────

function paintOverview() {
  renderDial();
  renderLegend();
  renderAttention();
  renderTiles();
  const report = state.cycle;
  const age = known(report?.fetched_at) ? Date.now()/1000-report.fetched_at : Infinity;
  const stale = state.cycleError || report?.stale || age > 300;
  $('cycleMeta').textContent = report ? `${stale ? 'Last known' : 'Cursor snapshot'} · ${relativeTime(report.fetched_at)}` : 'No Cursor snapshot';
  $('brandSub').textContent = report ? `Cursor ${report.plan || ''}${report.email ? ` · ${report.email}` : ''}` : 'Provider-reported usage';
  $('statusEvents').textContent = report ? `${compact(report.events_fetched)} / ${compact(report.events_total)} events${report.events_complete ? '' : ' · incomplete'}` : '';
  if (state.fetching) setStatus('off','Refreshing Cursor data…');
  else if (state.cycleError) setStatus('now',`Cursor refresh failed · last snapshot ${relativeTime(report?.fetched_at)}`);
  else if (report) setStatus(stale || !report.events_complete ? 'soon' : 'ok', `Cursor ${stale ? 'stale' : 'snapshot'} · ${relativeTime(report.fetched_at)}${report.events_complete ? '' : ' · event history incomplete'}`);
  else setStatus('off','Waiting for Cursor data');
}

async function refreshCycle(force = false) {
  if (state.fetching) return;
  state.fetching = true;
  $('btnRefresh').classList.add('is-spinning');
  paintOverview();
  try {
    state.cycle = await window.hud.call('cycle',{force});
    state.cycleError = null;
  } catch (err) {
    state.cycleError = engineMessage(err);
    if (/account changed|no Cursor web session|Cursor DB not found/i.test(state.cycleError)) {
      state.cycle = null;
    }
  } finally {
    state.fetching = false;
    $('btnRefresh').classList.remove('is-spinning');
    paintOverview();
    renderChat();
  }
}

async function refreshProviders(force = false) {
  if (state.providersFetching) {
    state.providersRefreshPending ||= force;
    return;
  }
  state.providersFetching = true;
  try {
    state.providers = await window.hud.call('providers',{force});
    state.providersError = null;
  } catch (err) {
    // The previous snapshot stays on screen with its age; a failed call is
    // reported, never painted as missing data.
    state.providersError = `Provider refresh failed: ${engineMessage(err)}`;
  } finally {
    state.providersFetching = false;
    renderResets();
    paintOverview();
    if (state.providersRefreshPending) {
      state.providersRefreshPending = false;
      refreshProviders(true);
    }
  }
}

// Auto mode asks Codex and Claude in parallel whether either currently has
// an open chat (the same 'active' lookup each provider already supports),
// and follows whichever answers yes. Codex and Claude cannot both be the
// chat in front of you, but if a stale answer briefly makes it look that
// way, the provider already being followed wins rather than flip-flopping.
async function detectAutoProvider() {
  const jobs = [window.hud.call('sessions', {provider: 'claude', pinned: null, follow: 'active'})
    .then(r => ['claude', r]).catch(() => ['claude', null])];
  if (state.activeChatSupported) jobs.push(window.hud.call('sessions', {provider: 'codex', pinned: null, follow: 'active'})
    .then(r => ['codex', r]).catch(() => ['codex', null]));
  const found = (await Promise.all(jobs)).filter(([, r]) => r?.chat);
  return found.find(([id]) => id === state.autoDetected) || found[0] || null;
}

async function refreshLocal() {
  if (state.localFetching) return;
  const provider = state.sessionProvider;
  const pinned = state.pinned;
  const follow = state.sessionFollow;
  const stillCurrent = () => provider === state.sessionProvider && pinned === state.pinned && follow === state.sessionFollow;
  state.localFetching = true;
  try {
    if (provider === 'auto') {
      const pick = await detectAutoProvider();
      if (!stillCurrent()) return;
      if (pick) {
        state.autoDetected = pick[0];
        state.autoActive = true;
        state.local = pick[1];
      } else {
        state.autoActive = false;
        // Nothing open right now: keep whatever was last shown rather than
        // blanking it, same as the provider quota tiles hold a stale reading.
        if (!state.local) state.local = {available: true, chat: null,
          reason: 'No open Codex or Claude Code chat detected yet. Open one, or pick a provider above.'};
      }
      renderChat();
      return;
    }
    const result = await window.hud.call('sessions', {provider, pinned, follow});
    if (!stillCurrent()) return;
    state.local = result;
    renderChat();
  } catch (err) {
    if (!stillCurrent()) return;
    state.local = {available:false,reason:`Local read failed: ${engineMessage(err)}`};
    renderChat();
  } finally {
    state.localFetching = false;
    if (!stillCurrent()) refreshLocal();
  }
}

function tickClocks() {
  if (document.activeElement?.closest('.provider-connection')) return;
  renderResets();
  paintOverview();
}

function showResetAlert(alert) {
  const dialog = $('resetAlert');
  if (!dialog) return;
  if (!alert) { if (dialog.open) dialog.close(); return; }
  $('resetAlertTitle').textContent = alert.events.some(e => e.phase === 'preview') ? 'Alert preview' :
    alert.events.some(e => e.phase === 'reset') ? 'Usage has reset' : 'Reset approaching';
  $('resetAlertMessages').replaceChildren(...alert.events.map(e => el('p', {text:e.message})));
  dialog.classList.toggle('is-pulsing', alert.pulsing);
  if (!dialog.open) { dialog.showModal(); $('btnDismissAlert').focus(); }
}

function showSmsStatus(status) {
  $('smsStatus').textContent = status.last;
  $('btnSms').textContent = status.configured ? 'Twilio SMS settings' : 'Connect Twilio SMS';
  $('smsFeedback').textContent = status.last;
  $('btnTestSms').disabled = !status.configured;
}

// ── boot ──────────────────────────────────────────────────────────────────

async function boot() {
  const { platform, alwaysOnTop, openSmsSetup, openSessions } = await window.hud.windowState();
  document.body.classList.toggle('is-mac', platform === 'darwin');
  $('btnPin').setAttribute('aria-pressed', String(alwaysOnTop));
  positionThumb();

  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener('click', () => selectView(tab.id.replace('tab-', '')));
  }

  $('btnRefresh').addEventListener('click', () => { refreshCycle(true); refreshProviders(true); });
  $('btnPin').addEventListener('click', async () => {
    const next = $('btnPin').getAttribute('aria-pressed') !== 'true';
    const applied = await window.hud.setAlwaysOnTop(next);
    $('btnPin').setAttribute('aria-pressed', String(applied));
  });
  $('chatSelect').addEventListener('change', (e) => {
    // Picking a specific chat is itself an override: it locks Auto onto
    // whichever provider it just found, so the pin means what it says.
    if (state.sessionProvider === 'auto') state.sessionProvider = effectiveProvider();
    state.pinned = e.target.value;
    state.local = {available:false,reason:'Loading selected session…'};
    renderChat();
    saveSessionChoice();
    refreshLocal();
  });
  $('btnUnpin').addEventListener('click', () => {
    state.pinned = null;
    state.local = {available:false,reason:'Finding session…'};
    renderChat();
    saveSessionChoice();
    refreshLocal();
  });
  try {
    const saved = JSON.parse(localStorage.getItem('sessionChoice') || '{}');
    if (['auto','codex','claude','cursor'].includes(saved.provider)) state.sessionProvider = saved.provider;
    if (typeof saved.pinned === 'string') state.pinned = saved.pinned;
    if (['active','latest'].includes(saved.follow)) state.sessionFollow = saved.follow;
  } catch { /* Invalid preference falls back to Auto. */ }
  // Codex is followed through a Windows accessibility helper; Claude Code
  // reports its own open session through the status line on any platform.
  state.activeChatSupported = platform === 'win32';
  if (!state.activeChatSupported && state.sessionProvider === 'codex') state.sessionFollow = 'latest';
  $('sessionFollow').value = state.sessionFollow;
  $('sessionFollow').addEventListener('change', event => {
    state.sessionFollow = event.target.value;
    state.pinned = null;
    state.local = {available:false,reason:'Finding session…'};
    saveSessionChoice();
    renderChat();
    refreshLocal();
  });
  $('sessionProvider').value = state.sessionProvider;
  $('sessionProvider').addEventListener('change', event => {
    state.sessionProvider = event.target.value;
    state.pinned = null;
    state.local = null;
    state.autoDetected = null;
    state.autoActive = false;
    saveSessionChoice();
    renderChat();
    refreshLocal();
  });
  $('btnTestBuzz').addEventListener('click', () => window.hud.previewResetAlert());
  $('btnDismissAlert').addEventListener('click', () => window.hud.dismissResetAlert());
  $('resetAlert').addEventListener('cancel', event => { event.preventDefault(); window.hud.dismissResetAlert(); });
  let alertSettings = await window.hud.call('alert_settings');
  const paintAlertToggle = () => { $('btnOpenConfig').textContent = alertSettings.buzz_enabled ? 'Pause alerts' : 'Enable alerts'; };
  paintAlertToggle();
  $('btnOpenConfig').addEventListener('click', async () => {
    await window.hud.call('set_config', {patch:{buzz_enabled:!alertSettings.buzz_enabled, prewarn_enabled:true, prewarn_minutes:15}});
    alertSettings = await window.hud.call('alert_settings');
    if (!alertSettings.buzz_enabled) await window.hud.dismissResetAlert();
    paintAlertToggle();
  });
  $('btnSms').addEventListener('click', async () => {
    const status = await window.hud.smsStatus();
    $('smsTo').value = status.to || alertSettings.sms_to || '';
    $('smsFrom').value = status.from || '';
    showSmsStatus(status);
    $('smsSettings').showModal();
  });
  $('smsForm').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      showSmsStatus(await window.hud.saveSms({sid:$('smsSid').value, token:$('smsToken').value, from:$('smsFrom').value, to:$('smsTo').value}));
      $('smsSid').value = ''; $('smsToken').value = '';
    } catch (error) { $('smsFeedback').textContent = engineMessage(error); }
  });
  $('btnTestSms').addEventListener('click', async () => {
    $('btnTestSms').disabled = true;
    $('smsFeedback').textContent = 'Sending test SMS…';
    try { showSmsStatus(await window.hud.testSms()); }
    catch (error) { $('smsFeedback').textContent = engineMessage(error); }
    finally { $('btnTestSms').disabled = false; }
  });
  $('btnCloseSms').addEventListener('click', () => $('smsSettings').close());
  $('smsSettings').addEventListener('close', () => { $('smsToken').value = ''; $('smsSid').value = ''; });
  $('btnTwilioConsole').addEventListener('click', () => window.hud.openExternal('https://console.twilio.com/'));
  showSmsStatus(await window.hud.smsStatus());
  showResetAlert(await window.hud.resetAlertStatus());
  window.addEventListener('resize', positionThumb);
  window.addEventListener('focus', () => refreshProviders(false));
  for (const link of Object.values(await window.hud.providerLinkStatus())) handleLinkState(link);

  // Cached report first so the dial is never empty while the network runs.
  try {
    const cached = await window.hud.call('cached_cycle');
    if (cached) {
      state.cycle = cached;
      paintOverview();
    }
  } catch {
    /* no cache yet */
  }

  await refreshLocal();
  refreshProviders(false);
  paintOverview();
  refreshCycle(true);

  setInterval(tickClocks, POLL.clock);
  setInterval(() => refreshProviders(false), POLL.providers);
  setInterval(refreshLocal, POLL.local);
  setInterval(() => refreshCycle(false), POLL.cycle);
  if (openSmsSetup) { selectView('resets'); $('btnSms').click(); }
  else if (openSessions) selectView('chat');
}

let booted = false;
function bootOnce() {
  if (booted) return;
  booted = true;
  setStatus('ok', 'Engine ready');
  boot();
}

window.hud.on('ready', bootOnce);
window.hud.on('resetAlert', showResetAlert);
window.hud.on('smsStatus', showSmsStatus);
window.hud.on('link', handleLinkState);
window.hud.on('connectionsChanged', () => refreshProviders(true));
window.hud.on('down', ({ code }) => {
  state.providers = null;
  state.providersError = `Usage engine stopped (${code}). Reopen TOKENMAXXING.`;
  state.cycleError = state.providersError;
  state.local = {available:false,reason:state.providersError};
  paintOverview();
  renderResets();
  renderChat();
});
window.hud.on('fatal', ({ message }) => {
  setStatus('now', message);
  $('brandSub').textContent = message;
});

// The engine may have reported ready before this script ran, so ask as well as listen.
window.hud.engineStatus().then((status) => {
  if (status.ready) bootOnce();
  else if (status.fatal) {
    setStatus('now', status.fatal.message);
    $('brandSub').textContent = status.fatal.message;
  }
});
