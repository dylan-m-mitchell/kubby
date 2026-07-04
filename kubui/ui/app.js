(function () {
  "use strict";

  // window.kubui is the namespace the Python side calls into via
  // `window.evaluate_js("window.kubui.appendLog(...)")`.
  const kubui = {
    els: {},
    state: { info: null, tools: [], inflight: new Set() },
    pendingTool: null,

    init() {
      this.cache();
      this.bindEvents();
      this.showSkeleton();
      const start = () => this.load();
      if (window.pywebview && window.pywebview.api) {
        start();
      } else {
        window.addEventListener("pywebviewready", start, { once: true });
      }
    },

    cache() {
      const id = (s) => document.getElementById(s);
      this.els = {
        cards: id("cards"),
        meta: id("meta"),
        refresh: id("refresh"),
        logPanel: id("log-panel"),
        log: id("log"),
        logTitle: id("log-title"),
        logClear: id("log-clear"),
        modal: id("confirm-modal"),
        modalTitle: id("confirm-title"),
        modalBody: id("confirm-body"),
        modalPm: id("confirm-pm"),
        modalNote: id("confirm-note"),
        modalCancel: id("confirm-cancel"),
        modalProceed: id("confirm-proceed"),
        toast: id("toast"),
      };
      // Cache pointer to the <strong> inside the modal body so we can update
      // the tool name in place without nuking the surrounding template.
      this.els.modalStrong = id("confirm-body").querySelector("strong");
    },

    bindEvents() {
      this.els.refresh.addEventListener("click", () => this.load());
      this.els.modalCancel.addEventListener("click", () => this.hideConfirm());
      this.els.logClear.addEventListener("click", () => this.clearLog());
      this.els.modal.addEventListener("click", (e) => {
        // click outside the .modal box on the backdrop dismisses
        if (e.target === this.els.modal) this.hideConfirm();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !this.els.modal.hidden) this.hideConfirm();
      });
    },

    showSkeleton() {
      this.els.cards.innerHTML = "";
      for (let i = 0; i < 4; i++) {
        const sk = document.createElement("div");
        sk.className = "skeleton";
        this.els.cards.appendChild(sk);
      }
    },

    showToast(msg, kind = "err") {
      this.els.toast.textContent = msg;
      this.els.toast.className = "toast " + kind;
      this.els.toast.hidden = false;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => this.hideToast(), 5000);
    },
    hideToast() {
      this.els.toast.hidden = true;
    },

    async load() {
      try {
        this.state.info = await window.pywebview.api.system_info();
        this.state.tools = await window.pywebview.api.get_status();
        this.renderMeta();
        this.renderCards();
        this.hideToast();
      } catch (err) {
        this.showToast("Failed to load: " + err);
      }
    },

    renderMeta() {
      const info = this.state.info;
      const pm = info.package_manager_label || "no package manager";
      const elv = info.elevation || "";
      this.els.meta.textContent = `${info.platform} · ${pm} · ${elv}`;
    },

    renderCards() {
      this.els.cards.innerHTML = "";
      this.state.tools.forEach((t, i) =>
        this.els.cards.appendChild(this.makeCard(t, i))
      );
    },

    makeCard(tool, idx) {
      const card = document.createElement("article");
      card.className = "card";
      card.dataset.key = tool.key;
      card.style.animationDelay = `${idx * 70}ms`;

      const header = document.createElement("div");
      header.className = "card-header";

      const titleWrap = document.createElement("div");
      const h3 = document.createElement("h3");
      h3.textContent = tool.label;
      const desc = document.createElement("p");
      desc.className = "desc";
      desc.textContent = tool.description;
      titleWrap.append(h3, desc);

      const badge = document.createElement("span");
      if (tool.installed) {
        badge.className = "badge ok";
        badge.textContent = tool.version ? `v${tool.version}` : "installed";
      } else {
        badge.className = "badge missing";
        badge.textContent = "not installed";
      }

      header.append(titleWrap, badge);
      card.appendChild(header);

      const footer = document.createElement("div");
      footer.className = "card-footer";

      const link = document.createElement("a");
      link.className = "link";
      link.href = tool.website;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "docs ↗";
      footer.appendChild(link);

      const btn = document.createElement("button");
      btn.className = tool.installed ? "ghost btn" : "primary btn";
      const inflight = this.state.inflight.has(tool.key);
      btn.disabled = inflight;
      btn.textContent = inflight
        ? "installing…"
        : tool.installed
        ? "reinstall"
        : "install";
      btn.addEventListener("click", () => {
        if (kubui.state.inflight.has(tool.key)) return;
        kubui.requestInstall(tool, btn);
      });
      footer.appendChild(btn);

      card.appendChild(footer);
      return card;
    },

    requestInstall(tool, btn) {
      const info = this.state.info || {};
      const pmLabel = info.package_manager_label || "system package manager";
      const note = info.elevation
        ? `Elevation: ${info.elevation}.`
        : "";
      this.pendingTool = { tool };
      this.els.modalTitle.textContent = `install ${tool.label}?`;
      this.els.modalStrong.textContent = tool.label;
      this.els.modalPm.textContent = pmLabel;
      this.els.modalNote.textContent = note;
      this.els.modalProceed.disabled = false;
      this.els.modalProceed.textContent = "install";
      this.els.modalProceed.onclick = () => this.confirmInstall(tool);
      this.els.modal.hidden = false;
      this.els.modalProceed.focus();
    },

    hideConfirm() {
      const key = this.pendingTool?.tool?.key;
      this.els.modal.hidden = true;
      this.pendingTool = null;
      // Restore focus to the matching card's button. We do this via
      // querySelector (not by caching the prior DOM node) because the card
      // DOM gets replaced by renderCards() during install.
      if (key) {
        const btn = document.querySelector(
          `.card[data-key="${CSS.escape(key)}"] .btn`
        );
        if (btn) btn.focus();
      }
    },

    async confirmInstall(tool) {
      if (this.state.inflight.has(tool.key)) return; // already running
      this.state.inflight.add(tool.key);
      this.els.modalProceed.disabled = true;
      this.els.modalProceed.textContent = "installing…";
      this.openLog(tool);
      this.clearLog();
      this.appendLog(`→ installing ${tool.label}…`);
      try {
        const res = await window.pywebview.api.install_tool(tool.key);
        if (res.ok) {
          this.appendLog(
            `✔ done — ${tool.label} ${res.version ? "v" + res.version : ""}`.trim()
          );
        } else {
          this.appendLog(`✘ failed: ${res.error || "unknown error"}`);
        }
      } catch (err) {
        this.appendLog(`✘ failed: ${err}`);
      } finally {
        this.state.inflight.delete(tool.key);
        await this.load(); // refresh status badges (replaces card DOM)
        this.hideConfirm(); // close modal AFTER re-render so focus can find the new button
      }
    },

    openLog(tool) {
      this.els.logTitle.textContent = `install log · ${tool.label}`;
      this.els.logPanel.hidden = false;
    },

    appendLog(line) {
      // Called from Python via window.evaluate_js("window.kubui.appendLog(...)").
      this.els.log.textContent += line + "\n";
      this.els.log.scrollTop = this.els.log.scrollHeight;
    },

    clearLog() {
      this.els.log.textContent = "";
    },
  };

  window.kubui = kubui;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => kubui.init());
  } else {
    kubui.init();
  }
})();
