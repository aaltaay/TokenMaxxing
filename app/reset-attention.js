'use strict';

// Event ids from reset_alerts.py are `${provider}:${windowId}:${phase}:${boundary}`
// (windowId may itself contain colons, e.g. Codex's "codex:primary"). Stripping
// the trailing segment peels off one part at a time regardless of what's inside.
function withoutLastSegment(id) {
  const i = id.lastIndexOf(':');
  return i === -1 ? id : id.slice(0, i);
}

class ResetAttention {
  constructor({getWindow, publish, buzz, notify, durationMs = 60000, repeatMs = 15000}) {
    Object.assign(this, {getWindow, publish, buzz, notify, durationMs, repeatMs});
    this.active = null;
    this.repeat = null;
    this.stopTimer = null;
    this.priorTop = null;
    // Dismissing an alert is a promise not to repeat it, not just a click.
    // The backend already dedupes exact re-sends, but its reset timestamp can
    // shift by a few seconds between polls, minting a "new" event id for what
    // is really the same warning — so dismissal is tracked here per window+phase
    // (id with the boundary stripped), independent of that jitter.
    this.dismissed = new Set();
  }
  show(events) {
    if (!events.length) return;
    // A window actually rolling over to a new cycle means any earlier
    // dismissal for that window no longer applies to what comes next.
    for (const e of events) {
      if (e.phase === 'reset') {
        const prefix = `${withoutLastSegment(withoutLastSegment(e.id))}:`;
        for (const d of this.dismissed) if (d.startsWith(prefix)) this.dismissed.delete(d);
      }
    }
    events = events.filter(e => e.phase === 'preview' || !this.dismissed.has(withoutLastSegment(e.id)));
    if (!events.length) return;
    const win = this.getWindow();
    if (!win || win.isDestroyed()) return;
    this.clearTimers();
    if (this.priorTop === null) this.priorTop = win.isAlwaysOnTop();
    const all = [...(this.active?.events || []), ...events];
    this.active = {events: [...new Map(all.map(e => [e.id, e])).values()], pulsing: true};
    if (win.isMinimized()) win.restore();
    win.setAlwaysOnTop(true, 'screen-saver');
    win.show();
    win.moveTop();
    win.focus();
    win.flashFrame(true);
    this.publish(this.active);
    this.buzz();
    this.notify(events.map(e => e.message).join('\n'));
    this.repeat = setInterval(() => this.buzz(), this.repeatMs);
    this.repeat.unref?.();
    this.stopTimer = setTimeout(() => {
      this.clearTimers();
      this.restoreTop();
      if (this.active) { this.active.pulsing = false; this.publish(this.active); }
    }, this.durationMs);
    this.stopTimer.unref?.();
  }
  clearTimers() { clearInterval(this.repeat); clearTimeout(this.stopTimer); }
  restoreTop() {
    const win = this.getWindow();
    if (win && !win.isDestroyed()) {
      win.flashFrame(false);
      if (this.priorTop !== null) win.setAlwaysOnTop(this.priorTop, 'floating');
    }
    this.priorTop = null;
  }
  dismiss() {
    for (const e of this.active?.events || []) {
      if (e.phase !== 'preview') this.dismissed.add(withoutLastSegment(e.id));
    }
    this.clearTimers(); this.restoreTop(); this.active = null; this.publish(null);
  }
}
module.exports = {ResetAttention};
