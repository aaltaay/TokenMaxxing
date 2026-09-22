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
  // Overview detail: compact rings, or every provider spelled out.
  overviewMode: 'compact',
  // What the bell badge reflects besides alerts: a waiting update and the
  // engine's own status.
  update: null,
  statusKind: 'off',
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

// ── overview ──────────────────────────────────────────────────────────────
// Compact by default: one ring per provider for the window that is live now,
// every other window and pool as a bar beneath it, and a line saying where
// there is room to work. Expanded keeps those rings and adds each provider's
// verdict in full, the watch list, and the cycle's own figures.

const OVERVIEW_RING = { compact: 92, expanded: 104 };

function overviewMode() {
  return state.overviewMode === 'expanded' ? 'expanded' : 'compact';
}

function setOverviewMode(mode) {
  state.overviewMode = mode;
  try { localStorage.setItem('overviewMode', mode); } catch { /* preference is optional */ }
  paintOverview();
}

/** The Cursor billing cycle and its pools, reduced to what the overview needs. */
function cursorSummary() {
  const report = state.cycle;
  const end = report?.cycle_end ? Date.parse(report.cycle_end) / 1000 : null;
  const used = report?.total_pct;
  const p = known(used) ? cursorPace(report, used) : null;
  const meters = report?.meters || [];
  return {
    id: 'cursor', label: 'Cursor', hue: PROVIDER_HUE.cursor,
    note: `${report?.plan ? `${report.plan} · ` : ''}monthly cycle`,
    ring: { value: used, label: 'cycle' },
    pace: p, resetAt: end, span: p?.total ?? 30 * 86400,
    bars: meters.map(meter => ({
      name: meter.label, value: meter.pct, even: p?.expected,
      maxxed: meter.status === 'maxxed' || meter.pct >= 100,
    })),
    foot: [
      known(report?.total_spend_cents)
        ? `${money(report.total_spend_cents, 0)}${known(report.burn_cents_per_day) ? ` · ${money(report.burn_cents_per_day, 0)} / day` : ''}`
        : null,
      known(end) ? `Resets ${momentText(end, 30 * 86400)}` : null,
    ],
    unavailable: report ? null : (state.cycleError || 'Waiting for the Cursor dashboard.'),
    onOpen: openDashboard,
  };
}

/** Claude or Codex: the live window in the ring, the rest as bars. */
function accountSummary(id, label) {
  const provider = state.providers?.providers?.find(p => p.id === id);
  const current = providerCurrent(provider);
  const windows = current ? (provider.windows || []).filter(w => windowCurrent(w) && known(w.used_percent)) : [];
  const ringWindow = windows[0];
  const p = ringWindow ? windowPace(ringWindow) : null;
  const rest = windows.slice(1);
  const bars = (rest.length ? rest : ringWindow ? [ringWindow] : []).map((window) => {
    const wp = windowPace(window);
    return {
      name: rest.length ? window.label : `${window.label} · against even pace`,
      value: window.used_percent, even: wp?.expected, maxxed: window.used_percent >= 100,
    };
  });
  const budget = (provider?.budget || []).find(b => b.status === 'measured' && b.share > 0);
  const week = budget && provider.windows.find(w => w.id === budget.weekly);
  const sessionsLeft = budget && week && known(week.used_percent)
    ? Math.max(0, 100 - week.used_percent) / (100 * budget.share) : null;
  const rate = p ? rateParts(p.allowedPerHour, p.total) : null;
  return {
    id, label, hue: PROVIDER_HUE[id],
    note: windows.length > 1 ? windows.map(w => w.label.replace(' (', ' · ').replace(')', '')).join(' + ') : ringWindow?.label || '',
    ring: { value: ringWindow?.used_percent, label: ringWindow?.label?.toLowerCase() },
    pace: p, resetAt: ringWindow?.resets_at, span: p?.total ?? 300 * 60,
    bars,
    foot: [
      known(sessionsLeft) ? `≈ ${sessionsLeft < 10 ? sessionsLeft.toFixed(1) : Math.round(sessionsLeft)} sessions left this week`
        : rate ? `${rate.value}%/${rate.unit} would last` : null,
      known(ringWindow?.resets_at) ? `Resets ${momentText(ringWindow.resets_at, p?.total ?? 300 * 60)}` : null,
    ],
    unavailable: current && ringWindow ? null
      : (state.providersError || provider?.reason || `Waiting for ${label} usage.`),
    onOpen: () => selectView('resets'),
  };
}

