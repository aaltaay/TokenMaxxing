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
  // Limits cards the user has opened, kept across the clock re-renders.
  openWindows: new Set(),
  // Sessions list: whether the followed session is open, which of its
  // priciest requests is broken down, and how many rows are shown.
  sessionOpen: true,
  sessionRequest: null,
  sessionFor: null,
  sessionRows: 6,
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
  chevron: ['m6 9 6 6 6-6'],
  clock: ['M12 6v6l4 2', 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z'],
  plus: ['M12 5v14', 'M5 12h14'],
  refresh: ['M21 12a9 9 0 1 1-2.6-6.4', 'M21 3v6h-6'],
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

function duration(seconds) {
  if (!known(seconds) || seconds < 0) return '—';
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${Math.max(1, minutes)}m`;
}

// ── pace ──────────────────────────────────────────────────────────────────
// How spending compares with the even rate that would land at 100% exactly
// when the provider resets the window. Used percent, reset time and window
// length all come from the provider; the only thing computed here is the
// straight line between them. It answers one question: at this rate, does
// the quota run out before the reset, and what rate would last?

// Below this share of a window (~7h of a week, 12min of a 5-hour window)
// a low reading says nothing about pace yet.
const PACE_MIN_ELAPSED = 0.04;
// used ÷ even-pace used. 1.0 lands exactly on the reset.
const PACE_AHEAD = 1.1;
const PACE_FAST = 1.5;

function pace({ used, elapsed, remaining }) {
  if (!known(used) || !known(elapsed) || !known(remaining)) return null;
  const total = elapsed + remaining;
  if (total <= 0 || remaining < 0) return null;
  const elapsedFrac = Math.min(1, Math.max(0, elapsed / total));
  const expected = elapsedFrac * 100;
  const left = Math.max(0, 100 - used);
  const ratePerHour = elapsed > 0 ? used / (elapsed / 3600) : null;
  const allowedPerHour = remaining > 0 ? left / (remaining / 3600) : 0;
  const multiplier = expected > 0 && elapsed > 0 ? used / expected : null;
  const runsOutIn = known(ratePerHour) && ratePerHour > 0 ? (left / ratePerHour) * 3600 : null;
  const early = known(runsOutIn) ? remaining - runsOutIn : null;
  let status;
  if (used >= 100) status = 'exhausted';
  else if (elapsedFrac < PACE_MIN_ELAPSED && used < 20) status = 'early';
  else if (known(multiplier) && multiplier > PACE_FAST) status = 'fast';
  else if (known(multiplier) && multiplier > PACE_AHEAD) status = 'ahead';
  else if (left > 25 && elapsedFrac > 0.75 && used < expected - 25) status = 'spare';
  else status = 'on-pace';
  return { status, used, left, total, elapsed, remaining, elapsedFrac, expected,
           ratePerHour, allowedPerHour, multiplier, runsOutIn, early };
}

const PACE_SEVERITY = { fast: 'now', ahead: 'soon', exhausted: 'now', spare: 'ok', 'on-pace': 'ok', early: 'off' };
const PACE_BADGE = { fast: 'Too fast', ahead: 'Ahead of pace', exhausted: 'Limit reached', spare: 'Spare quota', 'on-pace': 'On pace', early: 'Just started' };

/** A rate in % per hour, shown per day for windows of a day or more. */
function rateParts(perHour, total) {
  if (!known(perHour)) return null;
  const perDay = total >= 86400;
  const value = perDay ? perHour * 24 : perHour;
  const digits = value >= 10 ? 0 : value >= 1 ? 1 : 2;
  return { value: value.toFixed(digits), unit: perDay ? 'day' : 'hr' };
}

function rateText(perHour, total) {
  const rate = rateParts(perHour, total);
  return rate ? `${rate.value}% / ${rate.unit === 'hr' ? 'hour' : 'day'}` : '—';
}

function paceSentence(p) {
  if (!p) return '';
  const rate = rateText(p.allowedPerHour, p.total);
  switch (p.status) {
    case 'exhausted':
      return `Limit reached. Nothing left until the reset in ${duration(p.remaining)}.`;
    case 'early':
      return `Window just started (${pct(p.used)} used); pace is not meaningful yet.`;
    case 'fast':
    case 'ahead':
      return `${pct(p.used)} used with ${duration(p.remaining)} left, ${p.multiplier.toFixed(1)}× the even pace. ` +
        `At this rate it runs out in ${duration(p.runsOutIn)}, ${duration(p.early)} before the reset. ` +
        `Keep under ${rate} to last.`;
    case 'spare':
      return `${pct(p.left)} is still unused with ${duration(p.remaining)} left; ` +
        `anything under ${rate} expires at the reset.`;
    default:
      return `${pct(p.used)} used against ${pct(p.expected)} even pace. ` +
        `Room for ${rate} until the reset.`;
  }
}

function windowPace(window, now = Date.now() / 1000) {
  if (!known(window?.resets_at) || !known(window?.window_minutes) || !known(window?.used_percent)) return null;
  const total = window.window_minutes * 60;
  const remaining = window.resets_at - now;
  if (remaining <= 0) return null;
  return pace({ used: window.used_percent, elapsed: total - remaining, remaining });
}

function cursorPace(report, used, now = Date.now() / 1000) {
  const start = report?.cycle_start ? Date.parse(report.cycle_start) / 1000 : null;
  const end = report?.cycle_end ? Date.parse(report.cycle_end) / 1000 : null;
  if (!known(start) || !known(end) || end <= start || end <= now || !known(used)) return null;
  return pace({ used, elapsed: now - start, remaining: end - now });
}

/** Every window that is spending faster than its reset allows, worst first. */
function paceAlerts() {
  const alerts = [];
  for (const [id, label] of [['claude', 'Claude'], ['codex', 'Codex']]) {
    const provider = state.providers?.providers?.find(p => p.id === id);
    if (!providerCurrent(provider)) continue;
    for (const window of (provider.windows || []).filter(windowCurrent)) {
      const p = windowPace(window);
      if (p && (p.status === 'fast' || p.status === 'ahead')) alerts.push({ id, name: `${label} ${window.label.toLowerCase()}`, resets_at: window.resets_at, pace: p });
    }
  }
  const cycle = state.cycle;
  if (cycle && !state.cycleError) {
    const p = cursorPace(cycle, cycle.total_pct);
    if (p && (p.status === 'fast' || p.status === 'ahead')) alerts.push({ id: 'cursor', name: 'Cursor billing cycle', resets_at: Date.parse(cycle.cycle_end) / 1000, pace: p });
  }
  return alerts.sort((a, b) => (b.pace.multiplier || 0) - (a.pace.multiplier || 0));
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
  const pacing = paceAlerts();
  for (const alert of pacing) {
    const p = alert.pace;
    const node = el('div', { class: 'alert', vars: { '--c': SEVERITY[PACE_SEVERITY[p.status]] } }, [
      iconWithClass(ICONS.alert, 'alert-icon'),
      el('div', { class: 'alert-title', text: p.status === 'fast'
        ? `${alert.name}: slow down, ${p.multiplier.toFixed(1)}× faster than the reset allows`
        : `${alert.name}: ahead of pace, ${p.multiplier.toFixed(1)}× the even rate` }),
      el('div', { class: 'alert-detail', text: `${paceSentence(p)} ${resetText(alert.resets_at)}.` }),
      el('div', { class: 'alert-actions' }, [
        el('button', { class: 'chip', type: 'button', text: 'See every window', onclick: () => selectView('resets') }),
      ]),
    ]);
    host.append(node);
  }
  if (!state.cycle) return;

  if (!items.length && !pacing.length) {
    host.append(el('div', { class: 'all-clear' }, [
      iconWithClass(ICONS.check, '', 17),
      el('span', { text: 'No high usage reported, and every window with a reset time is on pace.' }),
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

// Recent sessions as a Material 3 list. The one being followed opens into
// its cost story: a bar per request, the priciest requests, and for each of
// those where the money went and why. Every figure is read from the session
// log and priced by the engine; the explanations only restate what the
// log records (token classes, the gap before a request, a model change).

const SESSION_ROWS = 6;
const TOP_SHOWN = 5;
const CHART_BARS = 240;

function agoText(epoch) {
  if (!known(epoch)) return '—';
  const secs = Math.max(0, Date.now() / 1000 - epoch);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  if (secs < 2 * 86400) return 'yesterday';
  return new Date(epoch * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

/** Per-request amounts: cents below a dollar, dollars above. */
function centsText(c) {
  if (!known(c)) return '—';
  if (c >= 100) return money(c);
  return `${c < 1 ? c.toFixed(2) : c < 10 ? c.toFixed(1) : Math.round(c)}¢`;
}

/** "$25" or "$0.50" per million tokens, from what a part actually cost. */
function perMillion(cents, tokens) {
  const rate = (cents / tokens) * 1e4;
  return `$${rate < 1 ? rate.toFixed(2) : +rate.toFixed(2)}/M`;
}

function splitName(name) {
  const at = String(name || '').lastIndexOf(' · ');
  return at > 0 ? [name.slice(0, at), name.slice(at + 3)] : [name || 'Untitled session', null];
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** What the per-request series says about a session as a whole. */
function sessionStats(cost) {
  const series = cost?.series || [];
  const values = series.map(r => r[1]);
  const rebuilds = series.filter(r => r[2]);
  const early = series.length >= 20 && cost.series_start === 0 ? values.slice(0, 10).reduce((a, b) => a + b, 0) / 10 : null;
  const last = cost?.last_request_cents;
  return {
    typical: median(values),
    early,
    last,
    rebuilds: rebuilds.length,
    rebuildCents: rebuilds.reduce((a, r) => a + r[1], 0),
    firstContext: cost?.series_start === 0 && series.length ? series[0][3] : null,
    climbing: known(early) && known(last) && early > 0 && last >= 1.5 * early,
  };
}

/** Why one request cost what it did, from its own token classes. */
function explainRequest(d, stats) {
  const parts = [...(d.parts || [])].sort((a, b) => b[2] - a[2]);
  if (!parts.length) return { tag: 'No priced tokens', why: [] };
  const [label, tokens, cents] = parts[0];
  const why = [];
  let tag;
  if (label.startsWith('Cache write')) {
    const hour = label.includes('1-hour');
    const ttl = hour ? 3600 : 300;
    if (d.index === 0) {
      tag = 'First request';
      why.push(`First request of the session: ${compact(tokens)} tokens of starting context were written to the cache.`);
    } else if (d.previous_model && d.model && d.previous_model !== d.model) {
      tag = 'Model switch';
      why.push(`The model changed from ${d.previous_model} to ${d.model}. Each model keeps its own cache, so ${compact(tokens)} tokens of context were cached again.`);
    } else if (known(d.gap) && d.gap > ttl) {
      tag = `Cache expired · ${duration(d.gap)} idle`;
      why.push(`Nothing was sent for ${duration(d.gap)} before this, longer than the ${hour ? '1-hour' : '5-minute'} cache lasts, so ${compact(tokens)} tokens of context were written to the cache again.`);
    } else if (d.rebuild) {
      tag = 'Cache rebuild';
      why.push(`${compact(tokens)} tokens were written to the cache again${known(d.gap) ? ` ${duration(d.gap)} after the previous request` : ''}. The cached copy no longer matched the start of the context, which happens after a compaction or when earlier context changes.`);
    } else {
      tag = `New context · ${compact(tokens)}`;
      why.push(`${compact(tokens)} new tokens entered the cache: the previous reply, tool results or files added to the context, at ${perMillion(cents, tokens)}.`);
    }
  } else if (label === 'Output') {
    tag = `Long output · ${compact(tokens)}`;
    why.push(`A long output: ${compact(tokens)} tokens written (a long reply, code or file edits). Output is the priciest token class, at ${perMillion(cents, tokens)}.`);
  } else if (label === 'Cache read' || label === 'Cached input') {
    const growth = known(stats?.firstContext) && stats.firstContext > 0 ? d.context / stats.firstContext : null;
    tag = `Big context · ${compact(d.context)}`;
    why.push(`A big context: ${compact(tokens)} tokens re-read from the cache${growth >= 2 ? `, ${growth.toFixed(0)}× the size at the start of the session` : ''}. Cheap per token at ${perMillion(cents, tokens)}, but paid again on every request.`);
  } else {
    tag = `Uncached input · ${compact(tokens)}`;
    why.push(`${compact(tokens)} input tokens were sent without a cache hit, at the full input rate of ${perMillion(cents, tokens)}.`);
  }
  if (d.long_context) why.push('Over 272k input tokens, so the whole request was billed at the long-context rate (2× input, 1.5× output).');
  return { tag, why, dominant: label, share: d.cents > 0 ? cents / d.cents : 0 };
}

/** The notes a cost figure needs to be read correctly. */
function costNotes(cost) {
  if (!cost) return ['Cost unavailable: this log has no recorded requests with supported model pricing.'];
  if (cost.reported) return [cost.basis, (cost.series || []).length ? 'Per-request amounts are estimates at API rates from the session log.' : null].filter(Boolean);
  const notes = [];
  if (!known(cost.cents)) notes.push('Cost unavailable: no recorded requests with supported model pricing and complete token counts.');
  else notes.push(cost.partial
    ? `Partial estimate · ${cost.priced_requests} priced requests. Some usage could not be priced or read.`
    : `${cost.priced_requests} recorded requests · API-equivalent estimate.`);
  if (cost.basis) notes.push(cost.basis);
  if (cost.pricing_date) notes.push(`Pricing checked ${cost.pricing_date} · excludes separate subagent logs.`);
  return notes;
}

const PART_TONE = {
  'Uncached input': 'in', 'Cache read': 'read', 'Cached input': 'read',
  'Cache write (5-minute)': 'write', 'Cache write (1-hour)': 'write', Output: 'out',
};

function sessionRow({ provider, row, selected, open, live, chat }) {
  const [title, shortId] = splitName(row.name);
  const cursorMatch = provider === 'cursor' ? (state.cycle?.conversations || []).find(c => c.id === row.id) : null;
  const cost = selected && chat?.cost ? { cents: chat.cost.cents, requests: chat.cost.priced_requests }
    : cursorMatch ? { cents: cursorMatch.cents, events: cursorMatch.n }
    : { cents: row.cents, requests: row.requests };
  const model = row.model || (selected ? chat?.model : null);
  const when = live
    ? el('span', { class: 'sess-live' }, [el('i', {}), 'Live'])
    : el('span', { text: agoText(selected && chat?.updated_at ? chat.updated_at : row.updated_at) });
  return el('button', {
    type: 'button',
    class: `sess-row${open ? ' is-open' : ''}${selected ? ' is-selected' : ''}`,
    'data-key': `row:${row.id}`,
    'aria-expanded': selected ? String(open) : null,
    vars: { '--c': PROVIDER_HUE[provider] },
    onclick: () => (selected ? toggleSessionOpen() : openSession(row.id)),
  }, [
    el('span', { class: 'prov-mark' }, [icon(PROVIDER_GLYPH[provider], 17)]),
    el('span', { class: 'sess-row-text' }, [
      el('span', { class: 'sess-row-name', text: title }),
      el('span', { class: 'sess-row-sub' }, [when, model && el('span', { text: model }), shortId && el('code', { text: shortId })]),
    ]),
    known(cost.cents)
      ? el('span', { class: 'sess-row-cost' }, [money(cost.cents), el('small', {
        text: cost.events !== undefined ? `${compact(cost.events)} events` : `${compact(cost.requests)} requests`,
      })])
      : el('span'),
    iconWithClass(ICONS.chevron, 'sess-chev', 16),
  ]);
}

/** One bar per request (or per few, on long sessions); the requests that
    ran past the scale carry their price above them. */
function sessionChart(cost, hue, top) {
  const series = cost.series || [];
  const n = series.length;
  const per = Math.max(1, Math.ceil(n / CHART_BARS));
  const bars = [];
  for (let i = 0; i < n; i += per) {
    const group = series.slice(i, i + per);
    const rebuild = group.some(r => r[2]);
    const value = rebuild || per === 1 ? Math.max(...group.map(r => r[1])) : group.reduce((a, r) => a + r[1], 0) / group.length;
    bars.push({ first: cost.series_start + i, count: group.length, value, rebuild, at: group[0][0], context: group[group.length - 1][3] });
  }
  const width = Math.max(bars.length, 24);
  const ordinary = bars.filter(b => !b.rebuild).map(b => b.value).sort((a, b) => a - b);
  const p95 = ordinary.length ? ordinary[Math.min(ordinary.length - 1, Math.floor(ordinary.length * 0.95))] : Math.max(...bars.map(b => b.value));
  const cap = Math.max(p95 * 1.25, 0.01);
  const selected = state.sessionRequest;
  const topIndexes = new Set(top.map(t => t.index));

  const svg = svgEl('svg', { class: 'sc-svg', viewBox: `0 0 ${width} 100`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  bars.forEach((bar, i) => {
    const h = Math.max(1.5, Math.min(1, bar.value / cap) * 100);
    const holds = (index) => index >= bar.first && index < bar.first + bar.count;
    const cls = ['sc-bar', bar.rebuild && 'is-rebuild', [...topIndexes].some(holds) && 'is-top', known(selected) && holds(selected) && 'is-picked'].filter(Boolean).join(' ');
    svg.append(svgEl('rect', { class: cls, x: i + 0.14, y: 100 - h, width: 0.72, height: h }));
  });
  const plot = el('div', { class: 'sc-plot', vars: { '--c': hue } }, [svg]);
  for (const level of [0.5, 1]) {
    plot.append(el('i', { class: 'sc-grid', vars: { '--y': 100 - level * 100 } }));
    plot.append(el('span', { class: 'sc-grid-label', vars: { '--y': 100 - level * 100 }, text: centsText(cap * level) }));
  }
  // Price tags on the bars that ran off the scale, dearest first, skipping
  // any that would sit on top of one already placed.
  const clipped = bars.map((bar, i) => ({ bar, x: ((i + 0.5) / width) * 100 }))
    .filter(({ bar }) => bar.value > cap).sort((a, b) => b.bar.value - a.bar.value);
  const tagged = [];
  for (const spike of clipped) if (tagged.length < 5 && tagged.every(t => Math.abs(t.x - spike.x) >= 10)) tagged.push(spike);
  for (const { bar, x } of tagged) {
    plot.append(el('span', { class: `sc-spike${bar.rebuild ? ' is-rebuild' : ''}`, vars: { '--x': x }, text: centsText(bar.value) }));
  }
  // Every bar answers a hover; the priciest ones also open their breakdown.
  const barAt = (event) => {
    const box = plot.getBoundingClientRect();
    const i = Math.floor(((event.clientX - box.left) / box.width) * width);
    return bars[i] || null;
  };
  plot.addEventListener('mouseleave', () => tooltip.hide());
  plot.addEventListener('mousemove', (event) => {
    const bar = barAt(event);
    if (!bar) return tooltip.hide();
    const request = top.find(t => t.index >= bar.first && t.index < bar.first + bar.count);
    tooltip.show([
      el('b', { text: `${centsText(bar.value)}${bar.count > 1 ? ` · ${bar.count} requests` : ''}` }),
      el('div', { class: 'muted', text: `${known(bar.at) ? new Date(bar.at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : 'Time not recorded'} · ${compact(bar.context)} context${bar.rebuild ? ' · cache rebuild' : ''}` }),
      request && el('div', { class: 'muted', text: 'Click for the breakdown' }),
    ].filter(Boolean), event);
    plot.classList.toggle('is-pointing', Boolean(request));
  });
  plot.addEventListener('click', (event) => {
    const bar = barAt(event);
    const request = bar && top.find(t => t.index >= bar.first && t.index < bar.first + bar.count);
    if (request) pickRequest(request.index);
  });

  const firstAt = series[0]?.[0];
  const lastAt = series[n - 1]?.[0];
  const clock = (epoch) => known(epoch) ? momentText(epoch, Math.max(0, Date.now() / 1000 - epoch) + 1) : '—';
  const lastBarEdge = (bars.length / width) * 100;
  return el('div', { class: 'sc' }, [
    plot,
    el('div', { class: 'sc-axis', vars: { '--end': lastBarEdge } }, [
      el('span', { text: `${cost.series_start ? `Request ${compact(cost.series_start + 1)}` : 'First request'} · ${clock(firstAt)}` }),
      el('span', { class: 'sc-axis-end', text: known(lastAt) && Date.now() / 1000 - lastAt < 120 ? 'now' : clock(lastAt) }),
    ]),
    el('div', { class: 'proj-legend' }, [
      el('span', {}, [el('i', { class: 'sw sw-bar' }), per > 1 ? `Cost per request (each bar ${per} requests)` : 'Cost per request']),
      bars.some(b => b.rebuild) && el('span', {}, [el('i', { class: 'sw sw-bar is-rebuild' }), 'Cache rebuild']),
      top.length > 0 && el('span', {}, [el('i', { class: 'sw sw-bar is-top' }), 'Priciest, click for why']),
    ]),
  ]);
}

function requestBreakdown(d, stats) {
  const story = explainRequest(d, stats);
  const parts = [...d.parts].sort((a, b) => b[2] - a[2]);
  return el('div', { class: 'req-detail' }, [
    el('div', { class: 'req-mix' }, parts.map(([label, , cents]) =>
      el('i', { class: `tone-${PART_TONE[label] || 'in'}`, vars: { '--w': Math.max(cents, d.cents * 0.004) } }))),
    el('div', { class: 'req-parts' }, parts.map(([label, tokens, cents]) => el('div', { class: `req-part tone-${PART_TONE[label] || 'in'}` }, [
      el('i', {}),
      el('span', { class: 'req-part-name', text: label }),
      el('span', { class: 'req-part-tokens', text: `${compact(tokens)} tok × ${perMillion(cents, tokens)}` }),
      el('b', { text: centsText(cents) }),
      el('span', { class: 'req-part-share', text: d.cents > 0 ? pct((100 * cents) / d.cents) : '—' }),
    ]))),
    el('div', { class: 'req-why' }, [
      iconWithClass(ICONS.spark, '', 16),
      el('div', {}, story.why.map(text => el('p', { text }))),
    ]),
    el('div', { class: 'req-facts' }, [
      el('span', { text: `Request ${compact(d.index + 1)}` }),
      known(d.at) && el('span', { text: new Date(d.at * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) }),
      el('span', { text: `${compact(d.context)} context` }),
      known(d.gap) && el('span', { text: `${duration(d.gap)} after the previous request` }),
      d.model && el('span', { text: d.model }),
    ]),
  ]);
}

function topRequests(top, stats) {
  const list = el('div', { class: 'req-list' }, [
    el('div', { class: 'req-list-head' }, [
      el('span', { class: 'kicker', text: 'Most expensive requests' }),
      known(stats.typical) && el('span', { class: 'req-list-meta', text: `typical request ${centsText(stats.typical)}` }),
    ]),
  ]);
  top.slice(0, TOP_SHOWN).forEach((d, rank) => {
    const open = state.sessionRequest === d.index;
    const story = explainRequest(d, stats);
    const times = known(stats.typical) && stats.typical > 0 ? d.cents / stats.typical : null;
    list.append(el('button', {
      type: 'button',
      class: `req-row${open ? ' is-open' : ''}`,
      'data-key': `req:${d.index}`,
      'aria-expanded': String(open),
      onclick: () => pickRequest(d.index),
    }, [
      el('span', { class: 'req-rank', text: String(rank + 1) }),
      el('span', { class: 'req-cost', text: centsText(d.cents) }),
      // Always present so the columns line up; empty when unremarkable.
      el('span', { class: `req-times${times >= 5 ? ' is-hot' : ''}`, text: known(times) && times >= 1.5 ? `${times >= 10 ? times.toFixed(0) : times.toFixed(1)}× typical` : '' }),
      el('span', { class: 'req-tag', text: story.tag }),
      el('span', { class: 'req-when', text: known(d.at) ? new Date(d.at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '' }),
      iconWithClass(ICONS.chevron, 'sess-chev', 15),
    ]));
    if (open) list.append(requestBreakdown(d, stats));
  });
  return list;
}

function lastTurn(chat) {
  const m = chat.metrics || {};
  const output = m['Last request output'] ?? m['Output tokens this session'];
  const read = m['Last request cache read'] ?? m['Last request cached input (included in input)'];
  const total = known(m['Last request cache read'])
    ? ['Last request uncached input', 'Last request cache read', 'Last request cache write'].reduce((a, k) => a + (m[k] ?? NaN), 0)
    : m['Last request input'];
  const cached = known(read) && known(total) && total > 0 ? (100 * read) / total : null;
  const bits = [
    known(chat.used) && `${compact(chat.used)} in`,
    known(output) && `${compact(output)} out${m['Last request output'] === undefined ? ' this session' : ''}`,
    known(cached) && `${pct(cached, cached >= 99 ? 1 : 0)} from cache`,
  ].filter(Boolean);
  const node = el('div', { class: 'sess-last' }, [
    el('span', { class: 'kicker', text: 'Last turn' }),
    el('span', { text: bits.join(' · ') || 'No token counts recorded' }),
  ]);
  if (known(chat.limit) && chat.limit > 0 && known(chat.used)) {
    const share = Math.min(100, (100 * chat.used) / chat.limit);
    node.append(el('div', { class: 'sess-context' }, [
      el('div', { class: 'meter is-plain', vars: { '--c': share >= 90 ? 'var(--now)' : 'var(--c)', '--w': share } }, [el('i', { class: 'sess-context-fill' })]),
      el('span', { text: `${pct(share)} of ${compact(chat.limit)} context` }),
    ]));
  }
  return node;
}

function sessionDetail(provider, chat) {
  const cost = chat.cost;
  const stats = sessionStats(cost);
  const top = cost?.top_requests || [];
  const hue = PROVIDER_HUE[provider];
  const detail = el('div', { class: 'sess-detail', vars: { '--c': hue } });
  if (known(cost?.cents) && (cost.series || []).length) {
    const headline = stats.climbing ? `${centsText(stats.last)} a turn now, up from ${centsText(stats.early)}`
      : known(stats.typical) ? `About ${centsText(stats.typical)} a turn` : 'Priced requests';
    const support = [
      stats.climbing && 'Every request re-reads the whole context, so each turn costs more as the chat grows.',
      stats.rebuilds ? `**${stats.rebuilds}** cache rebuild${stats.rebuilds === 1 ? '' : 's'}, where the context was written to the cache again, cost **${money(stats.rebuildCents)}**.`
        : 'No cache rebuilds: the context stayed cached from one request to the next.',
    ].filter(Boolean).join(' ');
    detail.append(
      el('div', { class: 'proj-headline', text: headline }),
      rich('div', 'proj-support', support),
      sessionChart(cost, hue, top.slice(0, TOP_SHOWN)),
      el('div', { class: 'wc-tiles is-4' }, [
        top[0] ? el('button', { type: 'button', class: 'wc-tile is-action is-hot', 'data-key': 'priciest', onclick: () => pickRequest(top[0].index) }, [
          el('b', { text: centsText(top[0].cents) }), el('span', { text: 'priciest request' })]) : null,
        el('div', { class: 'wc-tile' }, [el('b', { text: centsText(stats.typical) }), el('span', { text: 'typical request' })]),
        el('div', { class: `wc-tile${stats.rebuilds ? ' is-hot' : ' is-good'}` }, [
          el('b', {}, [String(stats.rebuilds), stats.rebuilds ? el('small', { text: ` · ${money(stats.rebuildCents)}` }) : null]),
          el('span', { text: 'cache rebuilds' })]),
        el('div', { class: 'wc-tile' }, [el('b', { text: centsText(stats.last) }), el('span', { text: 'last request' })]),
      ]),
      top.length ? topRequests(top, stats) : null,
    );
  } else if (known(cost?.cents)) {
    detail.append(el('div', { class: 'proj-headline', text: `${money(cost.cents)} this session` }));
  } else {
    detail.append(el('p', { class: 'empty', text: 'No priced requests in this log yet.' }));
  }
  detail.append(lastTurn(chat));
  const usageTime = chat.usage_at ? Date.parse(chat.usage_at) / 1000 : null;
  detail.append(el('div', { class: 'sess-notes' }, [
    chat.measurement,
    `${chat.source} · session updated ${relativeTime(chat.updated_at)}${known(usageTime) ? ` · usage recorded ${relativeTime(usageTime)}` : ''}`,
    known(chat.limit) ? null : 'Context limit was not recorded, so no context percentage is shown.',
    ...costNotes(cost),
  ].filter(Boolean).map(text => el('p', { text }))));
  return detail;
}

function cursorDetail(chat) {
  const detail = el('div', { class: 'sess-detail', vars: { '--c': PROVIDER_HUE.cursor } });
  const usedPct = known(chat.used) && known(chat.limit) && chat.limit > 0 ? (100 * chat.used) / chat.limit : chat.pct;
  // A context window is not a billing pool: half full is unremarkable.
  const severity = usedPct >= 90 ? 'now' : usedPct >= 70 ? 'soon' : 'ok';
  detail.append(
    el('div', { class: 'proj-headline', text: known(usedPct) ? `${pct(usedPct, 1)} of context used` : 'Context size unavailable' }),
    el('div', { class: 'proj-support', text: `${compact(chat.used)} of ${compact(chat.limit)} tokens · ${pct(chat.tax_pct)} is non-conversation context` }),
    el('div', { class: 'meter sess-cursor-meter', vars: { '--c': severity === 'ok' ? PROVIDER_HUE.cursor : SEVERITY[severity], '--w': known(usedPct) ? Math.min(100, usedPct) : 0 } }, [el('i', { class: 'sess-context-fill' })]),
  );
  const rows = chat.cat_rows || [];
  const cats = el('div', { class: 'cat-rows' });
  if (!rows.length) cats.append(el('p', { class: 'empty', text: 'No category breakdown in this snapshot.' }));
  let group = null;
  for (const row of rows) {
    const next = row.overhead ? 'tax' : 'conversation';
    if (next !== group) {
      group = next;
      cats.append(el('div', { class: 'kicker cat-group', text: row.overhead ? 'Non-conversation context (Cursor estimate)' : 'Conversation (Cursor estimate)' }));
    }
    cats.append(el('div', { class: 'cat' }, [
      el('span', { class: 'cat-name', text: row.label }),
      el('span', { class: 'cat-val', text: `${compact(row.tokens)} · ${pct(row.share)}` }),
      el('div', { class: 'meter is-plain cat-bar', vars: { '--c': row.overhead ? 'var(--ink-4)' : PROVIDER_HUE.cursor, '--w': known(row.share) ? Math.min(100, row.share) : 0 } }, [el('i', { class: 'sess-context-fill' })]),
    ]));
  }
  detail.append(cats);
  const match = (state.cycle?.conversations || []).find(c => c.id === chat.id);
  const notes = [`Session updated ${relativeTime(chat.updated_at)} · ${chat.measurement}`];
  if (!state.cycle?.events_complete || !match) {
    notes.push(!state.cycle?.events_complete ? 'Chat usage value unavailable: complete event history has not been loaded.' : 'This chat was not found in the loaded usage results.');
  } else {
    detail.append(el('div', { class: 'wc-tiles' }, [
      el('div', { class: 'wc-tile' }, [el('b', { text: money(match.cents) }), el('span', { text: 'usage value' })]),
      el('div', { class: 'wc-tile' }, [el('b', { text: compact(match.n) }), el('span', { text: 'recorded events' })]),
      el('div', { class: 'wc-tile' }, [el('b', { text: compact(match.out) }), el('span', { text: `out · ${compact(match.cr)} cache read` })]),
    ]));
    notes.push('Event-reported usage value from Cursor usage history; this is not a cash charge.');
  }
  detail.append(el('div', { class: 'sess-notes' }, notes.map(text => el('p', { text }))));
  return detail;
}

function openSession(id) {
  // Picking a specific chat is itself an override: it locks Auto onto
  // whichever provider it just found, so the pin means what it says.
  if (state.sessionProvider === 'auto') state.sessionProvider = effectiveProvider();
  state.pinned = id;
  state.sessionOpen = true;
  state.sessionRequest = null;
  if (state.local) state.local = { ...state.local, chat: null, reason: 'Loading selected session…' };
  saveSessionChoice();
  renderChat();
  refreshLocal();
}

function toggleSessionOpen() {
  state.sessionOpen = !state.sessionOpen;
  renderChat();
}

function pickRequest(index) {
  state.sessionRequest = state.sessionRequest === index ? null : index;
  renderChat();
}

function renderSessionControls(provider, followsOpen, isAuto) {
  for (const chip of document.querySelectorAll('#sessionProviders [data-provider]')) {
    chip.setAttribute('aria-checked', String(chip.dataset.provider === state.sessionProvider));
    if (chip.dataset.provider === 'auto') chip.textContent = isAuto && state.autoDetected ? `Auto · ${provider === 'claude' ? 'Claude Code' : 'Codex'}` : 'Auto';
  }
  const followProvider = provider === 'codex' ? state.activeChatSupported : provider === 'claude';
  $('sessionFollowRow').hidden = isAuto || !followProvider || provider === 'cursor';
  for (const chip of document.querySelectorAll('#sessionFollowRow [data-follow]')) {
    chip.setAttribute('aria-checked', String(chip.dataset.follow === state.sessionFollow));
  }
  $('sessionFollowOpen').textContent = provider === 'claude' ? 'Open session' : 'Open chat';
  // A Claude session found through its log rather than the status line is
  // said so: it is the chat being written to, not a chat seen on screen.
  const local = state.local;
  const claudeOpen = local?.follow_source === 'recent log' ? 'Claude Code session active in the last 15 minutes' : 'open Claude Code session';
  $('sessionMode').textContent = state.pinned ? 'Pinned · stays on the session you picked.'
    : isAuto ? (state.autoActive ? `Following the ${provider === 'claude' ? claudeOpen : 'open Codex chat'}.`
                                  : 'No open Codex or Claude Code chat detected right now.')
    : followsOpen ? `Following the ${provider === 'claude' ? claudeOpen : 'open Codex chat'} · switches when you do.`
    : 'Latest activity · the most recently updated session, including background tasks.';
  $('sessionModeDot').classList.toggle('is-on', !state.pinned && (isAuto ? state.autoActive : followsOpen) && Boolean(local?.chat));
  const unpin = $('btnUnpin');
  unpin.hidden = !state.pinned;
  unpin.textContent = followsOpen || isAuto ? (provider === 'claude' ? 'Follow open session' : 'Follow open chat') : 'Follow latest activity';
}

function renderChat() {
  const local = state.local;
  const provider = effectiveProvider();
  const isAuto = state.sessionProvider === 'auto';
  const followProvider = provider === 'codex' ? state.activeChatSupported : provider === 'claude';
  const followsOpen = isAuto ? state.autoActive : (followProvider && state.sessionFollow === 'active');
  renderSessionControls(provider, followsOpen, isAuto);

  const list = $('sessionList');
  const focused = document.activeElement?.closest?.('#sessionList [data-key]')?.dataset.key;
  list.replaceChildren();
  if (!local?.available) {
    const why = local?.reason === 'Cursor DB not found'
      ? 'Cursor’s local database was not found. Sign in to Cursor and open a chat.'
      : local?.reason || (local ? 'Local chat data is unavailable.' : 'Loading sessions…');
    list.append(el('p', { class: 'empty', text: why }));
    return;
  }
  const chat = local.chat && !local.chat.error ? local.chat : null;
  const selectedId = chat?.id ?? state.pinned ?? null;
  if (state.sessionFor !== selectedId) {
    state.sessionFor = selectedId;
    state.sessionRequest = null;
  }
  const chats = local.chats || [];
  const limit = state.sessionRows || SESSION_ROWS;
  const shown = chats.slice(0, limit);
  if (selectedId && !shown.some(c => c.id === selectedId)) {
    const picked = chats.find(c => c.id === selectedId) || (chat ? { id: chat.id, name: chat.name, updated_at: chat.updated_at } : null);
    if (picked) shown.unshift(picked);
  }
  if (!selectedId || (!chat && !state.pinned)) {
    list.append(el('p', { class: 'sess-note', text: local.chat?.error || local.reason || 'No session selected. Pick one below.' }));
  }
  const live = !state.pinned && (isAuto ? state.autoActive : followsOpen);
  for (const row of shown) {
    const selected = row.id === selectedId;
    const open = selected && state.sessionOpen !== false;
    list.append(sessionRow({ provider, row, selected, open, live: selected && live && Boolean(chat), chat }));
    if (!open) continue;
    if (!chat) list.append(el('div', { class: 'sess-detail' }, [el('p', { class: 'empty', text: local.chat?.error || local.reason || 'Loading selected session…' })]));
    else list.append(provider === 'cursor' ? cursorDetail(chat) : sessionDetail(provider, chat));
  }
  if (!chats.length) list.append(el('p', { class: 'empty', text: 'No recent sessions found for this provider.' }));
  if (chats.length > limit) {
    list.append(el('button', {
      type: 'button', class: 'sess-more', 'data-key': 'more',
      text: `Show more sessions (${chats.length - limit} older)`,
      onclick: () => { state.sessionRows = limit + 12; renderChat(); },
    }));
  }
  if (focused) list.querySelector(`[data-key="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
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

// Each window is a Material 3 card after Android's Data usage meter: the
// reading, a bar with the even-pace mark, three stat tiles. Opening a card
// adds the projection from Android's Battery screen — when the current rate
// runs dry, drawn against the even pace to the reset. The one line drawn
// from the past is the straight average since the window opened: providers
// report a single reading, not a history.

const PACE_ICON = {
  fast: ['M13 2 4 14h7l-1 8 9-12h-7z'],
  ahead: ['m3 17 6-6 4 4 8-8', 'M14 7h7v7'],
  'on-pace': ICONS.check,
  spare: ICONS.spark,
  early: ICONS.clock,
  exhausted: ICONS.alert,
};

// Generic shapes, not logos: a burst, a prompt, a pointer.
const PROVIDER_GLYPH = {
  claude: ['M12 3v18', 'M3 12h18', 'm5.6 5.6 12.8 12.8', 'm18.4 5.6-12.8 12.8'],
  codex: ['m5 17 6-5-6-5', 'M13 19h7'],
  cursor: ['M5 3l6.5 17 2.4-7.1L21 10.5z'],
};

/** A reset or run-out moment, as short as the window allows. */
function momentText(epoch, span) {
  if (!known(epoch)) return '—';
  const at = new Date(epoch * 1000);
  if (span >= 14 * 86400) return at.toLocaleDateString([], { month: 'short', day: 'numeric' });
  const time = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (at.toDateString() === new Date().toDateString()) return time;
  const ahead = epoch - Date.now() / 1000;
  if (ahead > 0 && ahead < 6 * 86400) return `${at.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  return `${at.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

/** Where a window opened, dated for anything longer than a day so a weekly
    start never reads like its own reset. */
function openedText(epoch, span) {
  if (span >= 2 * 86400) return new Date(epoch * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  return momentText(epoch, span);
}

const times = (m) => !known(m) ? '—' : m >= 10 ? m.toFixed(0) : m < 1 ? m.toFixed(2) : m.toFixed(1);
const reading = (n) => n.toFixed(Math.abs(n - Math.round(n)) < 0.05 ? 0 : 1);

/** Text with **bold** runs, built as nodes rather than HTML. */
function rich(tag, className, text) {
  return el(tag, { class: className }, text.split(/\*\*(.+?)\*\*/).map((part, i) => (i % 2 ? el('b', { text: part }) : part)));
}

/** Where the average rate so far lands: the moment it runs dry (as a share
    of the window), or the reading it reaches at the reset. */
function projection(p) {
  if (p.status === 'early') return { runsOut: false, outAt: null, end: null };
  if (p.status === 'exhausted') return { runsOut: true, outAt: p.elapsedFrac, end: 100 };
  if (known(p.runsOutIn) && p.runsOutIn < p.remaining) {
    return { runsOut: true, outAt: (p.elapsed + p.runsOutIn) / p.total, end: 100 };
  }
  return { runsOut: false, outAt: null, end: p.elapsedFrac > 0 ? Math.min(100, p.used / p.elapsedFrac) : p.used };
}

function rateShort(perHour, total) {
  const r = rateParts(perHour, total);
  return r ? `${r.value}%/${r.unit}` : '—';
}

/** The one-line answer on the card and the headline inside it. */
function paceStory(p, resetAt) {
  const reset = momentText(resetAt, p.total);
  const proj = projection(p);
  const rate = rateShort(p.allowedPerHour, p.total);
  if (p.status === 'exhausted') return {
    foot: `Out of quota until **${reset}**`,
    headline: ['Back at ', reset, 'now'],
    support: `Limit reached. Nothing left for **${duration(p.remaining)}**.`,
  };
  if (p.status === 'early') return {
    foot: 'Too early to judge pace',
    headline: ['Just getting started'],
    support: `**${pct(p.used)}** used so far; pace settles once more of the window has passed. Resets ${reset}, in **${duration(p.remaining)}**.`,
  };
  if (proj.runsOut) {
    const outEpoch = Date.now() / 1000 + p.runsOutIn;
    const today = new Date(outEpoch * 1000).toDateString() === new Date().toDateString() && p.total < 14 * 86400;
    return {
      foot: `Runs dry **${duration(p.early)}** before the reset`,
      headline: [today ? 'Runs out at ' : 'Runs out ', momentText(outEpoch, p.total), p.status === 'on-pace' ? null : 'now'],
      support: `In **${duration(p.runsOutIn)}**, **${duration(p.early)}** before the ${reset} reset. Stay under **${rate}** to make it.`,
    };
  }
  const spare = 100 - proj.end;
  if (p.status === 'spare') return {
    foot: `**${pct(spare)}** will go unused at this rate`,
    headline: ['', pct(spare), 'ok', ' will go unused'],
    support: `This rate ends near **${pct(proj.end)}** at the ${reset} reset. Room for **${rate}**.`,
  };
  return {
    foot: spare >= 1 ? `Lasts to the reset with **~${pct(spare)}** to spare` : 'Lasts right up to the reset',
    headline: ['Lasts until ', 'the reset', 'ok'],
    support: `This rate ends near **${pct(proj.end)}** at ${reset}. Room for **${rate}**.`,
  };
}

function paceTiles(p) {
  const proj = projection(p);
  const hot = p.status === 'fast' || p.status === 'ahead';
  const rate = rateParts(p.allowedPerHour, p.total);
  const tile = (value, unit, label, tone) => el('div', { class: `wc-tile${tone ? ` is-${tone}` : ''}` }, [
    el('b', {}, [value, unit && el('small', { text: unit })]),
    el('span', { text: label }),
  ]);
  const pace = p.status === 'early'
    ? tile('—', null, 'even pace')
    : tile(times(p.multiplier), '×', 'even pace', hot ? 'hot' : known(p.multiplier) && p.multiplier <= 1 ? 'good' : null);
  if (p.status === 'exhausted') {
    const avg = rateParts(p.ratePerHour, p.total);
    return [pace, tile('0', '%', 'left'), tile(avg ? avg.value : '—', avg && `%/${avg.unit}`, 'average rate')];
  }
  const second = p.status === 'early' ? tile('—', null, 'too early to project')
    : proj.runsOut ? tile(duration(p.runsOutIn), null, 'until empty', hot ? 'hot' : null)
    : tile(pct(proj.end).replace('%', ''), '%', 'at the reset');
  return [pace, second, tile(rate ? rate.value : '—', rate && `%/${rate.unit}`, proj.runsOut ? 'rate that lasts' : 'room to last')];
}

function statusChip(p) {
  return el('span', { class: 'status-chip', vars: { '--c': SEVERITY[PACE_SEVERITY[p.status]] } }, [
    iconWithClass(PACE_ICON[p.status], '', 14),
    PACE_BADGE[p.status],
  ]);
}

/** Material 3 linear progress, with the even-pace mark and anything past
    it striped: ahead of schedule reads before a word does. */
function paceBar(used, p) {
  const u = Math.min(100, Math.max(0, used));
  const even = p ? Math.min(100, Math.max(0, p.expected)) : null;
  const over = known(even) && u > even + 0.5;
  return el('div', {
    class: `wbar${u >= 97 ? ' is-full' : ''}`,
    vars: { '--u': u, '--fill': over ? even : u, '--even': even ?? 0 },
  }, [
    el('div', { class: 'wbar-rail' }, [
      el('i', { class: 'wbar-fill' }),
      over && el('i', { class: 'wbar-over' }),
      el('i', { class: 'wbar-rest' }),
      el('b', { class: 'wbar-stop' }),
    ]),
    known(even) && el('i', { class: 'wbar-even' }),
    known(even) && el('span', { class: 'wbar-label', text: `even pace ${pct(even)}` }),
  ]);
}

let projSeq = 0;
function projectionChart(p, openedAt, resetAt) {
  const proj = projection(p);
  const e = p.elapsedFrac * 100;
  const u = Math.min(100, Math.max(0, p.used));
  const fade = `projFade${++projSeq}`;
  // The plot stretches to any width; strokes, dots and labels stay their
  // own size because only the lines live in the stretched coordinates.
  const svg = svgEl('svg', { class: 'proj-svg', viewBox: '0 0 100 100', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  const defs = svgEl('defs');
  const gradient = svgEl('linearGradient', { id: fade, x1: 0, y1: 0, x2: 0, y2: 1 });
  gradient.append(svgEl('stop', { offset: 0, class: 'proj-fade-top' }), svgEl('stop', { offset: 1, class: 'proj-fade-bottom' }));
  defs.append(gradient);
  svg.append(defs);
  const line = (cls, x1, y1, x2, y2) => svg.append(svgEl('line', { class: cls, x1, y1, x2, y2 }));
  line('proj-grid is-top', 0, 0, 100, 0);
  line('proj-grid', 0, 50, 100, 50);
  line('proj-grid is-base', 0, 100, 100, 100);
  line('proj-even', 0, 100, 100, 0);
  svg.append(svgEl('polygon', { class: 'proj-area', points: `0,100 ${e},${100 - u} ${e},100`, fill: `url(#${fade})` }));
  line('proj-now', e, 100 - u, e, 100);
  line('proj-used', 0, 100, e, 100 - u);
  if (proj.runsOut && p.status !== 'exhausted') line('proj-ahead', e, 100 - u, proj.outAt * 100, 0);
  else if (!proj.runsOut && known(proj.end)) line('proj-ahead', e, 100 - u, 100, 100 - proj.end);

  const plot = el('div', { class: 'proj-plot' }, [svg, el('span', { class: 'proj-cap', text: '100%' })]);
  if (proj.runsOut) {
    const out = proj.outAt * 100;
    plot.prepend(el('div', { class: 'proj-lock', vars: { '--out': out } }));
    const gap = p.status === 'exhausted' ? p.remaining : p.early;
    if (100 - out >= 28) plot.append(el('span', { class: 'proj-lock-label', vars: { '--out': out }, text: `out of quota for ${duration(gap)}` }));
    if (p.status !== 'exhausted') plot.append(el('i', { class: 'proj-dot is-out', vars: { '--x': out, '--y': 0 } }));
  } else if (known(proj.end)) {
    plot.append(el('i', { class: 'proj-dot is-end', vars: { '--x': 100, '--y': 100 - proj.end } }));
    plot.append(el('span', {
      class: `proj-end${proj.end < 18 ? ' is-above' : ''}`, vars: { '--y': 100 - proj.end }, text: `${pct(proj.end)} at reset`,
    }));
  }
  plot.append(el('i', { class: 'proj-dot is-now', vars: { '--x': e, '--y': 100 - u } }));
  plot.append(el('span', { class: `proj-val${e < 22 ? ' is-right' : ''}`, vars: { '--x': e, '--y': 100 - u } }, [
    `${reading(u)}%`, el('small', { text: 'now' }),
  ]));

  const key = [['sw-line', 'Average so far'], ['sw-dash', 'At this rate'], ['sw-dot', 'Even pace']];
  if (proj.runsOut) key.push(['sw-hatch', 'Out of quota']);
  return el('div', { class: 'proj' }, [
    plot,
    el('div', { class: 'proj-axis' }, [
      el('span', { text: openedText(openedAt, p.total) }),
      el('span', { text: `reset ${momentText(resetAt, p.total)}` }),
    ]),
    el('div', { class: 'proj-legend' }, key.map(([cls, text]) => el('span', {}, [el('i', { class: `sw ${cls}` }), text]))),
  ]);
}

/** Chart labels sit beside their points; where two would overlap at the
    current width, the less important one steps aside (the same figure is
    in the headline). */
function settleChartLabels() {
  for (const plot of document.querySelectorAll('.proj-plot')) {
    const value = plot.querySelector('.proj-val')?.getBoundingClientRect();
    const now = plot.querySelector('.proj-dot.is-now')?.getBoundingClientRect();
    const overlaps = (a, b) => Boolean(b) && a.left < b.right + 4 && a.right > b.left - 4 && a.top < b.bottom + 2 && a.bottom > b.top - 2;
    for (const label of plot.querySelectorAll('.proj-end, .proj-cap')) {
      label.hidden = false;
      const box = label.getBoundingClientRect();
      // The reset figure also needs clear space after "now", or it sits on the dashed line.
      const crowded = label.classList.contains('proj-end') && Boolean(now) && box.left < now.right + 8;
      label.hidden = crowded || overlaps(box, value) || overlaps(box, now);
    }
  }
}

let freshlyOpened = null;
function toggleWindow(key) {
  if (state.openWindows.has(key)) state.openWindows.delete(key);
  else { state.openWindows.add(key); freshlyOpened = key; }
  renderResets();
}

/** One window: the stat card, and when opened, the projection below it. */
function windowCard({ key, name, used, p, openedAt, resetAt, span }) {
  const live = known(used);
  const open = Boolean(p) && state.openWindows.has(key);
  const resetLine = known(resetAt) && resetAt > Date.now() / 1000 ? `Resets ${momentText(resetAt, span)}` : resetText(resetAt);
  const card = el('article', {
    class: `wc${open ? ' is-open' : ''}`,
    'data-key': key,
    role: p ? 'button' : null,
    tabindex: p ? 0 : null,
    'aria-expanded': p ? String(open) : null,
    onclick: p ? () => toggleWindow(key) : null,
    onkeydown: p ? (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      toggleWindow(key);
    } : null,
  }, [
    el('div', { class: 'wc-head' }, [
      el('div', {}, [el('div', { class: 'wc-name', text: name }), el('div', { class: 'wc-when', text: resetLine })]),
      p && statusChip(p),
    ]),
    el('div', { class: 'wc-hero' }, [
      el('div', {}, [
        live ? el('div', { class: 'wc-num' }, [reading(used), el('span', { class: 'u', text: '%' })])
          : el('div', { class: 'wc-num is-small is-muted', text: 'Awaiting update' }),
        el('div', { class: 'wc-num-label', text: 'used' }),
      ]),
      known(resetAt) && el('div', { class: 'is-right' }, [
        el('div', { class: 'wc-num is-small', text: remainingTime(resetAt) }),
        el('div', { class: 'wc-num-label', text: 'until reset' }),
      ]),
    ]),
    live && paceBar(used, p),
  ]);
  if (!p) return card;
  const story = paceStory(p, resetAt);
  card.append(
    el('div', { class: 'wc-tiles' }, paceTiles(p)),
    el('div', { class: 'wc-foot' }, [
      iconWithClass(ICONS.clock, '', 15),
      rich('span', '', story.foot),
      el('span', { class: 'wc-more' }, [open ? 'Less' : 'Details', iconWithClass(ICONS.chevron, 'wc-chev', 15)]),
    ]),
  );
  if (open) {
    const [lead, em, tone, tail] = story.headline;
    card.append(el('div', { class: `wc-details${freshlyOpened === key ? ' is-fresh' : ''}` }, [
      el('div', { class: 'proj-headline' }, [lead, em && el('em', { class: tone ? `is-${tone}` : null, text: em }), tail]),
      rich('div', 'proj-support', story.support),
      projectionChart(p, openedAt, resetAt),
    ]));
  }
  return card;
}

// ── session budget ────────────────────────────────────────────────────────
// How many full 5-hour sessions the rest of the week holds. Providers report
// the two windows separately and never say how they relate, so the engine
// measures it: the weekly points that moved alongside each point of 5-hour
// movement, across this user's own readings.

function budgetFigures(pair, session, week) {
  const share = pair.share;
  const perSession = 100 * share;
  const weeklyLeft = Math.max(0, 100 - week.used_percent);
  const sessionUsed = session && windowCurrent(session) && known(session.used_percent) ? session.used_percent : null;
  const room = known(sessionUsed) ? (100 - sessionUsed) * share : null;
  return {
    share, perSession, weeklyLeft, sessionUsed, room,
    perWeek: 1 / share,
    sessionsLeft: weeklyLeft / perSession,
    // The 5-hour reading at which the weekly limit would stop this session.
    stopsAt: known(room) && weeklyLeft < room ? sessionUsed + weeklyLeft / share : null,
  };
}

function sessionCells(perWeek, weeklyUsed) {
  const n = Math.max(1, Math.min(24, Math.round(perWeek)));
  const each = 100 / n;
  return el('div', { class: 'budget-cells', role: 'img', 'aria-label': `${pct(weeklyUsed)} of the week used, about ${n} sessions in all` },
    Array.from({ length: n }, (_, i) => el('i', { vars: { '--f': Math.min(1, Math.max(0, weeklyUsed / each - i)) } }, [el('b')])));
}

function surfaceShares(breakdown) {
  const rows = (breakdown?.rows || []).filter(r => r.percent > 0);
  if (!rows.length) return null;
  const tones = ['var(--c)', 'var(--brand-6)', 'var(--brand-5)', 'var(--ink-3)'];
  return el('div', { class: 'budget-surfaces' }, [
    el('span', { class: 'kicker', text: 'This week by surface' }),
    el('div', { class: 'budget-surface-bar' }, rows.map((r, i) => el('i', { vars: { '--w': r.percent, '--tone': tones[i % tones.length] } }))),
    el('div', { class: 'proj-legend' }, rows.map((r, i) => el('span', {}, [
      el('i', { class: 'sw budget-swatch', vars: { '--tone': tones[i % tones.length] } }), `${r.name} ${pct(r.percent)}`]))),
  ]);
}

function budgetCard(provider, label) {
  const pair = (provider.budget || []).find(b => b.status !== 'unrelated');
  if (!pair) return null;
  const session = provider.windows.find(w => w.id === pair.session);
  const week = provider.windows.find(w => w.id === pair.weekly);
  if (!week || !windowCurrent(week) || !known(week.used_percent)) return null;
  const reset = momentText(week.resets_at, 7 * 86400);
  const measured = pair.status === 'measured' && pair.share > 0;
  const card = el('article', { class: 'wc budget' }, [
    el('div', { class: 'wc-head' }, [
      el('div', {}, [
        el('div', { class: 'wc-name', text: 'Your week in 5-hour sessions' }),
        el('div', { class: 'wc-when', text: `${week.label} limit · resets ${reset}` }),
      ]),
      el('span', { class: 'status-chip', vars: { '--c': measured ? 'var(--ok)' : 'var(--off)' } }, [
        iconWithClass(measured ? ICONS.check : ICONS.clock, '', 14), measured ? 'Measured' : 'Measuring']),
    ]),
  ]);
  if (measured) {
    const f = budgetFigures(pair, session, week);
    const left = f.sessionsLeft < 10 ? f.sessionsLeft.toFixed(1) : String(Math.round(f.sessionsLeft));
    const per = pct(f.perSession, f.perSession < 10 ? 1 : 0);
    card.append(
      el('div', { class: 'wc-hero' }, [
        el('div', {}, [el('div', { class: 'wc-num', text: left }), el('div', { class: 'wc-num-label', text: `full sessions left before ${reset}` })]),
        el('div', { class: 'is-right' }, [el('div', { class: 'wc-num is-small', text: `≈ ${Math.round(f.perWeek)}` }), el('div', { class: 'wc-num-label', text: 'sessions a week' })]),
      ]),
      sessionCells(f.perWeek, week.used_percent),
      rich('div', 'budget-line', `One full 5-hour session uses about **${per}** of the week, and **${pct(f.weeklyLeft)}** of it is left.`),
      known(f.stopsAt)
        ? rich('div', 'budget-line is-warn', `The weekly limit will stop this session at about **${pct(f.stopsAt)}**, before its own 5-hour limit.`)
        : known(f.room) ? rich('div', 'budget-line', `Using the rest of this session (now ${pct(f.sessionUsed)}) would take about **${pct(f.room)}** of the week.`) : null,
      el('div', { class: 'wc-tiles' }, [
        el('div', { class: 'wc-tile' }, [el('b', {}, [per.replace('%', ''), el('small', { text: '%' })]), el('span', { text: 'of the week per session' })]),
        el('div', { class: `wc-tile${known(f.stopsAt) ? ' is-hot' : ''}` }, [el('b', { text: left }), el('span', { text: 'sessions left' })]),
        el('div', { class: 'wc-tile' }, [el('b', { text: remainingTime(week.resets_at) }), el('span', { text: 'until the week resets' })]),
      ]),
    );
  } else {
    const progress = Math.min(1, pair.session_points / pair.needed_session_points, pair.weekly_points / pair.needed_weekly_points);
    card.append(
      el('div', { class: 'budget-line', text: `Learning how much of the week one 5-hour session uses. ${label} reports the two limits separately and never says how they relate, so this is measured from your own readings as they come in.` }),
      el('div', { class: 'meter budget-progress', vars: { '--w': progress * 100 } }, [el('i', { class: 'sess-context-fill' })]),
      el('div', { class: 'wc-when', text: `${compact(pair.session_points)} of ${compact(pair.needed_session_points)} points of 5-hour movement seen · the weekly window moved ${compact(pair.weekly_points)} of the ${compact(pair.needed_weekly_points)} points needed` }),
    );
  }
  const surfaces = surfaceShares(provider.breakdown);
  if (surfaces) card.append(surfaces);
  if (measured) card.append(el('div', { class: 'sess-notes' }, [el('p', {
    text: `Measured from your own usage: ${compact(pair.session_points)} points of 5-hour movement and the ${compact(pair.weekly_points)} weekly points that moved with them${pair.cycles > 1 ? `, across ${pair.cycles} weeks` : ''}. Readings are whole percentages, so treat it as approximate.`,
  })]));
  return card;
}

function providerSection({ id, label, live, meta, warning, onRefresh, cards, note, lead }) {
  const actions = el('div', { class: 'prov-actions' }, [
    warning && withTooltip(el('span', { class: 'prov-flag', tabindex: 0, text: 'Cached reading' }), () => [el('div', { text: warning })]),
    el('button', { class: 'icon-btn prov-refresh', type: 'button', title: 'Refresh usage', 'aria-label': `Refresh ${label} usage`, onclick: onRefresh }, [icon(ICONS.refresh, 16)]),
  ]);
  return el('section', { class: 'prov', vars: { '--c': PROVIDER_HUE[id] } }, [
    el('div', { class: 'prov-head' }, [
      el('div', { class: 'prov-mark' }, [icon(PROVIDER_GLYPH[id], 18)]),
      el('div', { class: 'prov-title' }, [
        el('h2', { text: label }),
        el('div', { class: 'prov-sub' }, [el('i', { class: `prov-live${live ? ' is-on' : ''}` }), el('span', { text: meta })]),
      ]),
      actions,
    ]),
    note,
    lead,
    cards.length > 0 && el('div', { class: 'prov-grid' }, cards),
  ]);
}

function cursorSection() {
  const report = state.cycle;
  if (!report) return null;
  const start = report.cycle_start ? Date.parse(report.cycle_start) / 1000 : null;
  const end = report.cycle_end ? Date.parse(report.cycle_end) / 1000 : null;
  const span = known(start) && known(end) ? end - start : 30 * 86400;
  const cards = [['total', 'Billing cycle', report.total_pct], ['api', 'Other models', report.api_pct], ['auto', 'Cursor models', report.auto_pct]]
    .filter(([, , used]) => known(used))
    .map(([pool, name, used]) => windowCard({
      key: `cursor:${pool}`, name, used, p: cursorPace(report, used), openedAt: start, resetAt: end, span,
    }));
  return providerSection({
    id: 'cursor', label: 'Cursor', live: !state.cycleError,
    meta: `Dashboard · ${relativeTime(report.fetched_at)}${state.cycleError ? ' · refresh failed' : ''}`,
    onRefresh: () => refreshCycle(true),
    cards,
    note: cards.length ? null : el('div', { class: 'wc is-note', text: 'Cursor returned no quota percentage for this cycle.' }),
  });
}

function renderResets() {
  const host = $('resetCards');
  const focused = document.activeElement?.closest?.('#resetCards [data-key]')?.dataset.key;
  host.replaceChildren();
  $('resetsNow').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })}`;
  for (const [id, label] of [['claude', 'Claude'], ['codex', 'Codex']]) {
    const provider = state.providers?.providers?.find(p => p.id === id);
    const current = providerCurrent(provider);
    const link = state.links[id] || {};
    const cards = current ? (provider.windows || []).map((window) => {
      const live = windowCurrent(window) && known(window.used_percent);
      const span = known(window.window_minutes) ? window.window_minutes * 60 : null;
      return windowCard({
        key: `${id}:${window.id || window.label}`,
        name: window.label,
        used: live ? window.used_percent : null,
        p: live ? windowPace(window) : null,
        openedAt: known(span) && known(window.resets_at) ? window.resets_at - span : null,
        resetAt: window.resets_at,
        span,
      });
    }) : [];
    let note = null;
    if (!cards.length || link.status === 'error' || link.status === 'connecting') {
      const reason = cards.length ? null : state.providersError || provider?.reason || (provider ? 'No current provider snapshot available.' : 'Waiting for provider data.');
      note = el('div', { class: 'wc is-note' }, [
        reason && el('div', { class: 'connection-reason', text: reason.replace('Open Claude Code to reconnect.', `Use Reconnect ${label} below.`) }),
        connectionControls(id, label, current),
      ]);
    }
    host.append(providerSection({
      id, label, live: current,
      meta: current ? `Connected · checked ${relativeTime(provider.fetched_at)}` : 'Unavailable',
      warning: current && provider.warning ? `${provider.warning} Showing the reading from ${relativeTime(provider.fetched_at)}.` : null,
      onRefresh: () => refreshProviders(true),
      cards, note,
      lead: current ? budgetCard(provider, label) : null,
    }));
  }
  const cursor = cursorSection();
  if (cursor) host.append(cursor);
  freshlyOpened = null;
  settleChartLabels();
  if (focused) host.querySelector(`[data-key="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
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
    renderResets();
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
  // Leaving a pin or changing follow mode keeps the list on screen and
  // clears only the selection while the engine finds the new one.
  const findSession = () => {
    state.pinned = null;
    state.sessionOpen = true;
    if (state.local) state.local = {...state.local, chat: null, reason: 'Finding session…'};
    saveSessionChoice();
    renderChat();
    refreshLocal();
  };
  $('btnUnpin').addEventListener('click', findSession);
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
  for (const chip of document.querySelectorAll('#sessionFollowRow [data-follow]')) {
    chip.addEventListener('click', () => {
      state.sessionFollow = chip.dataset.follow;
      findSession();
    });
  }
  for (const chip of document.querySelectorAll('#sessionProviders [data-provider]')) {
    chip.addEventListener('click', () => {
      if (chip.dataset.provider === state.sessionProvider && !state.pinned) return;
      state.sessionProvider = chip.dataset.provider;
      state.pinned = null;
      state.local = null;
      state.autoDetected = null;
      state.autoActive = false;
      state.sessionOpen = true;
      state.sessionRows = SESSION_ROWS;
      saveSessionChoice();
      renderChat();
      refreshLocal();
    });
  }
  renderChat();
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
  window.addEventListener('resize', settleChartLabels);
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
