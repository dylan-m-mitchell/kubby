(function () {
  "use strict";

  const kubui = {
    els: {},
    state: { info: null, tools: [] },

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
        toast: id("toast"),
      };
    },

    bindEvents() {
      this.els.refresh.addEventListener("click", () => this.load());
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

      card.appendChild(footer);
      return card;
    },

    /** Called from Python via evaluate_js — kept for API compatibility. */
    appendLog(line) {
      // No-op: install log panel was removed when install buttons were dropped.
    },
  };

  window.kubui = kubui;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => kubui.init());
  } else {
    kubui.init();
  }
})();