/** How long this provider's quota lasts at the current rate: the soonest of
    its windows to run dry, or to reset if none will. */
function summaryRunway(summary) {
  const p = summary.pace;
  const ring = !p ? null
    : (summary.ring.value ?? 0) >= 100 ? 0
    : known(p.runsOutIn) && p.runsOutIn < p.remaining ? p.runsOutIn
    : p.remaining;
  const spent = summary.bars.some(bar => bar.maxxed) ? 0 : null;
  const both = [ring, spent].filter(known);
  // Named apart from summary.ring, which is the figure in the circle.
  return { ringRunway: ring, runway: both.length ? Math.min(...both) : null };
}

function overviewSummaries() {
  return [cursorSummary(), accountSummary('claude', 'Claude'), accountSummary('codex', 'Codex')]
    .map(summary => ({ ...summary, ...summaryRunway(summary) }));
}

/** One line: where there is room, and what is about to bite. */
function overviewAdvice(summaries) {
  const live = summaries.filter(s => !s.unavailable && known(s.ring.value));
  if (!live.length) return [el('span', { text: 'Waiting for the first usage reading.' })];
  const ranked = [...live].sort((a, b) => (a.ring.value - b.ring.value)
    || ((a.resetAt ?? Infinity) - (b.resetAt ?? Infinity)));
  const best = ranked[0];
  const sentences = [];
  if (best.ring.value >= 100) {
    sentences.push(['Every provider is at its ceiling — ', el('b', { class: 'is-warn', text: best.label }),
      ` comes back first, ${momentText(best.resetAt, best.span)}.`]);
  } else {
    const sessions = best.foot[0] && best.foot[0].startsWith('≈') ? best.foot[0].slice(2) : null;
    sentences.push(['Work on ', el('b', { class: 'is-ok', text: best.label }), ' — ',
      el('b', { text: sessions ? sessions.split(' ')[0] : pct(100 - best.ring.value) }),
      sessions ? ` ${sessions.split(' ').slice(1).join(' ')}.` : ` of its ${best.ring.label} window is unused.`]);
  }
  for (const other of ranked.filter(s => s !== best && known(s.ringRunway) && s.ringRunway < 3 * 3600)) {
    sentences.push([el('b', { class: 'is-warn', text: other.label }),
      other.ringRunway <= 0 ? ' is out.' : ` runs out in ${duration(other.ringRunway)}.`]);
  }
  // A spent pool is only worth saying when it is not already the headline.
  const spent = live.filter(s => s !== best).flatMap(s => s.bars.filter(b => b.maxxed).map(b => ({ s, b })));
  if (spent.length) {
    sentences.push([`${spent.map(({ b }) => b.name).join(' and ')} spent until ${momentText(spent[0].s.resetAt, spent[0].s.span)}.`]);
  }
  return sentences.flatMap((parts, i) => (i ? [' ', ...parts] : parts));
}

function ringFigure(value, label, size, maxxed) {
  const radius = size / 2 - 7;
  const circumference = 2 * Math.PI * radius;
  const svg = svgEl('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true' });
  svg.append(svgEl('circle', { cx: size / 2, cy: size / 2, r: radius, class: 'ov-ring-track', 'stroke-width': 10 }));
  if (known(value)) {
    svg.append(svgEl('circle', {
      cx: size / 2, cy: size / 2, r: radius, 'stroke-width': 10,
      class: `ov-ring-arc${maxxed ? ' is-maxxed' : ''}`,
      'stroke-dasharray': `${Math.max(0, (circumference * Math.min(100, Math.max(0, value))) / 100 - 0.1)} ${circumference}`,
    }));
  }
  return el('div', { class: 'ov-ring' }, [
    svg,
    el('div', { class: 'ov-ring-mid' }, [
      known(value) ? el('b', {}, [reading(Math.min(999, value)), el('small', { text: '%' })]) : el('b', { class: 'is-off', text: '—' }),
      el('span', { text: label || '' }),
    ]),
  ]);
}

