(function () {
  "use strict";

  const kubby = {
    els: {},
    state: { tools: [], cluster: null, activeTab: "cluster", minikubeSettings: null, minikubePrereqs: null, minikubeJobRunning: false, minikubeJobKind: null, logBuffer: [], },

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
        tabs: id("tabs"),
        tabCluster: id("tab-cluster"),
        tabDocs: id("tab-docs"),
        clusterContent: id("cluster-content"),
        cards: id("cards"),
        refresh: id("refresh"),
        toast: id("toast"),
      };
      // Log panel + modal root are created lazily on first need; clear
      // them here so a stale reference from a previous render doesn't
      // get reused if appendLog fires before render.
      this.els.logPanel = null;
      this.els.modalRoot = null;
    },

    bindEvents() {
      this.els.refresh.addEventListener("click", () => this.load());
      this.els.tabs.addEventListener("click", (e) => {
        const btn = e.target.closest(".tab");
        if (!btn) return;
        this.switchTab(btn.dataset.tab);
      });
      // Delegated handler for the minikube buttons (Start / Settings /
      // Stop / Delete) — we re-render on every state change, so attaching
      // per-render listeners is more fragile than delegation.
      this.els.clusterContent.addEventListener(
        "click",
        (e) => this._onClusterClick(e),
      );
    },

    switchTab(tab) {
      this.state.activeTab = tab;
      // Update tab buttons
      this.els.tabs.querySelectorAll(".tab").forEach((b) => {
        b.classList.toggle("active", b.dataset.tab === tab);
      });
      // Show/hide panels
      this.els.tabCluster.hidden = tab !== "cluster";
      this.els.tabDocs.hidden = tab !== "docs";
      // Lazy-load cluster data when switching to cluster tab
      if (tab === "cluster" && this.state.cluster === null) {
        this.loadCluster();
      }
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
        // Show skeleton in cluster tab while loading
        this.els.clusterContent.innerHTML = "";
        this.els.clusterContent.appendChild(this._skeleton(2));

        this.state.tools = await window.pywebview.api.get_status();
        // Always load cluster on startup (it's the default tab)
        await this.loadCluster();
        // Fetch minikube settings + prereqs in parallel; failures are
        // tolerated so the offline CTA can still render with a generic
        // placeholder.
        try {
          const [s, p] = await Promise.all([
            window.pywebview.api.get_minikube_settings(),
            window.pywebview.api.get_minikube_prerequisites(),
          ]);
          this.state.minikubeSettings = s;
          this.state.minikubePrereqs = p;
        } catch (e) {
          console.warn("minikube state fetch failed", e);
        }
        this.renderCluster();
        this.renderDocsCards();
        this.hideToast();
      } catch (err) {
        this.showToast("Failed to load: " + err);
      }
    },

    async loadCluster() {
      try {
        this.state.cluster = await window.pywebview.api.get_cluster_info();
        this.renderCluster();
      } catch (err) {
        this.state.cluster = { running: false, error: String(err) };
        this.renderCluster();
      }
    },

    // ---------- Cluster tab (table view) ----------

    renderCluster() {
      const el = this.els.clusterContent;
      const c = this.state.cluster;
      if (!c) {
        el.innerHTML = "";
        el.appendChild(this._skeleton(2));
        return;
      }

      el.innerHTML = "";

      if (!c.running) {
        el.appendChild(this._clusterOffline(c.error));
        return;
      }

      // Status bar + metrics
      el.appendChild(this._clusterHero(c));
      el.appendChild(this._clusterMetrics(c));

      // Nodes table
      if (c.nodes && c.nodes.length) {
        el.appendChild(this._clusterNodes(c.nodes));
      }

      // Namespaces with expandable pod lists
      if (c.namespaces && c.namespaces.length) {
        el.appendChild(this._clusterNamespaces(c.namespaces));
      }
    },

    _clusterOffline(error) {
      const wrap = document.createElement("div");
      wrap.className = "cluster-offline";
      wrap.innerHTML = `
        <div class="offline-icon">🐾</div>
        <h2>No cluster connected</h2>
        <p class="offline-error">${this._esc(error || "Could not reach any Kubernetes cluster")}</p>
        <p class="offline-hint">Start a cluster with <code>minikube start</code> or connect to one, then hit Re-check.</p>
      `;
      return wrap;
    },

    _clusterHero(c) {
      const hero = document.createElement("div");
      hero.className = "cluster-hero";
      const readyNodes = (c.nodes || []).filter((n) => n.status === "Ready").length;
      const totalNodes = (c.nodes || []).length;
      hero.innerHTML = `
        <div class="hero-left">
          <span class="status-dot ${totalNodes > 0 && readyNodes === totalNodes ? "ok" : "warn"}"></span>
          <h2>${this._esc(c.context || "cluster")}</h2>
        </div>
        <div class="hero-right">
          <span class="badge ok">running</span>
          ${c.version ? `<span class="badge info">${this._esc(c.version)}</span>` : ""}
        </div>
      `;
      return hero;
    },

    _clusterMetrics(c) {
      const grid = document.createElement("div");
      grid.className = "metrics-grid";
      const nodes = (c.nodes || []).length;
      const ready = (c.nodes || []).filter((n) => n.status === "Ready").length;
      const namespaceCount = (c.namespaces || []).length;
      const podCount = c.pod_count ?? ((c.namespaces || []).reduce((sum, ns) => sum + (ns.pods || []).length, 0));
      const metrics = [
        { label: "Nodes", value: `${ready}/${nodes}`, sub: "ready" },
        { label: "Pods", value: podCount, sub: "total" },
        { label: "Namespaces", value: namespaceCount, sub: "total" },
      ];
      for (const m of metrics) {
        const card = document.createElement("div");
        card.className = "metric-card";
        card.innerHTML = `
          <div class="metric-value">${m.value}</div>
          <div class="metric-label">${m.label}</div>
          <div class="metric-sub">${m.sub}</div>
        `;
        grid.appendChild(card);
      }
      return grid;
    },

    // ---- Table views ----

    _clusterNodes(nodes) {
      const section = document.createElement("div");
      section.className = "cluster-section";
      const h3 = document.createElement("h3");
      h3.textContent = "Nodes";
      section.appendChild(h3);

      const table = document.createElement("div");
      table.className = "node-table";
      for (const n of nodes) {
        const row = document.createElement("div");
        row.className = "node-row";
        const statusClass = n.status === "Ready" ? "ok" : "err";
        const roles = n.roles && n.roles.length ? n.roles.join(", ") : "worker";
        row.innerHTML = `
          <span class="node-name">${this._esc(n.name)}</span>
          <span class="node-roles">${this._esc(roles)}</span>
          <span class="badge ${statusClass}">${this._esc(n.status)}</span>
        `;
        table.appendChild(row);
      }
      section.appendChild(table);
      return section;
    },

    _clusterNamespaces(namespaces) {
      const section = document.createElement("div");
      section.className = "cluster-section";
      const h3 = document.createElement("h3");
      h3.textContent = "Namespaces";
      section.appendChild(h3);

      for (const ns of namespaces) {
        const nsBlock = document.createElement("div");
        nsBlock.className = "ns-block";

        // Namespace header (clickable to expand pods)
        const header = document.createElement("button");
        header.className = "ns-header";
        header.type = "button";
        const podCount = (ns.pods || []).length;
        header.innerHTML = `
          <span class="ns-chevron">▸</span>
          <span class="ns-name">${this._esc(ns.name)}</span>
          <span class="badge info">${podCount} pod${podCount !== 1 ? "s" : ""}</span>
        `;
        header.onclick = function () {
          const expanded = nsBlock.classList.toggle("expanded");
          const chevron = header.querySelector(".ns-chevron");
          chevron.textContent = expanded ? "▾" : "▸";
        };
        nsBlock.appendChild(header);

        // Pod table (hidden by default)
        const podTable = document.createElement("div");
        podTable.className = "pod-table";
        const pods = ns.pods || [];
        if (pods.length === 0) {
          const empty = document.createElement("div");
          empty.className = "pod-empty-row";
          empty.textContent = "No pods";
          podTable.appendChild(empty);
        } else {
          for (const pod of pods) {
            const row = document.createElement("div");
            row.className = "pod-row";
            const statusClass = (pod.status === "Running" || pod.status === "Succeeded") ? "ok" : "warn";
            row.innerHTML = `
              <span class="pod-name">${this._esc(pod.name)}</span>
              <span class="badge ${statusClass}">${this._esc(pod.status)}</span>
            `;
            podTable.appendChild(row);
          }
        }
        nsBlock.appendChild(podTable);
        section.appendChild(nsBlock);
      }
      return section;
    },

    // ---------- Docs tab (tool status) ----------

    renderDocsCards() {
      this.els.cards.innerHTML = "";
      this.state.tools.forEach((t, i) =>
        this.els.cards.appendChild(this._makeToolCard(t, i))
      );
    },

    _makeToolCard(tool, idx) {
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

    // ---------- helpers ----------

    _skeleton(count) {
      const frag = document.createDocumentFragment();
      for (let i = 0; i < count; i++) {
        const sk = document.createElement("div");
        sk.className = "skeleton";
        frag.appendChild(sk);
      }
      return frag;
    },

    _esc(s) {
      const d = document.createElement("div");
      d.textContent = s;
      return d.innerHTML;
    },

    /** Called from Python via evaluate_js — kept for API compatibility. */
    appendLog(line) {
      this.state.logBuffer.push(line);
      const MAX_LINES = 800;
      if (this.state.logBuffer.length > MAX_LINES) {
        this.state.logBuffer.splice(0, this.state.logBuffer.length - MAX_LINES);
      }
      if (this.els.logPanel && document.body.contains(this.els.logPanel)) {
        const log = this.els.logPanel.querySelector(".log");
        if (log) {
          log.textContent = this.state.logBuffer.join("\n");
          this._scrollLogToBottom();
        }
      }
    },

    /**
     * Called from Python when a minikube start/stop/delete job finishes.
     */
    onClusterActionDone(payload) {
      this.state.minikubeJobRunning = false;
      this.state.minikubeJobKind = null;
      if (payload && payload.ok) {
        const verb = (
          payload.action === "start"  ? "started" :
          payload.action === "stop"   ? "stopped" :
          payload.action === "delete" ? "deleted" :
          payload.action
        );
        this.showToast("minikube " + verb, "ok");
      } else {
        const err = (payload && payload.error) || "unknown error";
        this.showToast("minikube " + (payload && payload.action) + " failed: " + err, "err");
      }
      this.state.logBuffer = [];
      this.els.logPanel = null;
      this.loadCluster();
    },

    /** Idempotent guard: refuse new jobs while one is already running. */
    _canStartJob() {
      return !this.state.minikubeJobRunning;
    },

    async _startCluster() {
      if (!this._canStartJob()) return;
      try {
        await this._refreshMinikubeState();
      } catch (e) {
        this.showToast("Could not check prerequisites: " + e, "err");
        return;
      }
      const prereqs = this.state.minikubePrereqs;
      if (prereqs && !prereqs.ok) {
        this._showPrereqModal(prereqs);
        return;
      }
      this._beginJobUi("start");
      try {
        const res = await window.pywebview.api.start_minikube();
        if (!res || !res.ok) {
          this.showToast("Could not start minikube: " + (res && res.error ? res.error : "unknown error"), "err");
          this._endJobUi();
        }
      } catch (e) {
        this.showToast("minikube start failed: " + e, "err");
        this._endJobUi();
      }
    },

    async _stopCluster() {
      if (!this._canStartJob()) return;
      this._beginJobUi("stop");
      try {
        const res = await window.pywebview.api.stop_minikube();
        if (!res || !res.ok) {
          this.showToast("Could not stop minikube: " + (res && res.error ? res.error : "unknown error"), "err");
          this._endJobUi();
        }
      } catch (e) {
        this.showToast("minikube stop failed: " + e, "err");
        this._endJobUi();
      }
    },

    async _deleteCluster() {
      if (!this._canStartJob()) return;
      const ok = await this._confirm({
        title: "Delete minikube cluster?",
        body: "This removes the cluster and any workloads you had running on it. The minikube binary stays installed.",
        confirmLabel: "Delete cluster",
      });
      if (!ok) return;
      this._beginJobUi("delete");
      try {
        const res = await window.pywebview.api.delete_minikube();
        if (!res || !res.ok) {
          this.showToast("Could not delete: " + (res && res.error ? res.error : "unknown error"), "err");
          this._endJobUi();
        }
      } catch (e) {
        this.showToast("minikube delete failed: " + e, "err");
        this._endJobUi();
      }
    },

    _beginJobUi(kind) {
      this.state.minikubeJobRunning = true;
      this.state.minikubeJobKind = kind;
      this.state.logBuffer = [];
      this.renderCluster();
    },

    _endJobUi() {
      this.state.minikubeJobRunning = false;
      this.state.minikubeJobKind = null;
      this.renderCluster();
    },

    async _refreshMinikubeState() {
      const [s, p] = await Promise.all([
        window.pywebview.api.get_minikube_settings(),
        window.pywebview.api.get_minikube_prerequisites(),
      ]);
      this.state.minikubeSettings = s;
      this.state.minikubePrereqs = p;
    },

    _ensureModalRoot() {
      if (this.els.modalRoot && document.body.contains(this.els.modalRoot)) {
        return this.els.modalRoot;
      }
      const root = document.createElement("div");
      root.id = "kubby-modal-root";
      document.body.appendChild(root);
      this.els.modalRoot = root;
      return root;
    },

    _openSettings() {
      const root = this._ensureModalRoot();
      const settings = (this.state.minikubeSettings || { minikube: {} }).minikube;
      root.innerHTML = "";
      const back = document.createElement("div");
      back.className = "modal-backdrop";
      const modal = document.createElement("div");
      modal.className = "modal modal-wide";
      modal.innerHTML =
        '<h2>minikube settings</h2>' +
        '<p>Settings are saved to <span class="settings-path"></span> and persist across launches.</p>' +
        '<form id="kubby-settings-form" class="settings-form">' +
          '<label class="form-field"><span class="form-label">Driver</span>' +
            '<select name="driver" class="form-input">' +
              '<option value="">automatic (let minikube pick)</option>' +
              '<option value="docker">docker</option>' +
              '<option value="podman">podman</option>' +
              '<option value="kvm2">kvm2 (Linux, requires QEMU)</option>' +
              '<option value="none">none (Linux only)</option>' +
            '</select>' +
            '<span class="form-help">Container or VM driver. Defaults to whatever minikube auto-detects.</span>' +
          '</label>' +
          '<label class="form-field form-field-row"><span class="form-label">CPUs</span>' +
            '<input name="cpus" type="text" class="form-input form-input-small" placeholder="2" />' +
          '</label>' +
          '<label class="form-field form-field-row"><span class="form-label">Memory</span>' +
            '<input name="memory" type="text" class="form-input form-input-small" placeholder="2g" />' +
          '</label>' +
          '<label class="form-field form-field-row"><span class="form-label">Kubernetes version</span>' +
            '<input name="kubernetes_version" type="text" class="form-input form-input-small" placeholder="latest stable" />' +
          '</label>' +
          '<label class="form-field form-checkbox">' +
            '<input name="rootless" type="checkbox" />' +
            '<span class="form-label">Run as rootless (no sudo for the VM)</span>' +
          '</label>' +
          '<fieldset class="form-field">' +
            '<legend class="form-label">Addons</legend>' +
            '<div class="form-addons">' +
              '<label><input type="checkbox" data-addon="default" /> default (storage, dashboard)</label>' +
              '<label><input type="checkbox" data-addon="ingress" /> ingress (NGINX controller)</label>' +
              '<label><input type="checkbox" data-addon="metrics-server" /> metrics-server</label>' +
            '</div>' +
            '<span class="form-help">Addons are enabled at <code>minikube start</code> time. Defaults to <code>default</code>.</span>' +
          '</fieldset>' +
          '<div class="form-error" id="kubby-settings-error" hidden></div>' +
        '</form>' +
        '<div class="modal-actions">' +
          '<button type="button" class="ghost" data-action="cancel">Cancel</button>' +
          '<button type="button" class="primary" data-action="save">Save</button>' +
        '</div>';
      back.appendChild(modal);
      root.appendChild(back);
      modal.querySelector(".settings-path").textContent =
        (this.state.minikubePrereqs && this.state.minikubePrereqs.settings_path) ||
        "~/.config/kubby/settings.json";
      modal.querySelector('select[name="driver"]').value = settings.driver || "";
      modal.querySelector('input[name="cpus"]').value = settings.cpus || "";
      modal.querySelector('input[name="memory"]').value = settings.memory || "";
      modal.querySelector('input[name="kubernetes_version"]').value = settings.kubernetes_version || "";
      modal.querySelector('input[name="rootless"]').checked = !!settings.rootless;
      const addons = new Set(settings.addons || []);
      modal.querySelectorAll("[data-addon]").forEach((cb) => { cb.checked = addons.has(cb.dataset.addon); });
      const close = () => { root.innerHTML = ""; };
      back.addEventListener("click", (e) => { if (e.target === back) close(); });
      modal.querySelector('[data-action="cancel"]').addEventListener("click", close);
      modal.querySelector('[data-action="save"]').addEventListener("click", () => {
        const errEl = modal.querySelector("#kubby-settings-error");
        errEl.hidden = true;
        const cpus = modal.querySelector('input[name="cpus"]').value.trim();
        const memory = modal.querySelector('input[name="memory"]').value.trim();
        if (cpus && !/^\d+(\.\d+)?$/.test(cpus)) {
          errEl.textContent = "CPUs must be a positive number like '2' or '2.5' (or empty)";
          errEl.hidden = false; return;
        }
        if (memory && !/^\d+(\.\d+)?\s*(m|mi|mb|g|gi|gb)?$/i.test(memory)) {
          errEl.textContent = 'Memory must look like "2g", "4096mb", "2048Mi" (or empty)';
          errEl.hidden = false; return;
        }
        const addonNames = [];
        modal.querySelectorAll("[data-addon]").forEach((cb) => { if (cb.checked) addonNames.push(cb.dataset.addon); });
        const newSettings = {
          minikube: {
            ...((this.state.minikubeSettings || {}).minikube || {}),
            driver: modal.querySelector('select[name="driver"]').value,
            cpus, memory,
            kubernetes_version: modal.querySelector('input[name="kubernetes_version"]').value.trim(),
            rootless: modal.querySelector('input[name="rootless"]').checked,
            addons: addonNames,
          },
        };
        window.pywebview.api.save_minikube_settings(newSettings).then((res) => {
          if (!res || !res.ok) {
            errEl.textContent = (res && res.error) || "save failed";
            errEl.hidden = false; return null;
          }
          this.state.minikubeSettings = newSettings;
          return window.pywebview.api.get_minikube_prerequisites().then((p) => {
            this.state.minikubePrereqs = p;
            this.showToast("Settings saved", "warn");
            close();
            this.renderCluster();
          });
        }).catch((e) => { errEl.textContent = String(e); errEl.hidden = false; });
      });
    },

    _showPrereqModal(prereqs) {
      const root = this._ensureModalRoot();
      root.innerHTML = "";
      const back = document.createElement("div");
      back.className = "modal-backdrop";
      const modal = document.createElement("div");
      modal.className = "modal";
      const items = prereqs.issues.map((s) => "<li>" + this._esc(s) + "</li>").join("");
      modal.innerHTML =
        '<h2>Cannot start minikube yet</h2>' +
        '<p>A few prerequisites are missing:</p>' +
        '<ul class="prereq-list">' + items + '</ul>' +
        '<p class="modal-note">Open the <code>Docs</code> tab to install the missing tools, then come back here.</p>' +
        '<div class="modal-actions">' +
          '<button type="button" class="ghost" data-action="settings">Open settings</button>' +
          '<button type="button" class="primary" data-action="close">OK</button>' +
        '</div>';
      back.appendChild(modal);
      root.appendChild(back);
      const close = () => { root.innerHTML = ""; };
      modal.querySelector('[data-action="close"]').addEventListener("click", close);
      modal.querySelector('[data-action="settings"]').addEventListener("click", () => { close(); this._openSettings(); });
      back.addEventListener("click", (e) => { if (e.target === back) close(); });
    },

    _confirm(opts) {
      return new Promise((resolve) => {
        const root = this._ensureModalRoot();
        root.innerHTML = "";
        const back = document.createElement("div");
        back.className = "modal-backdrop";
        const modal = document.createElement("div");
        modal.className = "modal";
        modal.innerHTML =
          '<h2>' + this._esc(opts.title) + '</h2>' +
          '<p>' + this._esc(opts.body) + '</p>' +
          '<div class="modal-actions">' +
            '<button type="button" class="ghost" data-action="cancel">Cancel</button>' +
            '<button type="button" class="primary danger" data-action="confirm">' + this._esc(opts.confirmLabel || "Confirm") + '</button>' +
          '</div>';
        back.appendChild(modal);
        root.appendChild(back);
        const done = (val) => { root.innerHTML = ""; resolve(val); };
        back.addEventListener("click", (e) => { if (e.target === back) done(false); });
        modal.querySelector('[data-action="cancel"]').addEventListener("click", () => done(false));
        modal.querySelector('[data-action="confirm"]').addEventListener("click", () => done(true));
      });
    },

    _onClusterClick(e) {
      const btn = e.target.closest("[data-action]");
      if (!btn) return;
      switch (btn.dataset.action) {
        case "start-cluster":  this._startCluster(); break;
        case "open-settings":  this._openSettings(); break;
        case "stop-cluster":   this._stopCluster();  break;
        case "delete-cluster": this._deleteCluster();break;
      }
    },

    _renderLogPanel() {
      if (this.els.logPanel && document.body.contains(this.els.logPanel) && this.els.logPanel.parentElement) {
        this.els.logPanel.querySelector(".log").textContent = this.state.logBuffer.join("\n");
        this._scrollLogToBottom();
        return this.els.logPanel;
      }
      const panel = document.createElement("div");
      panel.className = "log-panel";
      panel.innerHTML = '<div class="log-header"><h2>minikube output</h2><span class="log-hint">streaming…</span></div><pre class="log"></pre>';
      panel.querySelector(".log").textContent = this.state.logBuffer.join("\n");
      this.els.logPanel = panel;
      this._scrollLogToBottom();
      return panel;
    },

    _scrollLogToBottom() {
      const log = this.els.logPanel && this.els.logPanel.querySelector(".log");
      if (!log) return;
      log.scrollTop = log.scrollHeight;
    },
  };

  window.kubby = kubby;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => kubby.init());
  } else {
    kubby.init();
  }
})();
