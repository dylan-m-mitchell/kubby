(function () {
  "use strict";

  const kubby = {
    els: {},
    state: { tools: [], cluster: null, activeTab: "cluster" },

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
    },

    bindEvents() {
      this.els.refresh.addEventListener("click", () => this.load());
      this.els.tabs.addEventListener("click", (e) => {
        const btn = e.target.closest(".tab");
        if (!btn) return;
        this.switchTab(btn.dataset.tab);
      });
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
        const header = document.createElement("div");
        header.className = "ns-header";
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
      // No-op: install log panel was removed when install buttons were dropped.
    },
  };

  window.kubby = kubby;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => kubby.init());
  } else {
    kubby.init();
  }
})();