function overviewChip(summary) {
  if (summary.unavailable) return el('span', { class: 'ov-chip', vars: { '--s': 'var(--off)' }, text: 'Unavailable' });
  const p = summary.pace;
  if (!p) return null;
  const hot = p.status === 'fast' || p.status === 'ahead';
  return el('span', {
    class: 'ov-chip', vars: { '--s': SEVERITY[PACE_SEVERITY[p.status]] },
    text: hot ? `${times(p.multiplier)}× pace` : PACE_BADGE[p.status].toLowerCase(),
  });
}

function overviewWhen(summary) {
  if (summary.unavailable) return summary.unavailable;
  const p = summary.pace;
  const reset = known(summary.resetAt) ? `resets ${momentText(summary.resetAt, summary.span)}` : 'reset time unavailable';
  if (p && projection(p).runsOut && p.status !== 'exhausted') return `dry in ${duration(p.runsOutIn)} · ${reset}`;
  if (p && p.status === 'exhausted') return `out · ${reset}`;
  return `${remainingTime(summary.resetAt)} left · ${reset}`;
}

function overviewBar(bar) {
  return el('div', { class: 'ov-bar' }, [
    el('span', { class: 'ov-bar-name', text: bar.name }),
    el('span', { class: `ov-bar-val${bar.maxxed ? ' is-maxxed' : ''}`, text: `${pct(bar.value)}${bar.maxxed ? ' ✦' : ''}` }),
    el('span', { class: 'ov-bar-rail', vars: { '--w': Math.min(100, Math.max(0, bar.value || 0)), '--even': known(bar.even) ? Math.min(100, Math.max(0, bar.even)) : -5 } }, [
      el('i', { class: bar.maxxed ? 'is-maxxed' : null }),
      known(bar.even) ? el('b', { title: `${pct(bar.even)} would be even pace` }) : null,
    ]),
  ]);
}

/** Compact: ring, verdict, countdown, then every other window as a bar. */
function overviewCluster(summary) {
  return el('button', {
    type: 'button', class: 'ovc', vars: { '--c': summary.hue },
    title: `Open ${summary.label} detail`, onclick: summary.onOpen,
  }, [
    ringFigure(summary.ring.value, summary.ring.label, OVERVIEW_RING.compact, summary.ring.value >= 100),
    el('div', { class: 'ovc-head' }, [el('span', { class: 'ovc-name', text: summary.label }), overviewChip(summary)]),
    el('div', { class: 'ovc-when', text: overviewWhen(summary) }),
    el('div', { class: 'ovc-bars' }, summary.bars.map(overviewBar)),
  ]);
}

/** Expanded: the same ring with the verdict spelled out. */
function overviewCard(summary) {
  const p = summary.pace;
  const proj = p ? projection(p) : null;
  const headline = summary.unavailable ? 'Unavailable'
    : p && proj.runsOut && p.status !== 'exhausted' ? `Runs dry in ${duration(p.runsOutIn)}`
    : p && p.status === 'exhausted' ? 'Out until the reset'
    : `${remainingTime(summary.resetAt)} of ${summary.ring.label} left`;
  const detail = summary.unavailable ? summary.unavailable
    : p ? paceSentence(p) : 'No pace yet: this window reports no reset time.';
  return el('article', { class: 'ovp', vars: { '--c': summary.hue } }, [
    el('div', { class: 'ovp-head' }, [
      el('span', { class: 'prov-mark' }, [icon(PROVIDER_GLYPH[summary.id], 18)]),
      el('div', {}, [
        el('div', { class: 'ovp-name', text: summary.label }),
        el('div', { class: 'ovp-note', text: summary.note }),
      ]),
      overviewChip(summary),
    ]),
    el('div', { class: 'ovp-body' }, [
      ringFigure(summary.ring.value, summary.ring.label, OVERVIEW_RING.expanded, summary.ring.value >= 100),
      el('div', { class: 'ovp-facts' }, [
        el('div', { class: `ovp-headline${p && proj?.runsOut ? ' is-warn' : ''}`, text: headline }),
        el('div', { class: 'ovp-detail', text: detail }),
      ]),
    ]),
    summary.bars.length ? el('div', { class: 'ovp-bars' }, summary.bars.map(overviewBar)) : null,
    el('div', { class: 'ovp-foot' }, summary.foot.filter(Boolean).map(text => el('span', { text }))),
  ]);
}

