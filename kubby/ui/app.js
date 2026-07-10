(function () {
  "use strict";

  const kubby = {
    els: {},
    state: { tools: [], cluster: null, activeTab: "cluster", minikubeSettings: null, minikubePrereqs: null, minikubeJobRunning: false, minikubeJobKind: null, installJobKey: null, globalLogTitle: null, globalLogHint: null, globalLogPinned: false, logBuffer: [],
      // Image search state
      searchQuery: "", searchTagFilter: "", localImages: [],
      remoteResults: { results: [], total_count: 0, has_more: false, page: 1 },
      imagePullInProgress: null, pulledImages: [],
      _searchTimer: null, _tagFilterTimer: null, _searchToken: 0, },

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
        tabSearch: id("tab-search"),
        clusterContent: id("cluster-content"),
        cards: id("cards"),
        refresh: id("refresh"),
        toast: id("toast"),
        globalLog: id("global-log"),
        globalLogContent: id("global-log-content"),
        globalLogTitle: id("global-log-title"),
        globalLogHint: id("global-log-hint"),
        installAll: id("install-all"),
        // Image search elements
        searchInput: id("search-input"),
        tagFilter: id("tag-filter"),
        dockerHubLink: id("dockerhub-link"),
        searchResults: id("search-results"),
        searchEmpty: id("search-empty"),
        searchStatus: id("search-status"),
        loadMore: id("load-more"),
      };
      // Modal root is created lazily on first need.
      this.els.modalRoot = null;
    },

    bindEvents() {
      this.els.refresh.addEventListener("click", () => this.load());
      this.els.tabs.addEventListener("click", (e) => {
        const btn = e.target.closest(".tab");
        if (!btn) return;
        this.switchTab(btn.dataset.tab);
      });
      // Delegated handlers: we re-render on every state change, so
      // attaching per-render listeners is more fragile than delegation.
      // clusterContent handles the minikube Start/Settings/Stop/Delete
      // buttons. cards handles the per-tool Install buttons on the
      // docs tab.
      this.els.clusterContent.addEventListener(
        "click",
        (e) => this._onClusterClick(e),
      );
      this.els.cards.addEventListener(
        "click",
        (e) => this._onDocsClick(e),
      );
      this.els.installAll.addEventListener("click", () => this._installAll());
      // Search tab events
      if (this.els.searchInput) {
        this.els.searchInput.addEventListener("input", () => this._onSearchInput());
        this.els.searchInput.addEventListener("change", () => this._updateDockerHubLink());
        this.els.tagFilter.addEventListener("input", () => {
          this.state.searchTagFilter = (this.els.tagFilter ? this.els.tagFilter.value : "").trim();
          this._renderSearchResults();
          clearTimeout(this.state._tagFilterTimer);
          this.state._tagFilterTimer = setTimeout(() => this._doSearch(), 300);
        });
      }
      if (this.els.loadMore) {
        this.els.loadMore.addEventListener("click", () => this._loadMoreRemote());
      }
    },

    switchTab(tab) {
      this.state.activeTab = tab;
      // Update tab buttons
      this.els.tabs.querySelectorAll(".tab").forEach((b) => {
        b.classList.toggle("active", b.dataset.tab === tab);
      });
      // Show/hide panels
      this.els.tabCluster.hidden = tab !== "cluster";
      this.els.tabSearch.hidden = tab !== "search";
      this.els.tabDocs.hidden = tab !== "docs";
      // Lazy-load cluster data when switching to cluster tab
      if (tab === "cluster" && this.state.cluster === null) {
        this.loadCluster();
      }
      // Lazy-load local images when switching to search tab
      if (tab === "search") {
        this._updateDockerHubLink();
        if (this.state.localImages.length === 0) {
          this._loadLocalImages();
        }
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
        // Note: do NOT call hideToast() here. The install flow surfaces
        // its own success/failure toast ~1-60s after the user clicks
        // Install; if the user clicks Re-check in that window, we'd
        // silently dismiss the install's toast. Toasts already auto-dismiss
        // after 5s in showToast().
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
        this._syncGlobalLog();
        return;
      }

      // Status bar + metrics
      el.appendChild(this._clusterHero(c));
      this._syncGlobalLog();
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
        <p class="offline-hint">Start a cluster with <code>minikube start</code> (or connect to an existing one), then hit Re-check.</p>
      <div class="offline-actions">
        <button type="button" class="primary" data-action="start-cluster"${this.state.minikubeJobRunning ? " disabled" : ""}>${this.state.minikubeJobRunning ? (this.state.minikubeJobKind || "start") + " in progress…" : "Start minikube"}</button>
        <button type="button" class="ghost" data-action="open-settings"${this.state.minikubeJobRunning ? " disabled" : ""}>⚙ Settings</button>
      </div>
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
      <div class="hero-actions">
        <button type="button" class="ghost" data-action="open-settings"${this.state.minikubeJobRunning ? " disabled" : ""} title="Edit minikube settings">⚙</button>
        <button type="button" class="ghost" data-action="stop-cluster"${this.state.minikubeJobRunning ? " disabled" : ""}>Stop</button>
        <button type="button" class="ghost danger" data-action="delete-cluster"${this.state.minikubeJobRunning ? " disabled" : ""}>Delete</button>
      </div>
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
      this._syncInstallAllButton();
    },

    _syncInstallAllButton() {
      const btn = this.els.installAll;
      if (!btn) return;
      const uninstalled = (this.state.tools || []).filter((t) => !t.installed);
      const busy = this.state.installJobKey || this.state.minikubeJobRunning;
      btn.hidden = uninstalled.length === 0;
      btn.disabled = busy;
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

      // Install button: only shown for uninstalled tools.
      // Disabled while any job (install or minikube) is running, so we
      // don't overlap elevation prompts or stream two log panels at once.
      if (!tool.installed) {
        const installBtn = document.createElement("button");
        installBtn.type = "button";
        installBtn.className = "primary btn-sm";
        installBtn.dataset.action = "install";
        const busy = this.state.installJobKey || this.state.minikubeJobRunning;
        if (this.state.installJobKey === tool.key) {
          installBtn.textContent = "Installing…";
          installBtn.disabled = true;
        } else if (busy) {
          installBtn.textContent = "Install";
          installBtn.disabled = true;
          installBtn.title = "another job is in progress";
        } else {
          installBtn.textContent = "Install";
        }
        footer.appendChild(installBtn);
      }

      card.appendChild(footer);
      return card;
    },

    // ---------- helpers ----------

    // --- image search ---

    async _loadLocalImages() {
      try {
        const res = await window.pywebview.api.search_images("", 1, true, false);
        if (res && res.ok) {
          this.state.localImages = (res.local || []).map((img) => ({
            ...img,
            pulled: true,
          }));
          this._renderSearchResults();
        }
      } catch (e) {
        // podman probably not installed — empty list is fine
      }
    },

    _onSearchInput() {
      clearTimeout(this.state._searchTimer);
      this.state._searchTimer = setTimeout(() => this._doSearch(), 300);
      this._updateDockerHubLink();
    },

    _updateDockerHubLink() {
      const link = this.els.dockerHubLink;
      if (!link) return;
      const query = (this.els.searchInput ? this.els.searchInput.value : "").trim();
      if (query) {
        link.href = "https://hub.docker.com/search?q=" + encodeURIComponent(query);
      } else {
        link.href = "https://hub.docker.com/search";
      }
    },

    async _doSearch() {
      const query = (this.els.searchInput ? this.els.searchInput.value : "").trim();
      const tagFilter = (this.els.tagFilter ? this.els.tagFilter.value : "").trim();
      const token = ++this.state._searchToken;
      this.state.searchQuery = query;
      this.state.searchTagFilter = tagFilter;
      this.state.remoteResults = { results: [], total_count: 0, has_more: false, page: 1 };

      // Show/hide empty state
      if (!query) {
        this.els.searchStatus.hidden = true;
        // Reset to just local images
        this.state.remoteResults = { results: [], total_count: 0, has_more: false, page: 1 };
        try {
          const res = await window.pywebview.api.search_images("", 1, true, false);
          if (token !== this.state._searchToken) return;
          if (res && res.ok) {
            this.state.localImages = (res.local || []).map((img) => ({
              ...img,
              pulled: true,
            }));
          }
        } catch (e) {}
        if (token !== this.state._searchToken) return;
        this._renderSearchResults();
        return;
      }

      this.els.searchStatus.hidden = false;
      this.els.searchStatus.textContent = "Searching…";
      if (this.els.loadMore) this.els.loadMore.hidden = true;
      this._renderSearchResults(); // clear old results, show loading

      try {
        const res = await window.pywebview.api.search_images(query, 1);
        if (token !== this.state._searchToken) return;
        if (res && res.ok) {
          this.state.localImages = (res.local || []).map((img) => ({
            ...img,
            pulled: true,
          }));
          const remote = res.remote || { results: [], total_count: 0, has_more: false };
          remote.results = (remote.results || []).map((img) => ({
            ...img,
            pulled: this._isImagePulled(img.name),
          }));
          this.state.remoteResults = remote;
        }
      } catch (e) {
        // Network error — show empty remote
        if (token !== this.state._searchToken) return;
        this.state.remoteResults = { results: [], total_count: 0, has_more: false, page: 1 };
      }

      if (token !== this.state._searchToken) return;
      this.els.searchStatus.hidden = true;
      this._renderSearchResults();
    },

    async _loadMoreRemote() {
      const token = this.state._searchToken;
      const nextPage = (this.state.remoteResults.page || 1) + 1;
      if (this.els.loadMore) this.els.loadMore.disabled = true;

      try {
        const res = await window.pywebview.api.search_images(
          this.state.searchQuery,
          nextPage,
          false,
          true
        );
        if (token !== this.state._searchToken) return;
        if (res && res.ok) {
          const remote = res.remote || { results: [], total_count: 0, has_more: false };
          const newResults = (remote.results || []).map((img) => ({
            ...img,
            pulled: this._isImagePulled(img.name),
          }));
          this.state.remoteResults.results = [
            ...(this.state.remoteResults.results || []),
            ...newResults,
          ];
          this.state.remoteResults.has_more = remote.has_more || false;
          this.state.remoteResults.page = nextPage;
        }
      } catch (e) {}

      if (token !== this.state._searchToken) return;
      if (this.els.loadMore) {
        this.els.loadMore.disabled = false;
        this.els.loadMore.hidden = !this.state.remoteResults.has_more;
      }
      this._renderSearchResults();
    },

    _renderSearchResults() {
      const grid = this.els.searchResults;
      const empty = this.els.searchEmpty;
      if (!grid) return;

      grid.innerHTML = "";
      let local = this.state.localImages || [];
      let remote = (this.state.remoteResults && this.state.remoteResults.results) || [];

      // Apply tag filter client-side so rapid tag-filter keystrokes
      // re-render instantly from already-loaded data.
      const tf = (this.state.searchTagFilter || "").trim().toLowerCase();
      if (tf) {
        local = local.filter((img) =>
          (img.tags || []).some((t) => t.toLowerCase().includes(tf))
        );
        remote = remote.filter((img) =>
          (img.tags || []).some((t) => t.toLowerCase().includes(tf))
        );
      }

      const hasAny = local.length > 0 || remote.length > 0;
      const loading = !this.els.searchStatus.hidden;

      if (empty) {
        empty.hidden = hasAny || loading || this.state.searchQuery !== "";
      }

      if (!hasAny && !loading) {
        if (this.state.searchQuery) {
          const msg = document.createElement("div");
          msg.className = "search-empty";
          msg.innerHTML = '<span class="search-empty-icon">🔍</span><p>No images found for "' + this._esc(this.state.searchQuery) + '"</p>';
          grid.appendChild(msg);
        }
        if (this.els.loadMore) this.els.loadMore.hidden = true;
        return;
      }

      // Local section
      if (local.length > 0) {
        const label = document.createElement("div");
        label.className = "search-section-label";
        label.textContent = `Local (${local.length})`;
        grid.appendChild(label);
        local.forEach((img) => grid.appendChild(this._makeImageCard(img)));
      }

      // Remote section
      if (remote.length > 0) {
        const label = document.createElement("div");
        label.className = "search-section-label";
        label.textContent = `GitHub Container Registry (${this.state.remoteResults.total_count || remote.length})`;
        grid.appendChild(label);
        remote.forEach((img) => grid.appendChild(this._makeImageCard(img)));
      } else if (loading && this.state.searchQuery) {
        for (let i = 0; i < 3; i++) {
          const sk = document.createElement("div");
          sk.className = "skeleton";
          grid.appendChild(sk);
        }
      }

      if (this.els.loadMore) {
        this.els.loadMore.hidden = !this.state.remoteResults.has_more;
      }
    },

    _makeImageCard(img) {
      const card = document.createElement("article");
      card.className = "card";
      if (img.local && img.pulled) card.classList.add("local");
      if (this.state.imagePullInProgress === img.name) card.classList.add("pulling");

      const header = document.createElement("div");
      header.className = "card-header";

      const titleWrap = document.createElement("div");
      const h3 = document.createElement("h3");
      h3.textContent = img.name;
      h3.style.cssText = "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;word-break:break-all;";
      titleWrap.appendChild(h3);

      if (img.description) {
        const desc = document.createElement("p");
        desc.className = "desc";
        desc.textContent = img.description;
        titleWrap.appendChild(desc);
      }

      const badgeWrap = document.createElement("div");
      badgeWrap.style.cssText = "display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0;";

      if (img.pulled) {
        const b = document.createElement("span");
        b.className = "badge ok";
        b.textContent = "pulled";
        badgeWrap.appendChild(b);
      }
      if (img.local) {
        const b = document.createElement("span");
        b.className = "badge info";
        b.textContent = "local";
        badgeWrap.appendChild(b);
      }

      header.append(titleWrap, badgeWrap);
      card.appendChild(header);

      // Tags
      if (img.tags && img.tags.length > 0) {
        const meta = document.createElement("div");
        meta.className = "card-image-meta";
        const maxTags = 5;
        img.tags.slice(0, maxTags).forEach((t) => {
          const chip = document.createElement("span");
          chip.className = "tag-chip";
          chip.textContent = t;
          meta.appendChild(chip);
        });
        if (img.tags.length > maxTags) {
          const more = document.createElement("span");
          more.className = "tag-chip more";
          more.textContent = "+" + (img.tags.length - maxTags) + " more";
          meta.appendChild(more);
        }
        card.appendChild(meta);
      }

      // Stats
      const stats = document.createElement("div");
      stats.className = "card-stats";
      if (img.stars !== undefined && img.stars !== null) {
        stats.innerHTML += '<span>⭐ ' + img.stars + '</span>';
      }
      if (img.updated_at) {
        const date = img.updated_at.slice(0, 10);
        stats.innerHTML += '<span>📅 ' + date + '</span>';
      }
      if (img.size) {
        stats.innerHTML += '<span>💾 ' + img.size + '</span>';
      }
      card.appendChild(stats);

      // Pull progress (if in progress)
      if (this.state.imagePullInProgress === img.name) {
        const spinner = document.createElement("span");
        spinner.className = "spinner";
        const progress = document.createElement("div");
        progress.className = "pull-progress";
        progress.id = "pull-progress-" + btoa(encodeURIComponent(img.name)).slice(0, 12);
        const top = document.createElement("div");
        top.style.cssText = "display:flex;align-items:center;gap:6px;";
        top.appendChild(spinner);
        const label = document.createElement("span");
        label.textContent = "Pulling…";
        top.appendChild(label);
        progress.appendChild(top);
        const lines = document.createElement("div");
        lines.className = "pull-lines";
        progress.appendChild(lines);
        card.appendChild(progress);
      }

      // Footer with pull button
      const footer = document.createElement("div");
      footer.className = "card-footer";

      // Link to GitHub repo for remote images
      if (img.remote && img.owner && img.repo) {
        const link = document.createElement("a");
        link.className = "link";
        link.href = "https://github.com/" + img.owner + "/" + img.repo + "/pkgs/container/" + img.repo;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "GitHub ↗";
        footer.appendChild(link);
      } else {
        const spacer = document.createElement("span");
        footer.appendChild(spacer);
      }

      // Pull button (only for non-pulled images)
      if (!img.pulled) {
        const pullBtn = document.createElement("button");
        pullBtn.type = "button";
        pullBtn.className = "primary btn-sm";
        const busyMinikube = this.state.minikubeJobRunning || !!this.state.installJobKey;
        const busyPull = !!this.state.imagePullInProgress;
        if (this.state.imagePullInProgress === img.name) {
          pullBtn.textContent = "Pulling…";
          pullBtn.disabled = true;
        } else if (busyPull || busyMinikube) {
          pullBtn.textContent = "Pull";
          pullBtn.disabled = true;
          pullBtn.title = "another job is in progress";
        } else {
          pullBtn.textContent = "Pull";
          pullBtn.addEventListener("click", () => this._pullImage(img.name));
        }
        footer.appendChild(pullBtn);
      }

      card.appendChild(footer);
      return card;
    },

    async _pullImage(ref) {
      if (this.state.imagePullInProgress) return;
      if (this.state.minikubeJobRunning || !!this.state.installJobKey) return;

      this.state.imagePullInProgress = ref;
      this._renderSearchResults();

      try {
        const res = await window.pywebview.api.pull_image(ref);
        if (res && !res.ok) {
          this.showToast("Pull failed: " + (res.error || "unknown"), "err");
          this.state.imagePullInProgress = null;
          this._renderSearchResults();
        }
      } catch (e) {
        this.showToast("Pull failed: " + e, "err");
        this.state.imagePullInProgress = null;
        this._renderSearchResults();
      }
    },

    onImagePullProgress(ref, line) {
      if (this.state.imagePullInProgress !== ref) return;
      const id = "pull-progress-" + btoa(encodeURIComponent(ref)).slice(0, 12);
      const el = document.getElementById(id);
      if (el) {
        const lines = el.querySelector(".pull-lines");
        if (lines) {
          lines.textContent += line + "\n";
          lines.scrollTop = lines.scrollHeight;
        }
      }
    },

    onImagePullDone(payload) {
      const ref = this.state.imagePullInProgress;
      this.state.imagePullInProgress = null;
      if (payload && payload.ok) {
        this.state.pulledImages.push(payload.image_ref);
        this.showToast("Image pulled: " + (payload.image_ref || ref), "ok");
        // Update cached remote results' pulled state before re-rendering
        const remote = this.state.remoteResults;
        if (remote && remote.results) {
          for (const img of remote.results) {
            img.pulled = this._isImagePulled(img.name);
          }
        }
        // Refresh local images while preserving search context
        this._refreshLocalImages();
      } else {
        const err = (payload && payload.error) || "unknown error";
        this.showToast("Pull failed: " + err, "err");
        this._renderSearchResults();
      }
    },

    async _refreshLocalImages() {
      try {
        const res = await window.pywebview.api.search_images(
          this.state.searchQuery, 1, true, false
        );
        if (res && res.ok) {
          this.state.localImages = (res.local || []).map((img) => ({
            ...img,
            pulled: true,
          }));
          this._renderSearchResults();
        }
      } catch (e) {}
    },

    _isImagePulled(name) {
      if ((this.state.pulledImages || []).includes(name)) return true;
      return (this.state.localImages || []).some((img) => img.name === name);
    },

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

    /** Called from Python via evaluate_js — pushes one line of streaming
     *  output (minikube start/stop/delete or a per-tool install) into
     *  the global log panel. Buffer is bounded so a runaway stream
     *  doesn't OOM the webview. */
    appendLog(line) {
      this.state.logBuffer.push(line);
      const MAX_LINES = 800;
      if (this.state.logBuffer.length > MAX_LINES) {
        this.state.logBuffer.splice(0, this.state.logBuffer.length - MAX_LINES);
      }
      if (this.els.globalLogContent) {
        this.els.globalLogContent.textContent = this.state.logBuffer.join("\n");
        this._scrollLogToBottom();
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
        // Success: discard the streaming log, hide the panel, reload cluster.
        this.state.logBuffer = [];
        this._hideGlobalLog();
        this.loadCluster();
      } else {
        const err = (payload && payload.error) || "unknown error";
        this.showToast("minikube " + (payload && payload.action) + " failed: " + err, "err");
        // Failure: keep the global log visible so the user can read
        // minikube diagnostics. Pin the log so the renderCluster() below
        // doesn't immediately hide it via _syncGlobalLog. Defer
        // loadCluster() until the user dismisses or re-tries.
        this.state.globalLogPinned = true;
        this.renderCluster();
      }
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
      this._showGlobalLog("minikube output", kind + " in progress…");
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

    /**
     * Wire up minimal dialog semantics (role, aria-modal), focus trap,
     * and Escape-to-dismiss for a modal that lives inside `back` (the
     * backdrop). Returns a wrapped close function that also restores
     * focus to the previously-active element. Callers funnel their
     * backdrop/Cancel dismissals through the returned function so
     * focus restoration is consistent.
     */
    _setupModalA11y(modal, escapeCloseFn) {
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");
      const previouslyFocused = document.activeElement;
      const focusables = () =>
        modal.querySelectorAll(
          "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex=\"-1\"])"
        );
      const initial = focusables()[0];
      if (initial) {
        initial.focus();
      } else {
        modal.tabIndex = -1;
        modal.focus();
      }
      // Every dismiss path (backdrop click, Cancel, Confirm, Escape)
      // funnels through `wrappedClose`, which removes the keydown
      // listener and restores focus before invoking the per-path
      // closeFn. `escapeCloseFn` is what's called on Escape — pass it
      // as `() => done(false)` in confirm-style dialogs so Escape
      // always means Cancel even if the user just clicked Confirm.
      const wrappedClose = (customCloseFn) => {
        document.removeEventListener("keydown", onKey);
        (customCloseFn || escapeCloseFn)();
        if (previouslyFocused && previouslyFocused.focus) {
          try { previouslyFocused.focus(); } catch (_) { /* element gone */ }
        }
      };
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          wrappedClose();
          return;
        }
        if (e.key !== "Tab") return;
        const items = Array.from(focusables());
        if (items.length === 0) {
          e.preventDefault();
          return;
        }
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      };
      // Attach keydown to document (not backdrop) so Escape still works
      // when focus has drifted outside the modal entirely.
      document.addEventListener("keydown", onKey);
      return wrappedClose;
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
      const dismiss = this._setupModalA11y(modal, close);
      back.addEventListener("click", (e) => { if (e.target === back) dismiss(); });
      modal.querySelector('[data-action="cancel"]').addEventListener("click", () => dismiss());
      modal.querySelector('[data-action="save"]').addEventListener("click", () => {
        const errEl = modal.querySelector("#kubby-settings-error");
        errEl.hidden = true;
        const cpus = modal.querySelector('input[name="cpus"]').value.trim();
        const memory = modal.querySelector('input[name="memory"]').value.trim();
        if (cpus && !/^\d+(\.\d+)?$/.test(cpus)) {
          errEl.textContent = "CPUs must be a positive number like '2' or '2.5' (or empty)";
          errEl.hidden = false; return;
        }
        if (memory && !/^\d+(\.\d+)?(m|mi|mb|g|gi|gb)?$/i.test(memory)) {
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
            dismiss();
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
      const dismiss = this._setupModalA11y(modal, close);
      modal.querySelector('[data-action="close"]').addEventListener("click", () => dismiss());
      modal.querySelector('[data-action="settings"]').addEventListener("click", () => { dismiss(); this._openSettings(); });
      back.addEventListener("click", (e) => { if (e.target === back) dismiss(); });
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
        const dismissModal = this._setupModalA11y(modal, () => done(false));
        back.addEventListener("click", (e) => { if (e.target === back) dismissModal(); });
        modal.querySelector('[data-action="cancel"]').addEventListener("click", () => dismissModal());
        modal.querySelector('[data-action="confirm"]').addEventListener("click", () => dismissModal(() => done(true)));
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

    _onDocsClick(e) {
      const btn = e.target.closest("[data-action=\"install\"]");
      if (!btn) return;
      const card = btn.closest("[data-key]");
      if (!card) return;
      this._installTool(card.dataset.key);
    },

    async _installAll() {
      if (this.state.installJobKey || this.state.minikubeJobRunning) return;
      const uninstalled = (this.state.tools || []).filter((t) => !t.installed);
      if (uninstalled.length === 0) return;
      this.state.installJobKey = "__all__";
      this.state.logBuffer = [];
      this._showGlobalLog("installing " + uninstalled.length + " tool(s)", "streaming…");
      this.renderDocsCards();
      let res;
      try {
        res = await window.pywebview.api.install_all();
      } catch (e) {
        res = { ok: false, error: String(e) };
      }
      this.state.installJobKey = null;
      if (res && res.ok) {
        this.showToast("All tools installed", "ok");
        this.state.logBuffer = [];
        this._hideGlobalLog();
        await this.load();
      } else {
        const installed = (res && res.installed) ? res.installed.length : 0;
        const failed = (res && res.failed) ? res.failed.length : 0;
        if (installed === 0 && failed === 0 && res && res.error) {
          this.showToast("Install all failed: " + res.error, "err");
        } else {
          this.showToast(
            installed + " installed, " + failed + " failed",
            installed > 0 ? "ok" : "err"
          );
        }
        this.state.globalLogPinned = true;
        await this.load();
      }
    },

    async _installTool(key) {
      // One install at a time across the whole app, so we don't overlap
      // elevation prompts or stream into the same log panel twice.
      if (this.state.installJobKey || this.state.minikubeJobRunning) return;
      // Defensive: the Install button is only rendered for uninstalled
      // tools, but guard against a stale status or race condition.
      const tool = (this.state.tools || []).find((t) => t.key === key);
      if (!tool || tool.installed) return;
      this.state.installJobKey = key;
      this.state.logBuffer = [];
      const label = tool ? tool.label : key;
      this._showGlobalLog("installing " + label, "streaming…");
      this.renderDocsCards();
      let res;
      try {
        res = await window.pywebview.api.install_tool(key);
      } catch (e) {
        res = { ok: false, error: String(e) };
      }
      this.state.installJobKey = null;
      if (res && res.ok) {
        this.showToast(label + " installed", "ok");
        // Drop the streaming log (it's already shown in the toast) and
        // hide the panel. Reload status so the card badge flips to
        // "installed" and the version updates.
        this.state.logBuffer = [];
        this._hideGlobalLog();
        await this.load();
      } else {
        const err = (res && res.error) || "unknown error";
        this.showToast("install " + key + " failed: " + err, "err");
        // Keep the panel + buffer visible so the user can read the
        // diagnostics. The panel is already visible from the pre-await
        // _showGlobalLog; pin it so a subsequent Re-check (which would
        // call renderCluster → _syncGlobalLog) doesn't hide it. Pin is
        // cleared automatically when the user starts a new job.
        this.state.globalLogPinned = true;
        this.renderDocsCards();
      }
    },

    _showGlobalLog(title, hint) {
      if (!this.els.globalLog) return;
      // Persist on state so _syncGlobalLog can re-show the panel with
      // the same title/hint after a re-render (e.g. Re-check click
      // during a minikube start or in-flight install) instead of
      // clobbering the verb-specific hint with a generic "streaming…".
      // A new job replaces any pinned (post-failure) log.
      this.state.globalLogTitle = title || "output";
      this.state.globalLogHint = hint || "streaming…";
      this.state.globalLogPinned = false;
      this.els.globalLogTitle.textContent = this.state.globalLogTitle;
      this.els.globalLogHint.textContent = this.state.globalLogHint;
      this.els.globalLogContent.textContent = this.state.logBuffer.join("\n");
      this.els.globalLog.hidden = false;
      document.body.classList.add("has-global-log");
      this._scrollLogToBottom();
    },

    _hideGlobalLog() {
      if (!this.els.globalLog) return;
      this.els.globalLog.hidden = true;
      this.state.globalLogTitle = null;
      this.state.globalLogHint = null;
      this.state.globalLogPinned = false;
      document.body.classList.remove("has-global-log");
    },

    // Show the global log iff a job is in flight (minikube OR install) OR
    // a post-failure log is pinned (the user is reading diagnostics).
    // Called from renderCluster/renderDocsCards after a state change so
    // panel visibility stays in sync with the latest state. The current
    // title/hint were stashed on state by _showGlobalLog (or the initial
    // _beginJobUi / _installTool call) so we can re-show with the same
    // content instead of recomputing a generic label. Since _beginJobUi
    // and _installTool are the only callers of _showGlobalLog, and both
    // set state synchronously before any render runs, the title/hint are
    // always populated when we get here. The pinned branch handles the
    // case where a job has finished (busy=false) but the log should
    // stay visible because the user is reading failure output.
    _syncGlobalLog() {
      const busy = this.state.minikubeJobRunning || !!this.state.installJobKey;
      if (busy || this.state.globalLogPinned) {
        this._showGlobalLog(this.state.globalLogTitle, this.state.globalLogHint);
      } else {
        this._hideGlobalLog();
      }
    },

    _scrollLogToBottom() {
      const log = this.els.globalLogContent;
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
