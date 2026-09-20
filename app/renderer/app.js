'use strict';

/* AI Usage Command Center — renderer.
   Reads from the Python engine over window.hud and paints three views:
   Overview (dial + attention + tiles), This chat, Resets. */

const POLL = {
  clock: 1000,     // reset countdowns
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
  resets: null,
  pinned: null,
  focus: null,
  view: 'overview',
};

const $ = (id) => document.getElementById(id);

// ── formatting ────────────────────────────────────────────────────────────

const money = (cents, decimals = 2) =>
  `$${(Number(cents || 0) / 100).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;

function compact(n) {
  const v = Number(n || 0);
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(Math.round(v));
}

const pct = (n, digits = 0) => `${Number(n || 0).toFixed(digits)}%`;

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

/**
 * One band per provider, outermost first.
 *
 * Two different meters share the dial, so they are drawn differently:
 * a `pool` band is solid and thick (share of an allowance consumed), a
 * `window` band is thin and dashed (how far through a reset window we are).
 * The legend prints a percentage for one and a countdown for the other.
 */
function providerBands() {
  const bands = [];
  const cycle = state.cycle;
  if (cycle) {
    bands.push({
      id: 'cursor',
      label: 'Cursor',
      kind: 'pool',
      value: cycle.total_pct || 0,
      display: pct(cycle.total_pct),
      hint: `${pct(cycle.api_pct)} other models · ${pct(cycle.auto_pct)} cursor models`,
      tip: 'Share of this cycle’s included usage consumed.',
    });
  }
  for (const row of (state.resets?.rows || []).filter((r) => r.kind === 'session')) {
    bands.push({
      id: row.provider,
      label: row.short,
      kind: 'window',
      value: row.elapsed_pct,
      display: row.countdown,
      hint: row.anchored ? `session resets ${row.next_et}` : 'session window not anchored',
      tip: 'Time left in the session window — not a usage percentage.',
    });
  }
  let spare = 0;
  for (const band of bands) {
    band.hue = PROVIDER_HUE[band.id] || SPARE_HUES[spare++ % SPARE_HUES.length];
  }
  return bands;
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
    const value = Math.max(0, Math.min(100, band.value || 0));
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

    const maxxed = isPool && value >= 99.5;
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
  const total = state.cycle?.total_pct || 0;
  const value = $('dialValue');
  value.textContent = state.cycle ? pct(total) : '—';
  value.classList.toggle('is-maxxed', total >= 100);
  value.style.setProperty('--c', PROVIDER_HUE.cursor);
  $('dialLabel').textContent = total >= 100 ? 'maxxed out' : 'included usage';
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
      onclick: () => (band.kind === 'window' ? selectView('resets') : openDashboard()),
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
    bar.querySelector('i').style.width = `${Math.min(100, meter.pct).toFixed(1)}%`;
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
      el('span', { text: 'All clear. Nothing needs you right now.' }),
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
    host.append(el('div', { class: 'tile tile-span-2' }, [
      el('div', { class: 'tile-label', text: 'Spend this cycle' }),
      el('div', { class: 'tile-value skeleton', text: '$0,000.00' }),
    ]));
    return;
  }

  // Spend
  const spend = tile('Spend this cycle', money(report.total_spend_cents), [
    sparkline(report.spark || []),
    el('div', {
      class: 'tile-note',
      text: `${money(report.included_cents, 0)} included + ${money(report.bonus_cents, 0)} bonus`,
    }),
  ]);
  withTooltip(spend, () => [
    el('b', { text: money(report.total_spend_cents) }),
    el('div', { class: 'muted', text: `across ${(report.events_total || 0).toLocaleString('en-US')} billed events` }),
    el('div', { class: 'muted', text: 'Sparkline: daily spend, last 12 days.' }),
  ]);
  host.append(spend);

  // Tokens
  host.append(tile('Tokens', `${compact(report.agg_output)} out`, [
    el('div', { class: 'tile-note', text: `${compact(report.agg_input)} in` }),
    el('div', { class: 'tile-note', text: `${compact(report.agg_cache_read)} cache read` }),
    el('div', { class: 'tile-note', text: `${compact(report.agg_cache_write)} cache write` }),
  ]));

  // Burn rate
  host.append(tile('Burn rate', `${money(report.burn_cents_per_day, 0)} / day`, [
    deltaNode(report.burn_delta_pct),
    el('div', {
      class: 'tile-note',
      text: `day ${(report.elapsed_days || 0).toFixed(1)} of ${(report.cycle_days || 0).toFixed(0)}`,
    }),
  ]));

  // Runway — severity-coloured, because this is the number that bites.
  const left = report.api_days_left;
  const runwaySeverity = left === null || left === undefined ? 'off' : left < 1 ? 'now' : left < 3 ? 'soon' : 'ok';
  const runway = tile(
    'Runway',
    left === null || left === undefined ? '—' : left < 1 ? '< 1 day' : `${left.toFixed(1)} days`,
    [el('div', { class: 'tile-note', text: 'other-models pool at current burn' })],
  );
  runway.classList.add('tile-severity');
  runway.style.setProperty('--c', SEVERITY[runwaySeverity]);
  host.append(runway);

  // Agents & runs
  const runs = (report.conversations || []).slice(0, 4);
  host.append(el('div', { class: 'tile tile-span-2' }, [
    el('div', { class: 'tile-label', text: 'Agents & runs' }),
    el('div', { class: 'runs' }, runs.map((row) => el('div', { class: 'run' }, [
      el('span', { class: `run-dot${row.headless ? ' is-cloud' : ''}` }),
      el('span', { class: 'run-name', text: row.name, title: row.name }),
      el('span', { class: 'run-cost', text: `${row.n} · ${money(row.cents)}` }),
    ]))),
    el('div', {
      class: 'tile-note',
      text: `${(report.headless?.n || 0).toLocaleString('en-US')} cloud of ${(report.events_fetched || 0).toLocaleString('en-US')} events fetched`,
    }),
  ]));

  // The extension seam, stated plainly rather than implied.
  const add = el('button', {
    class: 'tile tile-add',
    type: 'button',
    onclick: () => banner('Tile catalog is not wired up yet — providers come from the engine.'),
  }, [
    iconWithClass(ICONS.plus, '', 15),
    el('div', { class: 'tile-note', text: 'Add a tile — provider, model, context, credits, compute' }),
  ]);
  host.append(add);
}

// ── this chat ─────────────────────────────────────────────────────────────

function renderChat() {
  const local = state.local;
  const select = $('chatSelect');
  const body = $('chatBody');
  const cats = $('chatCats');
  const billed = $('chatBilled');
  body.replaceChildren();
  cats.replaceChildren();
  billed.replaceChildren();

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
  const signature = (local.chats || []).map((c) => c.id).join('|');
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.replaceChildren(...(local.chats || []).map((c) => el('option', { value: c.id, text: c.name })));
  }
  if (local.chat?.id) select.value = local.chat.id;

  const chat = local.chat;
  if (!chat || chat.error) {
    $('chatModel').textContent = '—';
    body.append(el('div', {
      class: 'empty',
      text: chat ? `${chat.error} — open this chat in Cursor to populate it.` : 'No chat selected.',
    }));
    cats.append(el('div', { class: 'empty', text: 'No context snapshot to break down.' }));
    billed.append(el('div', { class: 'empty', text: 'Nothing to attribute yet.' }));
    return;
  }

  $('chatModel').textContent = chat.model || '—';
  const usedPct = chat.limit ? (100 * chat.used) / chat.limit : chat.pct || 0;
  // A context window is not a billing pool — half full is unremarkable.
  const severity = usedPct >= 90 ? 'now' : usedPct >= 70 ? 'soon' : 'ok';

  body.append(el('div', { class: 'tile-value', text: `${compact(chat.used)} / ${compact(chat.limit)}` }));
  body.append(el('div', { class: 'tile-note', text: `${pct(usedPct, 1)} of the context window · ${chat.name}` }));
  const meter = el('div', { class: 'meter', vars: { '--c': severity === 'ok' ? PROVIDER_HUE.cursor : SEVERITY[severity] } }, [el('i', {})]);
  meter.querySelector('i').style.width = `${Math.min(100, usedPct).toFixed(1)}%`;
  meter.classList.add('gap-above');
  body.append(meter);

  // Categories
  $('chatTax').textContent = `${pct(chat.tax_pct)} context tax`;
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
        text: row.overhead ? 'Context tax — re-sent every turn' : 'Conversation',
      }));
    }
    const node = el('div', { class: 'cat' }, [
      el('span', { class: 'cat-name', text: row.label }),
      el('span', { class: 'cat-val', text: `${compact(row.tokens)} · ${pct(row.share)}` }),
    ]);
    const bar = el('div', { class: 'meter is-plain cat-bar' }, [el('i', {})]);
    bar.style.setProperty('--c', row.overhead ? 'var(--ink-4)' : PROVIDER_HUE.cursor);
    bar.querySelector('i').style.width = `${Math.min(100, row.share).toFixed(1)}%`;
    node.append(bar);
    cats.append(node);
  }

  // Billed to this chat
  const match = (state.cycle?.conversations || []).find((c) => c.id === chat.id);
  if (!match) {
    billed.append(el('div', {
      class: 'empty',
      text: 'No billed events matched this chat id this cycle.',
    }));
  } else {
    billed.append(el('div', { class: 'tile-value', text: money(match.cents) }));
    billed.append(el('div', {
      class: 'tile-note',
      text: `${match.n} events · ${money(match.other_cents)} from the other-models pool`,
    }));
    billed.append(el('div', {
      class: 'tile-note',
      text: `${compact(match.out)} out · ${compact(match.cr)} cache read`,
    }));
  }
}

// ── resets ────────────────────────────────────────────────────────────────

function renderResets() {
  const host = $('resetCards');
  const data = state.resets;
  if (!data) return;

  $('resetsNow').textContent = data.now_et;
  $('resetConfigPath').textContent = data.config_path;

  const byProvider = new Map();
  for (const row of data.rows) {
    if (!byProvider.has(row.provider)) byProvider.set(row.provider, []);
    byProvider.get(row.provider).push(row);
  }

  host.replaceChildren();
  for (const [provider, rows] of byProvider) {
    const worst = rows.reduce((a, b) => (b.seconds_left < a.seconds_left ? b : a));
    const card = el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('h2', { text: rows[0].short }),
        el('span', {
          class: 'badge',
          text: worst.status === 'off' ? 'paused' : worst.status,
          vars: { '--c': SEVERITY[worst.status] },
        }),
      ]),
    ]);

    for (const row of rows) {
      const clock = el('div', { class: 'clock' }, [
        el('div', {}, [
          el('div', { class: 'clock-name', text: row.kind === 'session' ? 'Session' : 'Weekly' }),
          el('div', {
            class: 'clock-when',
            text: `next ${row.next_et}${row.last_buzzed ? ` · last buzz ${row.last_buzzed}` : ''}`,
          }),
        ]),
        el('div', { class: 'clock-left', text: row.countdown, vars: { '--c': SEVERITY[row.status] } }),
      ]);

      const flags = [];
      if (row.kind === 'session' && !row.anchored) flags.push('rolling guess — mark a session to sync');
      if (row.placeholder) flags.push('placeholder weekly time — paste the real one');
      if (row.error) flags.push(row.error);
      if (flags.length) clock.querySelector('.clock-when').textContent += ` · ${flags.join(' · ')}`;

      const bar = el('div', { class: 'meter is-plain clock-meter', vars: { '--c': SEVERITY[row.status] } }, [el('i', {})]);
      bar.querySelector('i').style.width = `${row.elapsed_pct.toFixed(1)}%`;
      clock.append(bar);
      card.append(clock);
    }

    card.append(el('div', { class: 'row-actions' }, [
      el('button', {
        class: 'chip',
        type: 'button',
        text: `Mark ${rows[0].short} session now`,
        onclick: async () => {
          state.resets = await window.hud.call('mark_session', { provider });
          renderResets();
          renderLegend();
          banner(`${rows[0].short} session anchored to now.`);
        },
      }),
    ]));

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
  $('cycleMeta').textContent = report
    ? `day ${(report.elapsed_days || 0).toFixed(1)} of ${(report.cycle_days || 0).toFixed(0)} · synced ${relativeTime(report.fetched_at)}`
    : '—';
  $('brandSub').textContent = report
    ? `${report.plan || 'Cursor'}${report.email ? ` · ${report.email}` : ''}`
    : state.cycleError || 'Reading local Cursor data…';
  $('statusEvents').textContent = report
    ? `${(report.events_fetched || 0).toLocaleString('en-US')} of ${(report.events_total || 0).toLocaleString('en-US')} events`
    : '';
}

async function refreshCycle(force = false) {
  if (state.fetching) return;
  state.fetching = true;
  $('btnRefresh').classList.add('is-spinning');
  setStatus('ok', force ? 'Refreshing usage…' : 'Fetching usage…');
  try {
    state.cycle = await window.hud.call('cycle', { force });
    state.cycleError = null;
    state.cycleAt = Date.now();
    setStatus('ok', 'Usage up to date');
  } catch (err) {
    state.cycleError = engineMessage(err);
    setStatus('now', state.cycleError);
  } finally {
    state.fetching = false;
    $('btnRefresh').classList.remove('is-spinning');
    paintOverview();
  }
}

async function refreshLocal() {
  try {
    state.local = await window.hud.call('local', { pinned: state.pinned });
    renderChat();
  } catch (err) {
    setStatus('soon', `Local read failed: ${engineMessage(err)}`);
  }
}

async function tickClocks() {
  try {
    state.resets = await window.hud.call('resets');
    renderResets();
    renderLegend();
  } catch {
    /* transient; the next tick retries */
  }
}

async function checkDue() {
  try {
    const { events } = await window.hud.call('due');
    if (!events.length) return;
    await window.hud.call('buzz');
    await window.hud.requestAttention();
    banner(events[0].message);
  } catch {
    /* buzzing is best-effort */
  }
}

// ── boot ──────────────────────────────────────────────────────────────────

async function boot() {
  const { platform, alwaysOnTop } = await window.hud.windowState();
  document.body.classList.toggle('is-mac', platform === 'darwin');
  $('btnPin').setAttribute('aria-pressed', String(alwaysOnTop));
  positionThumb();

  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener('click', () => selectView(tab.id.replace('tab-', '')));
  }

  $('btnRefresh').addEventListener('click', () => refreshCycle(true));
  $('btnPin').addEventListener('click', async () => {
    const next = $('btnPin').getAttribute('aria-pressed') !== 'true';
    const applied = await window.hud.setAlwaysOnTop(next);
    $('btnPin').setAttribute('aria-pressed', String(applied));
  });
  $('chatSelect').addEventListener('change', (e) => {
    state.pinned = e.target.value;
    refreshLocal();
  });
  $('btnUnpin').addEventListener('click', () => {
    state.pinned = null;
    refreshLocal();
  });
  $('btnTestBuzz').addEventListener('click', async () => {
    const { kind } = await window.hud.call('buzz');
    await window.hud.requestAttention();
    banner(`Test buzz (${kind})`);
  });
  $('btnOpenConfig').addEventListener('click', () => {
    if (state.resets?.config_path) window.hud.openPath(state.resets.config_path);
  });
  window.addEventListener('resize', positionThumb);

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

  await Promise.all([tickClocks(), refreshLocal()]);
  paintOverview();
  refreshCycle(false);

  setInterval(tickClocks, POLL.clock);
  setInterval(checkDue, POLL.due);
  setInterval(refreshLocal, POLL.local);
  setInterval(() => refreshCycle(false), POLL.cycle);
}

let booted = false;
function bootOnce() {
  if (booted) return;
  booted = true;
  setStatus('ok', 'Engine ready');
  boot();
}

window.hud.on('ready', bootOnce);
window.hud.on('down', ({ code }) => setStatus('now', `Engine stopped (${code}). Restarting…`));
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