/** Only what is actually biting, once each, worst first. */
function watchRows(summaries) {
  const rows = [];
  for (const alert of paceAlerts()) {
    const p = alert.pace;
    rows.push({
      tone: PACE_SEVERITY[p.status], icon: p.status === 'fast' ? PACE_ICON.fast : PACE_ICON.ahead,
      title: `${alert.name} ${projection(p).runsOut ? 'runs dry' : `is ${times(p.multiplier)}× the even pace`}`,
      detail: `${pct(alert.pace.used)} used · ${rateText(p.allowedPerHour, p.total)} would last`,
      when: projection(p).runsOut ? duration(p.runsOutIn) : remainingTime(alert.resets_at),
      sort: projection(p).runsOut ? p.runsOutIn : p.remaining,
    });
  }
  for (const summary of summaries) {
    const spent = summary.bars.filter(b => b.maxxed);
    if (spent.length) rows.push({
      tone: 'off', icon: ICONS.spark,
      title: `${spent.map(b => b.name).join(' and ')} ${spent.length > 1 ? 'are' : 'is'} spent`,
      detail: `${summary.label} · nothing left in ${spent.length > 1 ? 'these pools' : 'this pool'} until the reset`,
      when: known(summary.resetAt) ? momentText(summary.resetAt, summary.span) : '—',
      sort: Number.MAX_SAFE_INTEGER,
    });
    if (summary.unavailable) rows.push({
      tone: 'off', icon: ICONS.alert, title: `${summary.label} usage unavailable`,
      detail: summary.unavailable, when: '', sort: Infinity,
    });
  }
  return rows.sort((a, b) => a.sort - b.sort);
}

function cycleStats(report, full) {
  const end = report.cycle_end ? Date.parse(report.cycle_end) / 1000 : null;
  const stat = (value, unit, label, note) => el('div', { class: `ov-stat${full ? ' card' : ''}` }, [
    el('b', {}, [value, unit && el('small', { text: unit })]),
    el('span', { text: label }),
    full && note ? el('span', { class: 'sub', text: note }) : null,
  ]);
  return el('div', { class: `ov-stats${full ? '' : ' is-mini'}` }, [
    stat(money(report.total_spend_cents, 0), null, full ? 'Cursor reported spend' : 'cycle spend',
      `${money(report.included_cents, 0)} included · ${money(report.bonus_cents, 0)} bonus`),
    stat(known(report.burn_cents_per_day) ? money(report.burn_cents_per_day, 0) : '—', '/day', full ? 'average so far' : 'average',
      'reported spend ÷ elapsed cycle days'),
    stat(compact(report.agg_cache_read), null, full ? 'cache read tokens' : 'cache read',
      `${compact(report.agg_input)} in · ${compact(report.agg_output)} out`),
    full ? stat(compact(report.events_fetched), null, 'recorded events',
      report.events_complete ? 'complete history' : `of ${compact(report.events_total)} · incomplete`) : null,
    stat(known(end) ? remainingTime(end) : '—', null, full ? 'until the cycle resets' : 'cycle resets',
      known(end) ? momentText(end, 30 * 86400) : 'not returned by Cursor'),
  ]);
}

function topRuns(report, count) {
  const runs = (report.conversations || []).slice(0, count);
  if (!report.events_complete || !runs.length) return null;
  return el('article', { class: 'card' }, [
    el('span', { class: 'kicker', text: 'Top runs this cycle' }),
    el('div', { class: 'runs ov-runs' }, runs.map(row => el('div', { class: 'run' }, [
      el('span', { class: `run-dot${row.headless ? ' is-cloud' : ''}` }),
      el('span', { class: 'run-name', text: row.name, title: row.name }),
      el('span', { class: 'run-cost', text: `${compact(row.n)} event${row.n === 1 ? '' : 's'} · ${money(row.cents)}` }),
    ]))),
  ]);
}

