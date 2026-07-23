/*
 * EBO joystick card — a simple drag joystick for Home Assistant that publishes
 * {"x":-1..1,"y":-1..1} to an MQTT topic (default: ebo_air2/joystick) while you drag,
 * for smooth continuous driving of the Enabot EBO Air 2 (x = turn, y = forward).
 *
 * Install: copy this file to /config/www/, add it as a dashboard Resource
 *   URL: /local/ebo-joystick-card.js   Type: JavaScript Module
 * then add a card:
 *   type: custom:ebo-joystick-card
 *   topic: ebo_air2/joystick
 *   title: EBO drive
 */
class EboJoystickCard extends HTMLElement {
  setConfig(config) {
    this._topic = (config && config.topic) || "ebo_air2/joystick";
    this._title = (config && config.title) || "EBO drive";
    this._size = (config && config.size) || 220;
    if (!this._built) this._build();
  }

  set hass(hass) { this._hass = hass; }

  getCardSize() { return 4; }

  _build() {
    this._built = true;
    const s = this._size, r = s / 2, knob = s * 0.34;
    const root = document.createElement("ha-card");
    root.header = this._title;
    const wrap = document.createElement("div");
    wrap.style.cssText =
      "display:flex;justify-content:center;align-items:center;padding:16px;";
    const base = document.createElement("div");
    base.style.cssText =
      `position:relative;width:${s}px;height:${s}px;border-radius:50%;` +
      "background:var(--secondary-background-color,#2b2b2b);" +
      "box-shadow:inset 0 0 18px rgba(0,0,0,.5);touch-action:none;cursor:grab;";
    const dot = document.createElement("div");
    dot.style.cssText =
      `position:absolute;width:${knob}px;height:${knob}px;border-radius:50%;` +
      "left:50%;top:50%;transform:translate(-50%,-50%);" +
      "background:var(--primary-color,#03a9f4);box-shadow:0 2px 8px rgba(0,0,0,.5);";
    base.appendChild(dot);
    wrap.appendChild(base);
    root.appendChild(wrap);
    this.appendChild(root);

    let active = false, cx = 0, cy = 0, lastSend = 0;
    let curX = 0, curY = 0, timer = null;

    const publish = (x, y) => {
      if (!this._hass) return;
      this._hass.callService("mqtt", "publish", {
        topic: this._topic,
        payload: JSON.stringify({ x: +x.toFixed(2), y: +y.toFixed(2) }),
      });
    };

    const move = (px, py) => {
      let dx = px - cx, dy = py - cy;
      const dist = Math.hypot(dx, dy), max = r - knob / 2;
      if (dist > max) { dx *= max / dist; dy *= max / dist; }
      dot.style.transform = `translate(${dx - knob / 2}px,${dy - knob / 2}px)`;
      dot.style.left = "50%"; dot.style.top = "50%";
      curX = dx / max;            // right = +
      curY = -dy / max;           // up (forward) = +
    };

    const loop = () => {                    // resend ~10 Hz while dragging
      const now = Date.now();
      if (now - lastSend >= 100) { lastSend = now; publish(curX, curY); }
      if (active) timer = requestAnimationFrame(loop);
    };

    const start = (e) => {
      active = true; base.style.cursor = "grabbing";
      const rect = base.getBoundingClientRect();
      cx = rect.left + rect.width / 2; cy = rect.top + rect.height / 2;
      base.setPointerCapture(e.pointerId);
      move(e.clientX, e.clientY);
      lastSend = 0; loop();
    };
    const drag = (e) => { if (active) move(e.clientX, e.clientY); };
    const end = () => {
      active = false; base.style.cursor = "grab";
      dot.style.transform = "translate(-50%,-50%)";
      curX = 0; curY = 0;
      if (timer) cancelAnimationFrame(timer);
      publish(0, 0);              // stop on release (watchdog also stops it)
    };

    base.addEventListener("pointerdown", start);
    base.addEventListener("pointermove", drag);
    base.addEventListener("pointerup", end);
    base.addEventListener("pointercancel", end);
    base.addEventListener("lostpointercapture", end);
  }
}
customElements.define("ebo-joystick-card", EboJoystickCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "ebo-joystick-card",
  name: "EBO joystick",
  description: "Drag joystick to drive the Enabot EBO Air 2 (MQTT vector).",
});
