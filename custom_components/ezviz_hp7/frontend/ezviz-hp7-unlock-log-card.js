/*
 * EZVIZ HP7 unlock log card: the latest door and gate openings, each with
 * the picture the door station took. Tap a picture to see it large.
 *
 *   type: custom:ezviz-hp7-unlock-log-card
 *   title: Aperturas          # optional
 *   serial: BK0000000         # optional; every door station when left out
 *   limit: 10                 # optional, 1 to 10
 *   entity: event.door_unlock # optional: reload the moment it changes
 *
 * Rows come from the ezviz_hp7/unlock_log websocket command; picture links
 * are signed by Home Assistant and expire after an hour, so the list is
 * reloaded every few minutes.
 */
const TEXT = {
  en: {
    title: "Unlocks", empty: "No unlocks yet", error: "Could not load the unlocks",
    door: "Door", gate: "Gate", unknown: "Unknown",
    unlock_face: "Face", unlock_palm: "Palm", unlock_rfid: "Card",
    unlock_code: "Code", unlock_app: "App",
  },
  es: {
    title: "Aperturas", empty: "Aún no hay aperturas", error: "No se pudieron cargar las aperturas",
    door: "Puerta", gate: "Portón", unknown: "Desconocido",
    unlock_face: "Cara", unlock_palm: "Palma", unlock_rfid: "Tarjeta",
    unlock_code: "Código", unlock_app: "App",
  },
};
const ICONS = {
  unlock_face: "mdi:face-recognition", unlock_palm: "mdi:hand-back-right",
  unlock_rfid: "mdi:card-account-details", unlock_code: "mdi:dialpad",
  unlock_app: "mdi:cellphone", other: "mdi:lock-open-variant",
};
const RELOAD_MS = 5 * 60 * 1000;

class EzvizHp7UnlockLogCard extends HTMLElement {
  setConfig(config) {
    this._config = { limit: 10, ...(config || {}) };
    this._rows = null;
    this._error = false;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 2 + Math.min(this._config?.limit || 10, (this._rows || []).length);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const entity = this._config?.entity;
    const state = entity && hass.states[entity] ? hass.states[entity].state : undefined;
    if (first || (entity && state !== this._lastState)) {
      this._lastState = state;
      // A new unlock reaches the log at the next poll; wait a little for it.
      this._load(first ? 0 : 20000);
    }
  }

  connectedCallback() {
    this._timer = setInterval(() => this._load(0), RELOAD_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    clearTimeout(this._delay);
  }

  _t(key) {
    const lang = (this._hass?.language || "en").split("-")[0];
    return (TEXT[lang] || TEXT.en)[key] || TEXT.en[key] || key;
  }

  _load(delay) {
    clearTimeout(this._delay);
    this._delay = setTimeout(async () => {
      if (!this._hass) return;
      try {
        const msg = { type: "ezviz_hp7/unlock_log" };
        if (this._config.serial) msg.serial = String(this._config.serial);
        const res = await this._hass.callWS(msg);
        this._rows = (res.unlocks || []).slice(0, Math.max(1, Math.min(10, this._config.limit || 10)));
        this._error = false;
      } catch (err) {
        this._error = true;
      }
      this._render();
    }, delay);
  }

  _when(row) {
    // timeStr is the door station's local time, "YYYY-MM-DD HH:MM:SS".
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(row.time_str || "");
    if (!m) return "";
    const today = new Date();
    const sameDay = Number(m[1]) === today.getFullYear()
      && Number(m[2]) === today.getMonth() + 1 && Number(m[3]) === today.getDate();
    return sameDay ? `${m[4]}:${m[5]}` : `${m[3]}/${m[2]} ${m[4]}:${m[5]}`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const title = this._config?.title ?? this._t("title");
    const rows = this._rows;
    let body;
    if (this._error && !rows) {
      body = `<div class="msg">${this._t("error")}</div>`;
    } else if (!rows) {
      body = `<div class="msg">…</div>`;
    } else if (!rows.length) {
      body = `<div class="msg">${this._t("empty")}</div>`;
    } else {
      body = rows.map((row, i) => {
        const method = row.category ? this._t(row.category) : "";
        const who = String(row.who || "");  // EZVIZ's text, unchanged
        const icon = ICONS[row.category] || ICONS.other;
        const lock = row.lock === "gate" ? this._t("gate") : this._t("door");
        const pic = row.picture
          ? `<img class="thumb" data-i="${i}" src="${row.picture}" loading="lazy" alt="">`
          : `<div class="thumb none"><ha-icon icon="mdi:image-off-outline"></ha-icon></div>`;
        return `<div class="row">${pic}
          <div class="info">
            <div class="who">${escapeHtml(who || method || this._t("unknown"))}</div>
            <div class="sub"><ha-icon icon="${icon}"></ha-icon>${escapeHtml(method)}${method ? " · " : ""}${lock}</div>
          </div>
          <div class="when">${this._when(row)}</div>
        </div>`;
      }).join("");
    }
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .rows { padding: 0 16px 12px; }
        .row { display: flex; align-items: center; gap: 12px; padding: 6px 0;
               border-bottom: 1px solid var(--divider-color); }
        .row:last-child { border-bottom: none; }
        .thumb { width: 64px; height: 48px; object-fit: cover; border-radius: 6px;
                 cursor: zoom-in; flex: none; background: var(--secondary-background-color); }
        .thumb.none { display: flex; align-items: center; justify-content: center;
                      cursor: default; color: var(--secondary-text-color); }
        .info { flex: 1; min-width: 0; }
        .who { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .sub { color: var(--secondary-text-color); font-size: 0.9em; display: flex;
               align-items: center; gap: 4px; }
        .sub ha-icon { --mdc-icon-size: 16px; }
        .when { color: var(--secondary-text-color); font-variant-numeric: tabular-nums;
                white-space: nowrap; }
        .msg { padding: 8px 0 4px; color: var(--secondary-text-color); }
        .big { position: fixed; inset: 0; z-index: 999; background: rgba(0,0,0,.85);
               display: flex; align-items: center; justify-content: center; cursor: zoom-out; }
        .big img { max-width: 96vw; max-height: 92vh; border-radius: 8px; }
      </style>
      <ha-card header="${escapeHtml(title)}">
        <div class="rows">${body}</div>
      </ha-card>`;
    this.shadowRoot.querySelectorAll("img.thumb").forEach((img) => {
      img.addEventListener("click", () => this._open(img.src));
    });
  }

  _open(src) {
    const big = document.createElement("div");
    big.className = "big";
    big.innerHTML = `<img src="${src}" alt="">`;
    big.addEventListener("click", () => big.remove());
    this.shadowRoot.appendChild(big);
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

if (!customElements.get("ezviz-hp7-unlock-log-card")) {
  customElements.define("ezviz-hp7-unlock-log-card", EzvizHp7UnlockLogCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "ezviz-hp7-unlock-log-card",
    name: "EZVIZ HP7 unlock log",
    description: "Latest door and gate unlocks with their pictures.",
  });
}