function renderOverviewCompact(host, extra, summaries) {
  host.append(el('div', { class: 'ov-compact' }, summaries.map(overviewCluster)));
  const report = state.cycle;
  if (!report) return;
  extra.append(cycleStats(report, false));
  const runs = topRuns(report, 3);
  if (runs) extra.append(runs);
}

function renderOverviewExpanded(host, extra, summaries) {
  host.append(el('div', { class: 'ov-cards' }, summaries.map(overviewCard)));
  const report = state.cycle;
  if (!report) return;
  extra.append(cycleStats(report, true));
  const runs = topRuns(report, 6);
  if (runs) {
    runs.append(el('p', { class: 'tile-note', text: 'Event-reported usage values from Cursor usage history, not cash charges.' }));
    extra.append(runs);
  }
}

function renderOverview() {
  const mode = overviewMode();
  const host = $('overviewBody');
  const extra = $('overviewExtra');
  host.replaceChildren();
  extra.replaceChildren();
  for (const button of document.querySelectorAll('#overviewMode [data-mode]')) {
    button.setAttribute('aria-checked', String(button.dataset.mode === mode));
  }
  const summaries = overviewSummaries();
  if (mode === 'compact') renderOverviewCompact(host, extra, summaries);
  else renderOverviewExpanded(host, extra, summaries);
  renderBell(summaries);
}

// ── notifications ─────────────────────────────────────────────────────────
// Everything that asks for attention lives behind the bell: windows running
// dry, spent pools, where there is room to work, and the app's own status
// and updates. The badge counts what needs a look; the countdowns inside
// are the largest type in the panel.

function bellOpen() {
  return !$('bellPanel').hidden;
}

function setBellOpen(open) {
  $('bellPanel').hidden = !open;
  $('btnBell').setAttribute('aria-expanded', String(open));
}

function renderBell(summaries = overviewSummaries()) {
  const rows = watchRows(summaries);
  const list = $('bellRows');
  list.replaceChildren();
  if (rows.length) {
    for (const row of rows) list.append(el('button', {
      type: 'button', class: 'bell-row', vars: { '--s': SEVERITY[row.tone] || SEVERITY.off },
      onclick: () => { setBellOpen(false); selectView('resets'); },
    }, [
      iconWithClass(row.icon, '', 16),
      el('div', {}, [el('div', { class: 'bell-title', text: row.title }), el('div', { class: 'bell-detail', text: row.detail })]),
      el('span', { class: 'bell-when', text: row.when }),
    ]));
  } else {
    list.append(el('div', { class: 'bell-clear' }, [iconWithClass(ICONS.check, '', 16),
      el('span', { text: 'Nothing is over pace, and no pool is spent.' })]));
  }
  $('bellAdvice').replaceChildren(...overviewAdvice(summaries));
  $('bellCount').textContent = rows.length ? `${rows.length} now` : 'all clear';

  // The badge counts attention rows; an update waiting or an engine fault
  // still marks the bell even when every window is fine.
  const badge = $('bellBadge');
  const urgent = rows.some(row => row.tone === 'now' || row.tone === 'soon');
  const updateReady = state.update?.status === 'ready';
  const fault = state.statusKind === 'now';
  badge.hidden = !rows.length && !updateReady && !fault;
  badge.textContent = rows.length ? String(Math.min(rows.length, 9)) : '';
  badge.classList.toggle('is-calm', !urgent && !fault && !updateReady);
  $('btnBell').setAttribute('aria-label', rows.length ? `Notifications: ${rows.length} need attention` : 'Notifications');
}

function iconWithClass(paths, className, size = 18) {
  const node = icon(paths, size);
  if (className) node.setAttribute('class', className);
  return node;
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
  try {
    const saved = localStorage.getItem('overviewMode');
    if (saved === 'compact' || saved === 'expanded') state.overviewMode = saved;
  } catch { /* preference is optional */ }
  for (const button of document.querySelectorAll('#overviewMode [data-mode]')) {
    button.addEventListener('click', () => setOverviewMode(button.dataset.mode));
  }
  $('btnBell').addEventListener('click', (event) => {
    event.stopPropagation();
    setBellOpen(!bellOpen());
  });
  document.addEventListener('click', (event) => {
    if (bellOpen() && !event.target.closest('#bellPanel, #btnBell')) setBellOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && bellOpen()) setBellOpen(false);
  });
  window.hud.on('updateStatus', (value) => { state.update = value; renderBell(); });
  window.hud.updateStatus().then((value) => { state.update = value; renderBell(); }).catch(() => {});
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
  paintOverview();
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
  paintOverview();
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
function statusChip(p) {
  const hot = p.status === 'fast' || p.status === 'ahead';
  return el('span', { class: 'status-chip', vars: { '--c': SEVERITY[PACE_SEVERITY[p.status]] } }, [
    iconWithClass(PACE_ICON[p.status], '', 13),
    hot ? `${times(p.multiplier)}× pace` : PACE_BADGE[p.status],
  ]);
}

