'use strict';

class ResetAttention {
  constructor({getWindow, publish, buzz, notify, durationMs = 60000, repeatMs = 15000}) {
    Object.assign(this, {getWindow, publish, buzz, notify, durationMs, repeatMs});
    this.active = null;
    this.repeat = null;
    this.stopTimer = null;
    this.priorTop = null;
  }
  show(events) {
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
    this.clearTimers(); this.restoreTop(); this.active = null; this.publish(null);
  }
}
module.exports = {ResetAttention};
