(function () {
  "use strict";

  const kubby = {
    els: {},
    state: { info: null, tools: [], cluster: null, activeTab: "cluster" },

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
        meta: id("meta"),
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

        this.state.info = await window.pywebview.api.system_info();
        this.state.tools = await window.pywebview.api.get_status();
        this.renderMeta();
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

    renderMeta() {
      const info = this.state.info;
      const pm = info.package_manager_label || "no package manager";
      const elv = info.elevation || "";
      this.els.meta.textContent = `${info.platform} · ${pm} · ${elv}`;
    },

    // ---------- Cluster tab (graph view) ----------

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

      // Graph container
      const graphWrap = document.createElement("div");
      graphWrap.className = "graph-container";
      const graphEl = document.createElement("div");
      graphEl.id = "cluster-graph";
      graphEl.className = "graph";
      graphWrap.appendChild(graphEl);
      el.appendChild(graphWrap);

      // Build graph
      this._renderGraph(graphEl, c);
    },

    _clusterOffline(error) {
      const wrap = document.createElement("div");
      wrap.className = "cluster-offline";
      wrap.innerHTML = `
        <div class="offline-icon">⎈</div>
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

    // ---- Cytoscape graph ----

    _renderGraph(container, c) {
      const elements = [];

      // Cluster root node
      elements.push({
        data: { id: "cluster", label: c.context || "cluster", type: "cluster" },
      });

      // K8s nodes
      for (const n of (c.nodes || [])) {
        const nodeId = `node:${n.name}`;
        elements.push({
          data: {
            id: nodeId,
            label: n.name,
            status: n.status,
            roles: (n.roles || []).join(", ") || "worker",
            type: "knode",
          },
        });
        // Edge: cluster → k8s node
        elements.push({
          data: { source: "cluster", target: nodeId, type: "ns-edge" },
        });
      }

      // Namespaces (pods are added dynamically on click)
      for (const ns of (c.namespaces || [])) {
        const nsId = `ns:${ns.name}`;
        const pods = ns.pods || [];
        elements.push({
          data: {
            id: nsId,
            label: ns.name,
            podCount: pods.length,
            type: "namespace",
            expanded: false,
            // Store pod data for dynamic expansion
            _pods: pods,
          },
        });

        // Edge: k8s node → namespace (or cluster → namespace if no nodes)
        const edgeSource = (c.nodes && c.nodes.length)
          ? `node:${c.nodes[0].name}`
          : "cluster";
        elements.push({
          data: { source: edgeSource, target: nsId, type: "ns-edge" },
        });
      }

      const cy = cytoscape({
        container: container,
        elements: elements,
        minZoom: 0.3,
        maxZoom: 3,
        wheelSensitivity: 0.2,
        style: [
          // Cluster root node
          {
            selector: 'node[type="cluster"]',
            style: {
              "background-color": "rgba(50, 108, 229, 0.12)",
              "border-color": "#326ce5",
              "border-width": 2,
              label: "data(label)",
              color: "#93a4ba",
              "font-size": 13,
              "font-weight": 700,
              "text-valign": "center",
              "text-transform": "uppercase",
              "letter-spacing": "1px",
              shape: "round-rectangle",
              width: 160,
              height: 50,
            },
          },
          // K8s node
          {
            selector: 'node[type="knode"]',
            style: {
              "background-color": function (ele) {
                return ele.data("status") === "Ready" ? "#2ea043" : "#d29922";
              },
              "background-opacity": 0.2,
              "border-color": function (ele) {
                return ele.data("status") === "Ready" ? "#2ea043" : "#d29922";
              },
              "border-width": 2,
              label: "data(label)",
              color: "#e6edf3",
              "font-size": 11,
              "font-weight": 600,
              "text-valign": "center",
              "text-halign": "center",
              shape: "round-rectangle",
              width: 140,
              height: 40,
            },
          },
          // Namespace nodes
          {
            selector: 'node[type="namespace"]',
            style: {
              "background-color": "#161e2c",
              "border-color": "#243042",
              "border-width": 2,
              label: function (ele) {
                const count = ele.data("podCount") || 0;
                return ele.data("label") + "  (" + count + ")";
              },
              color: "#93a4ba",
              "font-size": 12,
              "font-weight": 600,
              "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
              "text-valign": "center",
              "text-halign": "center",
              shape: "round-rectangle",
              width: 180,
              height: 44,
              "overlay-padding": 4,
              "overlay-opacity": 0,
            },
          },
          // Namespace hover
          {
            selector: 'node[type="namespace"]:active',
            style: {
              "border-color": "#326ce5",
              "overlay-color": "#326ce5",
              "overlay-opacity": 0.1,
              "overlay-padding": 6,
              cursor: "pointer",
            },
          },
          // Namespace cursor
          {
            selector: 'node[type="namespace"]',
            style: { cursor: "pointer" },
          },
          // Pod nodes (visible when expanded)
          {
            selector: 'node[type="pod"]',
            style: {
              "background-color": function (ele) {
                const s = ele.data("status");
                if (s === "Running" || s === "Succeeded") return "rgba(46, 160, 67, 0.15)";
                return "rgba(210, 153, 34, 0.15)";
              },
              "border-color": function (ele) {
                const s = ele.data("status");
                if (s === "Running" || s === "Succeeded") return "#2ea043";
                return "#d29922";
              },
              "border-width": 1.5,
              label: function (ele) {
                const name = ele.data("label");
                const trunc = name.length > 30 ? name.slice(0, 28) + "…" : name;
                return trunc;
              },
              color: "#b8c8e0",
              "font-size": 10,
              "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
              "text-valign": "center",
              "text-halign": "center",
              shape: "round-rectangle",
              width: 200,
              height: 32,
            },
          },
          // Edges: k8s node → namespace
          {
            selector: 'edge[type="ns-edge"]',
            style: {
              width: 2,
              "line-color": "#243042",
              "target-arrow-color": "#243042",
              "target-arrow-shape": "triangle",
              "arrow-scale": 0.8,
              "curve-style": "bezier",
            },
          },
          // Edges: namespace → pod (dynamically added)
          {
            selector: 'edge[type="pod-edge"]',
            style: {
              width: 1.5,
              "line-color": "#1c2638",
              "target-arrow-color": "#1c2638",
              "target-arrow-shape": "triangle",
              "arrow-scale": 0.7,
              "curve-style": "bezier",
            },
          },
        ],
        layout: {
          name: "breadthfirst",
          directed: true,
          roots: "#cluster",
          spacingFactor: 1.2,
          padding: 20,
          animate: true,
          animationDuration: 400,
        },
      });

      // Handle container sizing: Cytoscape may init before the flex layout
      // gives the container its final dimensions.
      requestAnimationFrame(function () {
        cy.resize();
        cy.fit(undefined, 30);
      });

      // Click namespace to expand/collapse pods (dynamically add/remove)
      cy.on("tap", 'node[type="namespace"]', function (evt) {
        const nsNode = evt.target;
        const nsId = nsNode.id();
        const isExpanded = nsNode.data("expanded");
        const podData = nsNode.data("_pods") || [];

        if (isExpanded) {
          // Remove pod nodes and edges from graph
          const podsToRemove = cy.nodes().filter(function (n) {
            return n.data("parentNS") === nsId;
          });
          cy.remove(podsToRemove); // also removes connected edges
          nsNode.data("expanded", false);
          nsNode.style("border-color", "#243042");
        } else {
          // Add pod nodes and edges to graph
          const newEles = [];
          for (let i = 0; i < podData.length; i++) {
            const pod = podData[i];
            const podId = "pod:" + nsNode.data("label") + ":" + pod.name;
            newEles.push({
              group: "nodes",
              data: {
                id: podId,
                label: pod.name,
                status: pod.status,
                parentNS: nsId,
                type: "pod",
              },
            });
            newEles.push({
              group: "edges",
              data: { source: nsId, target: podId, type: "pod-edge" },
            });
          }
          cy.add(newEles);
          nsNode.data("expanded", true);
          nsNode.style("border-color", "#326ce5");
        }

        // Re-layout
        cy.layout({
          name: "breadthfirst",
          directed: true,
          roots: "#cluster",
          spacingFactor: 1.2,
          padding: 20,
          animate: true,
          animationDuration: 300,
        }).run();
      });

      // Hover tooltip for pods (appended to the wrapper, not the Cytoscape container)
      const tip = document.createElement("div");
      tip.className = "graph-tooltip";
      tip.hidden = true;
      container.parentElement.appendChild(tip);

      cy.on("mouseover", 'node[type="pod"]', function (evt) {
        const d = evt.target.data();
        tip.innerHTML = `<strong>${this._esc(d.label)}</strong><br><span class="badge ${d.status === "Running" || d.status === "Succeeded" ? "ok" : "warn"}">${d.status}</span>`;
        tip.hidden = false;
        const pos = evt.renderedPosition;
        tip.style.left = pos.x + 10 + "px";
        tip.style.top = pos.y - 30 + "px";
      }.bind(this));
      cy.on("mouseout", 'node[type="pod"]', function () {
        tip.hidden = true;
      });
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