let projSeq = 0;
function projectionChart(p, openedAt, resetAt, { legend = true } = {}) {
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
    // With the headline gone, the label carries when it runs dry and for how long.
    const text = p.status === 'exhausted' ? `out of quota for ${duration(p.remaining)}`
      : `dry ${momentText(Date.now() / 1000 + p.runsOutIn, p.total)} · out ${duration(p.early)}`;
    plot.append(el('span', { class: `proj-lock-label${100 - out < 45 ? ' is-edge' : ''}`, vars: { '--out': out }, text }));
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
    legend && el('div', { class: 'proj-legend' }, key.map(([cls, text]) => el('span', {}, [el('i', { class: `sw ${cls}` }), text]))),
  ]);
}

/** Chart labels sit beside their points; where two would overlap at the
    current width, the less important one steps aside (the same figure is
    in the headline). */
function settleChartLabels() {
  for (const plot of document.querySelectorAll('.proj-plot')) {
    const val = plot.querySelector('.proj-val');
    if (val && !val.classList.contains('is-right')) {
      if (val.getBoundingClientRect().left < plot.getBoundingClientRect().left) val.classList.add('is-right');
    }
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

/** One window: its name, reset and verdict, then the projection. */
function windowCard({ key, name, used, p, openedAt, resetAt }) {
  const card = el('article', { class: 'wc is-chart', 'data-key': key }, [
    el('div', { class: 'wc-head' }, [
      el('div', { class: 'wc-title' }, [el('span', { class: 'wc-name', text: name })]),
      p && statusChip(p),
    ]),
  ]);
  card.append(p ? projectionChart(p, openedAt, resetAt, { legend: false })
    : el('div', { class: 'wc-empty', text: known(used) ? `${reading(used)}% used · ${resetText(resetAt).toLowerCase()}` : 'Awaiting update' }));
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

/** How many full 5-hour sessions the rest of the week holds, as one chip. */
function budgetChip(provider) {
  const pair = (provider.budget || []).find(b => b.status === 'measured' && b.share > 0);
  if (!pair) return null;
  const session = provider.windows.find(w => w.id === pair.session);
  const week = provider.windows.find(w => w.id === pair.weekly);
  if (!week || !windowCurrent(week) || !known(week.used_percent)) return null;
  const f = budgetFigures(pair, session, week);
  const left = f.sessionsLeft < 10 ? f.sessionsLeft.toFixed(1) : String(Math.round(f.sessionsLeft));
  return withTooltip(el('span', { class: 'prov-flag is-budget', tabindex: 0, text: `${left} of ~${Math.round(f.perWeek)} sessions left` }), () => [
    el('b', { text: 'Your week in 5-hour sessions' }),
    el('div', { class: 'muted', text: `One full 5-hour session uses about ${pct(f.perSession, f.perSession < 10 ? 1 : 0)} of the week, measured from your own readings.` }),
  ]);
}

function providerSection({ id, label, live, meta, warning, onRefresh, cards, note, badge }) {
  const actions = el('div', { class: 'prov-actions' }, [
    badge,
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
      badge: current ? budgetChip(provider) : null,
    }));
  }
  const cursor = cursorSection();
  if (cursor) host.append(cursor);
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
  const changed = state.statusKind !== kind;
  state.statusKind = kind;
  $('statusDot').style.setProperty('--c', SEVERITY[kind] || SEVERITY.off);
  $('statusText').textContent = text;
  if (changed) renderBell();
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
  renderOverview();
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
