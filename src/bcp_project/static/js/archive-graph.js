(function (global) {
  "use strict";

  var BRANCH_META = {
    semantic: { label: "Semantic similarity", short: "Semantic", color: "#dc2626" },
    project: { label: "Shared project", short: "Project", color: "#16a34a" },
    person: { label: "Shared people", short: "Person", color: "#2563eb" },
    organization: { label: "Organization", short: "Org", color: "#9333ea" },
    keyword: { label: "Shared keywords", short: "Keyword", color: "#d97706" },
    type: { label: "Document type", short: "Type", color: "#64748b" },
  };

  var BRANCH_ORDER = ["semantic", "project", "person", "organization", "keyword", "type"];

  function csrfHeaders() {
    return global.BCPCsrf && typeof global.BCPCsrf.headers === "function" ? global.BCPCsrf.headers() : {};
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function nodeMap(graph) {
    var map = {};
    (graph.nodes || []).forEach(function (node) {
      map[node.id] = node;
    });
    return map;
  }

  function pickCenterId(graph, nodes) {
    if (graph.center) return graph.center;
    var scores = {};
    (graph.edges || []).forEach(function (edge) {
      scores[edge.source] = (scores[edge.source] || 0) + 1;
      scores[edge.target] = (scores[edge.target] || 0) + 1;
    });
    var best = nodes[0] && nodes[0].id;
    var bestScore = -1;
    nodes.forEach(function (node) {
      var score = scores[node.id] || 0;
      if (score > bestScore) {
        bestScore = score;
        best = node.id;
      }
    });
    return best;
  }

  function buildNeuralTreeModel(graph) {
    var nodes = graph.nodes || [];
    if (!nodes.length) return { center: null, branches: [], nodeById: {}, edges: graph.edges || [] };

    var byId = nodeMap(graph);
    var centerId = graph.center || pickCenterId(graph, nodes);
    var branches = {};
    BRANCH_ORDER.forEach(function (type) {
      branches[type] = { type: type, docs: [], meta: BRANCH_META[type] };
    });

    var docBestBranch = {};

    (graph.edges || []).forEach(function (edge) {
      if (edge.source !== centerId && edge.target !== centerId) return;
      var otherId = edge.source === centerId ? edge.target : edge.source;
      var branchType = BRANCH_META[edge.type] ? edge.type : "semantic";
      var weight = edge.weight || 0.5;
      var existing = docBestBranch[otherId];
      if (!existing || weight > existing.weight) {
        docBestBranch[otherId] = {
          id: otherId,
          weight: weight,
          label: edge.label || branchType,
          branchType: branchType,
        };
      }
    });

    Object.keys(docBestBranch).forEach(function (otherId) {
      var entry = docBestBranch[otherId];
      var bucket = branches[entry.branchType].docs;
      bucket.push({
        id: entry.id,
        weight: entry.weight,
        label: entry.label,
      });
    });

    var activeBranches = BRANCH_ORDER.map(function (type) { return branches[type]; }).filter(function (b) {
      return b.docs.length > 0;
    });

    if (!activeBranches.length && nodes.length > 1 && centerId) {
      var typeGroups = {};
      nodes.forEach(function (node) {
        if (node.id === centerId) return;
        var key = node.doc_type || "Document";
        if (!typeGroups[key]) typeGroups[key] = [];
        typeGroups[key].push({ id: node.id, weight: 0.5, label: key });
      });
      activeBranches = Object.keys(typeGroups).slice(0, 6).map(function (key) {
        return {
          type: "type",
          meta: { label: key, short: key, color: BRANCH_META.type.color },
          docs: typeGroups[key].slice(0, 8),
        };
      });
    }

    activeBranches.forEach(function (branch) {
      branch.docs.sort(function (a, b) { return b.weight - a.weight; });
      branch.docs = branch.docs.slice(0, 12);
    });

    return {
      center: byId[centerId] || null,
      branches: activeBranches,
      nodeById: byId,
      edges: graph.edges || [],
      hasFocus: Boolean(graph.center),
    };
  }

  function groupDocumentsByType(documents) {
    var groups = {};
    documents.forEach(function (doc) {
      var key = doc.doc_type || "Other";
      if (!groups[key]) groups[key] = [];
      groups[key].push(doc);
    });
    return Object.keys(groups).sort().map(function (key) {
      return { type: key, docs: groups[key] };
    });
  }

  function connectionStats(model) {
    if (!model || !model.branches) return [];
    return model.branches.map(function (branch) {
      return {
        type: branch.type,
        label: branch.meta.short,
        color: branch.meta.color,
        count: branch.docs.length,
      };
    });
  }

  function splitTitleLines(title, maxLen) {
    var text = String(title || "").trim();
    if (!text) return ["", ""];
    if (text.length <= maxLen) return [text, ""];
    var cut = text.lastIndexOf(" ", maxLen);
    if (cut < maxLen * 0.45) cut = maxLen;
    return [text.slice(0, cut).trim(), text.slice(cut).trim()];
  }

  function NeuralTreeRenderer(container, options) {
    this.container = container;
    this.options = options || {};
    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.model = null;
    this.layout = null;
    this.selectedId = null;
    this._drag = null;

    this.shell = document.createElement("div");
    this.shell.className = "neural-tree-canvas";
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.setAttribute("class", "neural-tree-svg");
    this.stage = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.stage.setAttribute("class", "neural-tree-stage");
    this.linkLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.nodeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.linkLayer.setAttribute("class", "neural-tree-links");
    this.nodeLayer.setAttribute("class", "neural-tree-nodes");
    this.stage.appendChild(this.linkLayer);
    this.stage.appendChild(this.nodeLayer);
    this.svg.appendChild(this.stage);
    this.shell.appendChild(this.svg);
    container.insertBefore(this.shell, container.firstChild);

    this._bindInteractions();
  }

  NeuralTreeRenderer.prototype._bindInteractions = function () {
    var self = this;
    var pan = null;

    this.shell.addEventListener("wheel", function (event) {
      event.preventDefault();
      var delta = event.deltaY > 0 ? 0.92 : 1.08;
      self.scale = Math.max(0.35, Math.min(2.4, self.scale * delta));
      self._applyTransform();
    }, { passive: false });

    this.shell.addEventListener("pointerdown", function (event) {
      var nodeEl = event.target && event.target.closest
        ? event.target.closest(".neural-node-card, .nlm-node, .nlm-hub")
        : null;
      if (nodeEl) return;
      pan = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        ox: self.offsetX,
        oy: self.offsetY,
        moved: false,
      };
      self._drag = pan;
      self.shell.classList.add("is-panning");
      try {
        self.shell.setPointerCapture(event.pointerId);
      } catch (_err) {
        /* ignore */
      }
    });

    this.shell.addEventListener("pointermove", function (event) {
      if (!self._drag) return;
      var dx = event.clientX - self._drag.x;
      var dy = event.clientY - self._drag.y;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) self._drag.moved = true;
      self.offsetX = self._drag.ox + dx;
      self.offsetY = self._drag.oy + dy;
      self._applyTransform();
    });

    this.shell.addEventListener("pointerup", function () {
      self._drag = null;
      self.shell.classList.remove("is-panning");
    });
    this.shell.addEventListener("pointercancel", function () {
      self._drag = null;
      self.shell.classList.remove("is-panning");
    });
  };

  NeuralTreeRenderer.prototype._applyTransform = function () {
    this.stage.setAttribute(
      "transform",
      "translate(" + this.offsetX + "," + this.offsetY + ") scale(" + this.scale + ")"
    );
  };

  NeuralTreeRenderer.prototype.layoutTree = function (model, compact, mode) {
    if (mode === "overview" || (!model.hasFocus && !model.center)) {
      return this.layoutOverviewGrid(model, compact);
    }
    return this.layoutWorkflowTree(model, compact);
  };

  NeuralTreeRenderer.prototype.layoutWorkflowTree = function (model, compact) {
    var branches = model.branches || [];
    var layoutNodes = [];
    var layoutLinks = [];
    var centerX = compact ? 90 : 160;
    var hubX = compact ? 250 : 400;
    var leafX = compact ? 420 : 670;
    var leafSpacing = compact ? 72 : 108;
    var branchGap = compact ? 28 : 40;
    var blockHeights = branches.map(function (branch) {
      return Math.max((branch.docs || []).length, 1) * leafSpacing;
    });
    var totalHeight = blockHeights.reduce(function (sum, h) { return sum + h; }, 0) +
      Math.max(0, branches.length - 1) * branchGap;
    var cursorY = -totalHeight / 2;

    if (model.center) {
      layoutNodes.push({
        id: model.center.id,
        kind: "center",
        x: centerX,
        y: 0,
        node: model.center,
        width: compact ? 150 : 180,
        height: compact ? 86 : 100,
      });
    }

    branches.forEach(function (branch, index) {
      var docs = branch.docs || [];
      var blockHeight = Math.max(docs.length, 1) * leafSpacing;
      var hubY = cursorY + blockHeight / 2;
      var hubId = "hub-" + branch.type + "-" + index;

      layoutNodes.push({
        id: hubId,
        kind: "hub",
        x: hubX,
        y: hubY,
        branch: branch,
        width: compact ? 108 : 128,
        height: compact ? 52 : 56,
      });

      if (model.center) {
        layoutLinks.push({
          from: model.center.id,
          to: hubId,
          type: branch.type,
          stroke: "#cbd5e1",
          dashed: true,
        });
      }

      docs.forEach(function (docRef, docIndex) {
        var docNode = model.nodeById[docRef.id];
        if (!docNode) return;
        var docY = cursorY + docIndex * leafSpacing + leafSpacing / 2;
        layoutNodes.push({
          id: docNode.id,
          kind: "doc",
          x: leafX,
          y: docY,
          node: docNode,
          branch: branch,
          weight: docRef.weight,
          reason: docRef.label,
          width: compact ? 170 : 200,
          height: compact ? 78 : 88,
        });
        layoutLinks.push({
          from: hubId,
          to: docNode.id,
          type: branch.type,
          stroke: branch.meta.color,
          dashed: false,
        });
      });

      cursorY += blockHeight + branchGap;
    });

    return { nodes: layoutNodes, links: layoutLinks, mode: "workflow" };
  };

  NeuralTreeRenderer.prototype.layoutOverviewGrid = function (model, compact) {
    var nodes = Object.keys(model.nodeById || {}).map(function (id) { return model.nodeById[id]; });
    var groups = {};
    nodes.forEach(function (node) {
      var key = node.doc_type || "Other";
      if (!groups[key]) groups[key] = [];
      groups[key].push(node);
    });
    var typeKeys = Object.keys(groups).sort();
    var layoutNodes = [];
    var colWidth = compact ? 136 : 196;
    var rowHeight = compact ? 56 : 78;
    var colGap = compact ? 28 : 48;
    var rowGap = compact ? 14 : 20;
    var startX = -(typeKeys.length - 1) * (colWidth + colGap) / 2;

    typeKeys.forEach(function (type, colIndex) {
      var colX = startX + colIndex * (colWidth + colGap);
      var docs = groups[type];
      var colStartY = -((docs.length - 1) * (rowHeight + rowGap)) / 2;
      layoutNodes.push({
        id: "type-" + type,
        kind: "hub",
        x: colX,
        y: colStartY - rowHeight,
        branch: { type: "type", meta: { label: type, short: type, color: BRANCH_META.type.color }, docs: [] },
        width: colWidth,
        height: 36,
      });
      docs.forEach(function (node, rowIndex) {
        layoutNodes.push({
          id: node.id,
          kind: "doc",
          x: colX,
          y: colStartY + rowIndex * (rowHeight + rowGap),
          node: node,
          branch: { type: "type", meta: BRANCH_META.type },
          width: colWidth,
          height: rowHeight,
        });
      });
    });

    return { nodes: layoutNodes, links: [], mode: "overview" };
  };

  NeuralTreeRenderer.prototype._curvePath = function (x1, y1, x2, y2, mode) {
    if (mode === "workflow") {
      var midX = (x1 + x2) / 2;
      return "M" + x1 + " " + y1 + " C" + midX + " " + y1 + ", " + midX + " " + y2 + ", " + x2 + " " + y2;
    }
    var mx = (x1 + x2) / 2;
    var my = (y1 + y2) / 2;
    var dx = x2 - x1;
    var dy = y2 - y1;
    var cx = mx - dy * 0.18;
    var cy = my + dx * 0.18;
    return "M" + x1 + " " + y1 + " Q" + cx + " " + cy + " " + x2 + " " + y2;
  };

  NeuralTreeRenderer.prototype.render = function (graph, compact, mode) {
    this.model = buildNeuralTreeModel(graph);
    this.layout = this.layoutTree(this.model, compact, mode || (this.model.hasFocus ? "workflow" : "overview"));
    this.linkLayer.innerHTML = "";
    this.nodeLayer.innerHTML = "";
    this.shell.classList.toggle("mind-map-mode-workflow", this.layout.mode === "workflow");
    this.shell.classList.toggle("mind-map-mode-overview", this.layout.mode === "overview");

    var self = this;
    var positions = {};
    this.layout.nodes.forEach(function (item) { positions[item.id] = item; });

    var skipAnim = this.container && this.container.classList.contains("is-updating");
    this.layout.links.forEach(function (link, index) {
      var from = positions[link.from];
      var to = positions[link.to];
      if (!from || !to) return;
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", self._curvePath(from.x, from.y, to.x, to.y, self.layout.mode));
      path.setAttribute(
        "class",
        "neural-tree-link neural-link-" + link.type + (link.dashed ? " is-dashed" : " is-solid")
      );
      path.setAttribute("stroke", link.stroke || "#cbd5e1");
      if (!skipAnim) path.style.animationDelay = (index * 35) + "ms";
      self.linkLayer.appendChild(path);
    });

    this.layout.nodes.forEach(function (item, index) {
      var group = document.createElementNS("http://www.w3.org/2000/svg", "foreignObject");
      group.setAttribute("x", item.x - item.width / 2);
      group.setAttribute("y", item.y - item.height / 2);
      group.setAttribute("width", item.width);
      group.setAttribute("height", item.height);
      group.setAttribute("class", "neural-tree-node-wrap");

      var card = document.createElement("div");
      var branchType = item.branch && item.branch.type ? item.branch.type : "";
      card.className =
        "neural-node-card nlm-node neural-node-" + item.kind +
        (item.kind === "doc" ? " neural-node-" + branchType : "") +
        (item.kind === "hub" ? " nlm-hub" : "");
      card.style.animationDelay = skipAnim ? "0ms" : (index * 45) + "ms";
      card.setAttribute(
        "data-node-id",
        item.kind === "hub" && String(item.id).indexOf("type-") === 0
          ? ""
          : (item.node && item.node.id) || ""
      );

      if (item.kind === "center") {
        var centerLines = splitTitleLines(item.node.title || item.node.doc_type || "", 28);
        card.innerHTML =
          '<span class="neural-node-eyebrow">' + escapeHtml(item.node.doc_type || "Document") + "</span>" +
          "<strong>" + escapeHtml(item.node.label) + "</strong>" +
          '<span class="neural-node-sub">' + escapeHtml(centerLines[0]) + "</span>" +
          (centerLines[1] ? '<span class="neural-node-sub">' + escapeHtml(centerLines[1]) + "</span>" : "");
      } else if (item.kind === "hub") {
        card.innerHTML =
          '<span class="neural-node-eyebrow">Connection</span>' +
          "<strong>" + escapeHtml(item.branch.meta.label) + "</strong>" +
          '<span class="neural-node-sub">' + (item.branch.docs ? item.branch.docs.length : 0) + " linked</span>";
      } else {
        var leafLines = splitTitleLines(item.node.title || item.node.doc_type || "", 30);
        card.innerHTML =
          '<div class="nlm-doc-top">' +
            '<span class="neural-node-eyebrow">' + escapeHtml(item.node.doc_type || "Related") + "</span>" +
            (item.weight
              ? '<span class="neural-node-score">' + Math.round(item.weight * 100) + "% match</span>"
              : "") +
          "</div>" +
          "<strong>" + escapeHtml(item.node.label) + "</strong>" +
          '<span class="neural-node-sub">' + escapeHtml(leafLines[0]) + "</span>" +
          (leafLines[1] ? '<span class="neural-node-sub">' + escapeHtml(leafLines[1]) + "</span>" : "");
      }

      if (item.kind !== "hub" || (item.node && item.node.id)) {
        if (item.node && item.node.id) {
          card.tabIndex = 0;
          card.setAttribute("role", "button");
          card.setAttribute("aria-label", (item.node.label || item.node.id) + " — click details, double-click to focus");
          card.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            self.selectNode(item.node.id);
          });
          card.addEventListener("dblclick", function (event) {
            event.preventDefault();
            event.stopPropagation();
            if (typeof self.options.onFocus === "function") {
              self.options.onFocus(item.node.id);
            } else {
              self.selectNode(item.node.id);
            }
          });
          card.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              self.selectNode(item.node.id);
            }
          });
        }
      }

      group.appendChild(card);
      self.nodeLayer.appendChild(group);
    });

    var selfFit = this;
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        selfFit.fitToView();
      });
    });
  };

  NeuralTreeRenderer.prototype.selectNode = function (nodeId) {
    this.selectedId = nodeId;
    this.nodeLayer.querySelectorAll(".neural-node-card").forEach(function (card) {
      card.classList.toggle("is-selected", card.getAttribute("data-node-id") === nodeId);
    });
    if (typeof this.options.onSelect === "function") {
      var node = this.model.nodeById[nodeId];
      if (node) this.options.onSelect(node, this.model);
    }
  };

  NeuralTreeRenderer.prototype.fitToView = function () {
    if (!this.layout || !this.layout.nodes.length) return;
    var rect = this.shell.getBoundingClientRect();
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    this.layout.nodes.forEach(function (node) {
      minX = Math.min(minX, node.x - node.width / 2);
      minY = Math.min(minY, node.y - node.height / 2);
      maxX = Math.max(maxX, node.x + node.width / 2);
      maxY = Math.max(maxY, node.y + node.height / 2);
    });
    if (!rect.width || !rect.height) return;
    var padX = 180;
    var padY = 140;
    var graphW = Math.max(maxX - minX + padX, 1);
    var graphH = Math.max(maxY - minY + padY, 1);
    var scale = Math.min((rect.width - 24) / graphW, (rect.height - 24) / graphH, 1.05);
    this.scale = Math.max(0.38, Math.min(1.05, scale));
    this.offsetX = rect.width / 2 - ((minX + maxX) / 2) * this.scale;
    this.offsetY = rect.height / 2 - ((minY + maxY) / 2) * this.scale;
    this._applyTransform();
  };

  NeuralTreeRenderer.prototype.zoomBy = function (factor) {
    this.scale = Math.max(0.35, Math.min(2.4, this.scale * factor));
    this._applyTransform();
  };

  NeuralTreeRenderer.prototype.destroy = function () {
    if (this.shell && this.shell.parentNode) {
      this.shell.parentNode.removeChild(this.shell);
    }
  };

  function redirectIfUnauthorized(res) {
    if (res && res.status === 401) {
      window.location.href = "/login";
      throw new Error("Session expired — please sign in again");
    }
    return res;
  }

  function fetchGraph(params) {
    var query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.min_similarity) query.set("min_similarity", String(params.min_similarity));
    if (params.focus) query.set("focus", params.focus);
    var opts = {
      credentials: "same-origin",
      headers: csrfHeaders(),
    };
    if (params.signal) opts.signal = params.signal;
    return fetch("/api/archive/graph?" + query.toString(), opts)
      .then(redirectIfUnauthorized)
      .then(function (res) {
        if (!res.ok) throw new Error("Could not load mind map (" + res.status + ")");
        return res.json();
      });
  }

  function fetchArchiveDocuments(options) {
    var opts = options || {};
    var params = new URLSearchParams();
    params.set("limit", String(opts.limit || 40));
    params.set("offset", String(opts.offset || 0));
    if (opts.q) params.set("q", opts.q);
    if (opts.doc_type) params.set("doc_type", opts.doc_type);
    return fetch("/api/archive/documents?" + params.toString(), {
      credentials: "same-origin",
      headers: csrfHeaders(),
    }).then(redirectIfUnauthorized).then(function (res) {
      if (!res.ok) throw new Error("Could not load documents (" + res.status + ")");
      return res.json();
    });
  }

  function fetchRelated(docId, params) {
    var query = new URLSearchParams();
    if (params && params.limit) query.set("limit", String(params.limit));
    if (params && params.min_similarity) query.set("min_similarity", String(params.min_similarity));
    return fetch("/api/documents/" + encodeURIComponent(docId) + "/related?" + query.toString(), {
      credentials: "same-origin",
      headers: csrfHeaders(),
    }).then(redirectIfUnauthorized).then(function (res) {
      if (!res.ok) throw new Error("Could not load related documents (" + res.status + ")");
      return res.json();
    });
  }

  function connectionsForNode(model, nodeId) {
    return (model.edges || []).filter(function (edge) {
      return edge.source === nodeId || edge.target === nodeId;
    }).map(function (edge) {
      var other = edge.source === nodeId ? edge.target : edge.source;
      var meta = BRANCH_META[edge.type] || { label: edge.type };
      return { other: other, type: edge.type, label: meta.label, weight: edge.weight };
    });
  }

  function initMapPage(config) {
    var root = document.querySelector('[data-page="archive-map"]');
    if (!root) return;

    var container = document.getElementById(config.containerId);
    var emptyEl = document.getElementById(config.emptyId);
    var statusEl = document.getElementById(config.statusId);
    var sidebar = document.getElementById(config.sidebarId);
    var layoutEl = document.getElementById("mindMapLayout");
    var focusInput = document.getElementById(config.focusInputId);
    var focusLabel = document.getElementById(config.focusLabelId);
    var limitSelect = document.getElementById(config.limitSelectId);
    var similaritySelect = document.getElementById(config.similaritySelectId);
    var reloadBtn = document.getElementById(config.reloadBtnId);
    var clearFocusBtn = document.getElementById(config.clearFocusBtnId);
    var docSearch = document.getElementById(config.docSearchId);
    var docList = document.getElementById(config.docListId);
    var docCount = document.getElementById(config.docCountId);
    var docEmpty = document.getElementById(config.docEmptyId);
    var docTypeFilters = document.getElementById(config.docTypeFiltersId);
    var workflowSteps = document.getElementById(config.workflowStepsId);
    var workflowTitle = document.getElementById(config.workflowTitleId);
    var workflowLead = document.getElementById(config.workflowLeadId);
    var workflowGroups = document.getElementById(config.workflowGroupsId);
    var connectionStatsEl = document.getElementById(config.connectionStatsId);
    var overviewGrid = document.getElementById(config.overviewGridId);
    var viewTabs = root.querySelectorAll(".mind-map-view-tab");
    var viewPanels = root.querySelectorAll(".mind-map-visual-panel");
    var renderer = null;
    var lastModel = null;
    var lastGraph = null;
    var allDocuments = [];
    var filteredDocuments = [];
    var docTypes = [];
    var archiveTotal = 0;
    var listOffset = 0;
    var listHasMore = false;
    var listLoading = false;
    var pageSize = 40;
    var activeDocTypeFilter = "";
    var activeView = "workflow";
    var focusedDocId = (config.initialFocus || "").trim();
    var searchTimer = null;
    var searchQuery = "";
    var loadMoreBtn = null;
    var mapAbort = null;
    var mapSeq = 0;

    if (focusInput && focusedDocId) {
      focusInput.value = focusedDocId;
    }

    function setStatus(text) {
      if (statusEl) statusEl.textContent = text;
    }

    function setEmptyState(kind) {
      if (!emptyEl) return;
      var titleEl = document.getElementById("neuralTreeEmptyTitle");
      var leadEl = document.getElementById("neuralTreeEmptyLead");
      var actionEl = document.getElementById("neuralTreeEmptyAction");
      var copy = {
        loading: {
          title: "Mapping connections",
          lead: "Finding related records by meaning, project, people, and keywords.",
        },
        vacant: {
          title: "No archive records yet",
          lead: "Upload documents to start mapping how records connect.",
        },
        idle: {
          title: "Choose a document",
          lead: "Search or pick a record — the map shows links by summary meaning, projects, people, org, and keywords.",
        },
        disconnected: {
          title: "No connections found",
          lead: "Try a lower similarity threshold or raise Max related.",
        },
      };
      if (!kind) {
        emptyEl.hidden = true;
        emptyEl.classList.remove("is-loading");
        if (container) container.classList.remove("is-empty", "is-loading");
        return;
      }
      var next = copy[kind] || copy.idle;
      if (titleEl) titleEl.textContent = next.title;
      if (leadEl) leadEl.textContent = next.lead;
      if (actionEl) actionEl.hidden = kind !== "vacant";
      emptyEl.hidden = false;
      emptyEl.classList.toggle("is-loading", kind === "loading");
      if (container) {
        container.classList.add("is-empty");
        container.classList.toggle("is-loading", kind === "loading");
      }
    }

    function updateFocusLabel() {
      if (!focusLabel) return;
      if (!focusedDocId) {
        focusLabel.textContent = "None selected";
        return;
      }
      var match = allDocuments.find(function (doc) { return doc.doc_id === focusedDocId; });
      focusLabel.textContent = match
        ? match.doc_id + " · " + (match.title || match.doc_type)
        : focusedDocId;
    }

    function updateWorkflowSteps(model) {
      if (!workflowSteps) return;
      workflowSteps.querySelectorAll(".mind-map-step").forEach(function (step) {
        step.classList.remove("is-active", "is-done");
      });
      var selectStep = workflowSteps.querySelector('[data-step="select"]');
      var connectStep = workflowSteps.querySelector('[data-step="connect"]');
      var relatedStep = workflowSteps.querySelector('[data-step="related"]');
      if (!focusedDocId) {
        if (selectStep) selectStep.classList.add("is-active");
        return;
      }
      if (selectStep) selectStep.classList.add("is-done");
      if (!model || !model.branches || !model.branches.length) {
        if (connectStep) connectStep.classList.add("is-active");
        return;
      }
      if (connectStep) connectStep.classList.add("is-done");
      if (relatedStep) relatedStep.classList.add("is-active");
    }

    function renderConnectionStats(model) {
      if (!connectionStatsEl) return;
      var stats = connectionStats(model);
      if (!stats.length) {
        connectionStatsEl.innerHTML = "";
        connectionStatsEl.hidden = true;
        return;
      }
      connectionStatsEl.hidden = false;
      connectionStatsEl.innerHTML = stats.map(function (stat) {
        return (
          '<span class="nlm-keyword-tag" style="--stat-color:' + stat.color + '">' +
          '<span class="nlm-keyword-dot"></span>' +
          escapeHtml(stat.label) + " · " + stat.count +
          "</span>"
        );
      }).join("");
    }

    function renderWorkflowPanel(model) {
      if (!workflowGroups) return;
      if (!model || !model.center) {
        if (workflowTitle) workflowTitle.textContent = "How relations work";
        if (workflowLead) {
          workflowLead.textContent = "Pick a document to map links across the full archive.";
        }
        workflowGroups.innerHTML =
          '<div class="mind-map-workflow-placeholder mind-map-relation-guide">' +
          "<p>Documents connect when they share:</p>" +
          "<ul>" +
          "<li><strong>Semantic</strong> — similar summary meaning (AI embeddings)</li>" +
          "<li><strong>Project</strong> — same major project in the summary</li>" +
          "<li><strong>Person</strong> — same key person named</li>" +
          "<li><strong>Organization</strong> — same organization</li>" +
          "<li><strong>Keyword</strong> — shared searchable keywords</li>" +
          "</ul>" +
          '<p class="mind-map-relation-tip">Click a related node for details · Double-click to make it the focus</p>' +
          "</div>";
        updateWorkflowSteps(null);
        renderConnectionStats(null);
        return;
      }

      if (workflowTitle) workflowTitle.textContent = model.center.label || model.center.id;
      if (workflowLead) {
        workflowLead.textContent = [model.center.doc_type, model.center.doc_date, model.center.title]
          .filter(Boolean)
          .join(" · ");
      }

      if (!model.branches.length) {
        workflowGroups.innerHTML = '<p class="mind-map-workflow-empty">No related documents found for the current Max related / similarity settings.</p>';
      } else {
        workflowGroups.innerHTML = model.branches.map(function (branch) {
          var docsHtml = branch.docs.map(function (docRef) {
            var node = model.nodeById[docRef.id] || {};
            return (
              '<button type="button" class="mind-map-workflow-doc" data-doc-id="' + escapeHtml(docRef.id) + '" title="Click to inspect · Double-click to focus">' +
              '<span class="mind-map-workflow-doc-id">' + escapeHtml(docRef.id) + "</span>" +
              '<span class="mind-map-workflow-doc-title">' + escapeHtml(node.title || node.doc_type || "Document") + "</span>" +
              '<span class="mind-map-workflow-doc-reason">' + escapeHtml(docRef.label || branch.meta.label) +
              (docRef.weight ? " · " + Math.round(docRef.weight * 100) + "%" : "") +
              "</span></button>"
            );
          }).join("");
          return (
            '<section class="mind-map-workflow-group mind-map-workflow-' + branch.type + '">' +
            '<header><i class="edge-dot edge-' + branch.type + '"></i>' +
            "<strong>" + escapeHtml(branch.meta.label) + "</strong>" +
            '<span>' + branch.docs.length + " documents</span></header>" +
            '<div class="mind-map-workflow-docs">' + docsHtml + "</div></section>"
          );
        }).join("");
      }

      updateWorkflowSteps(model);
      renderConnectionStats(model);
    }

    function renderOverviewGrid(types) {
      if (!overviewGrid) return;
      var groups = types && types.length ? types : docTypes;
      if (!groups.length) {
        overviewGrid.innerHTML = '<p class="mind-map-overview-empty">No documents in the archive yet.</p>';
        return;
      }
      overviewGrid.innerHTML =
        '<p class="mind-map-overview-lead">Archive by type — click a type to filter the document list (works at 10k+ scale).</p>' +
        '<div class="mind-map-overview-cards mind-map-overview-types">' +
        groups.map(function (group) {
          var type = group.doc_type || group.type;
          var count = group.count != null ? group.count : (group.docs ? group.docs.length : 0);
          var active = activeDocTypeFilter === type ? " is-focused" : "";
          return (
            '<button type="button" class="mind-map-overview-card' + active + '" data-doc-type="' + escapeHtml(type) + '">' +
            '<span class="mind-map-doc-title">' + escapeHtml(type) + "</span>" +
            '<span class="mind-map-doc-meta">' + count + " documents</span></button>"
          );
        }).join("") +
        "</div>";
    }

    function renderDocTypeFilters() {
      if (!docTypeFilters) return;
      var types = (docTypes || []).map(function (row) { return row.doc_type; }).filter(Boolean);
      docTypeFilters.innerHTML =
        '<button type="button" class="mind-map-type-chip' + (activeDocTypeFilter ? "" : " is-active") + '" data-type="">All types</button>' +
        types.map(function (type) {
          var active = activeDocTypeFilter === type ? " is-active" : "";
          var countRow = docTypes.find(function (row) { return row.doc_type === type; });
          var label = type + (countRow ? " (" + countRow.count + ")" : "");
          return '<button type="button" class="mind-map-type-chip' + active + '" data-type="' + escapeHtml(type) + '">' + escapeHtml(label) + "</button>";
        }).join("");
    }

    function syncFocusUrl() {
      var params = new URLSearchParams(window.location.search);
      if (focusedDocId) {
        params.set("focus", focusedDocId);
      } else {
        params.delete("focus");
      }
      var query = params.toString();
      var next = window.location.pathname + (query ? "?" + query : "");
      if (next !== window.location.pathname + window.location.search) {
        window.history.replaceState({}, "", next);
      }
    }

    function setFocus(docId, reload) {
      var next = (docId || "").trim();
      if (next && next === focusedDocId) {
        if (focusInput) focusInput.value = focusedDocId;
        updateFocusLabel();
        syncFocusUrl();
        inspectDocument(next);
        setActiveView("workflow");
        return;
      }
      focusedDocId = next;
      if (focusInput) focusInput.value = focusedDocId;
      updateFocusLabel();
      syncFocusUrl();
      highlightDocumentList(focusedDocId);
      setActiveView("workflow");
      if (reload !== false) loadMap();
    }

    function highlightDocumentList(docId) {
      if (!docList) return;
      docList.querySelectorAll(".mind-map-doc-item").forEach(function (btn) {
        var id = btn.getAttribute("data-doc-id");
        var isFocus = Boolean(docId) && id === docId;
        btn.classList.toggle("is-focused", isFocus);
        btn.classList.toggle("is-selected", isFocus);
        btn.setAttribute("aria-selected", isFocus ? "true" : "false");
      });
    }

    function cardToGraphNode(card, isCenter) {
      return {
        id: card.doc_id,
        label: card.doc_id,
        title: card.title || card.doc_type || "Document",
        doc_type: card.doc_type || "Document",
        doc_date: card.doc_date || "",
        organization: card.organization || "",
        projects: card.projects || [],
        is_center: Boolean(isCenter),
      };
    }

    function paintPreviewFocus(docId) {
      var card = allDocuments.find(function (doc) { return doc.doc_id === docId; });
      if (!card) return;
      var preview = {
        center: docId,
        count: 1,
        edge_count: 0,
        nodes: [cardToGraphNode(card, true)],
        edges: [],
      };
      if (!renderer) {
        renderer = new NeuralTreeRenderer(container, bindRendererHandlers());
      }
      if (container) container.classList.add("is-updating");
      renderer.render(preview, false, "workflow");
      if (workflowTitle) workflowTitle.textContent = card.doc_id;
      if (workflowLead) {
        workflowLead.textContent = [card.doc_type, card.doc_date, card.title].filter(Boolean).join(" · ");
      }
    }

    function bindRendererHandlers() {
      return {
        onSelect: function (node, model) {
          lastModel = model;
          showSidebar(node, model);
        },
        onFocus: function (docId) {
          setFocus(docId);
          setActiveView("workflow");
        },
      };
    }

    function inspectDocument(docId) {
      var id = (docId || "").trim();
      if (!id) return;
      var model = lastModel;
      var node = model && model.nodeById ? model.nodeById[id] : null;
      if (!node) {
        var card = allDocuments.find(function (doc) { return doc.doc_id === id; });
        if (card) {
          node = {
            id: card.doc_id,
            label: card.doc_id,
            title: card.title,
            doc_type: card.doc_type,
            doc_date: card.doc_date,
            organization: card.organization || "",
            projects: [],
          };
        }
      }
      if (node) showSidebar(node, model || { edges: [], nodeById: {} });
      if (renderer && model && model.nodeById && model.nodeById[id]) {
        renderer.selectNode(id);
      } else {
        renderDocumentList(id);
      }
    }

    function hideSidebar() {
      if (sidebar) sidebar.hidden = true;
      if (layoutEl) layoutEl.classList.remove("has-detail");
      renderDocumentList(focusedDocId || undefined);
    }

    function showSidebar(node, model) {
      if (!sidebar || !node) return;
      sidebar.hidden = false;
      if (layoutEl) layoutEl.classList.add("has-detail");
      var title = document.getElementById("mapSidebarTitle");
      var meta = document.getElementById("mapSidebarMeta");
      var projects = document.getElementById("mapSidebarProjects");
      var tags = document.getElementById("mapSidebarTags");
      var connections = document.getElementById("mapSidebarConnections");
      var view = document.getElementById("mapSidebarView");
      var focusBtn = document.getElementById("mapSidebarFocus");

      if (title) title.textContent = node.label || node.id;
      if (meta) meta.textContent = [node.doc_type, node.doc_date, node.organization].filter(Boolean).join(" · ");
      if (tags) {
        var tagItems = [node.doc_type, node.organization].filter(Boolean);
        tags.innerHTML = tagItems
          .map(function (t) { return '<span class="neural-tag">' + escapeHtml(t) + "</span>"; })
          .join("");
      }
      if (projects) {
        projects.innerHTML = (node.projects || [])
          .map(function (p) { return "<li>" + escapeHtml(p) + "</li>"; })
          .join("") || "<li class='muted'>No projects extracted</li>";
      }
      if (connections) {
        var links = connectionsForNode(model, node.id);
        connections.innerHTML = links.length
          ? links.slice(0, 8).map(function (link) {
              return (
                "<li><strong>" + escapeHtml(link.other) + "</strong>" +
                "<span>" + escapeHtml(link.label) + " · " + Math.round((link.weight || 0) * 100) + "%</span></li>"
              );
            }).join("")
          : "<li class='muted'>No direct connections in current map</li>";
      }
      if (view) {
        view.href = "/view/" + encodeURIComponent(node.id) +
          "?from=" + encodeURIComponent("/archive/map" + (focusedDocId ? "?focus=" + focusedDocId : ""));
      }
      if (focusBtn) {
        var isAlreadyFocused = node.id === focusedDocId;
        focusBtn.hidden = isAlreadyFocused;
        focusBtn.textContent = "Set as workflow focus";
        focusBtn.onclick = function () {
          setFocus(node.id);
        };
      }
      renderDocumentList(node.id);
    }

    function renderDocumentList(activeId, options) {
      if (!docList) return;
      var highlightId = activeId || focusedDocId;
      var groups = groupDocumentsByType(filteredDocuments);
      var html = groups.map(function (group) {
        var items = group.docs.map(function (doc) {
          var selected = doc.doc_id === highlightId ? " is-selected" : "";
          var focused = doc.doc_id === focusedDocId ? " is-focused" : "";
          return (
            '<button type="button" class="mind-map-doc-item' + selected + focused + '" data-doc-id="' +
            escapeHtml(doc.doc_id) + '" role="option" aria-selected="' + (doc.doc_id === highlightId ? "true" : "false") + '">' +
            '<span class="mind-map-doc-id">' + escapeHtml(doc.doc_id) + "</span>" +
            '<span class="mind-map-doc-title">' + escapeHtml(doc.title || doc.doc_type) + "</span>" +
            '<span class="mind-map-doc-meta">' + escapeHtml([doc.doc_type, doc.doc_date].filter(Boolean).join(" · ")) + "</span>" +
            "</button>"
          );
        }).join("");
        return (
          '<section class="mind-map-doc-group">' +
          '<h3 class="mind-map-doc-group-title">' + escapeHtml(group.type) + " <span>" + group.docs.length + "</span></h3>" +
          items +
          "</section>"
        );
      }).join("");

      if (listHasMore) {
        html += '<button type="button" class="button button-secondary mind-map-load-more" id="mapDocLoadMore">Load more documents</button>';
      }
      docList.innerHTML = html || "";
      loadMoreBtn = document.getElementById("mapDocLoadMore");
      if (loadMoreBtn) {
        loadMoreBtn.addEventListener("click", function () {
          loadDocuments({ append: true });
        });
      }

      if (docCount) {
        docCount.textContent = filteredDocuments.length + " shown · " + archiveTotal + " in archive" +
          (searchQuery || activeDocTypeFilter ? " (filtered)" : "");
      }
      if (docEmpty) {
        docEmpty.hidden = filteredDocuments.length > 0 || listLoading;
      }

      if (highlightId && options && options.scroll) {
        var escaped = highlightId.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
        var activeBtn = docList.querySelector('.mind-map-doc-item[data-doc-id="' + escaped + '"]');
        if (activeBtn && typeof activeBtn.scrollIntoView === "function") {
          activeBtn.scrollIntoView({ block: "nearest", behavior: "auto" });
        }
      }
    }

    function setActiveView(view) {
      activeView = view;
      viewTabs.forEach(function (tab) {
        var isActive = tab.getAttribute("data-view") === view;
        tab.classList.toggle("is-active", isActive);
        tab.setAttribute("aria-selected", isActive ? "true" : "false");
      });
      viewPanels.forEach(function (panel) {
        var show = panel.getAttribute("data-panel") === view;
        panel.classList.toggle("is-active", show);
        panel.hidden = !show;
      });
      if (view === "workflow" && lastGraph && focusedDocId) {
        setEmptyState(null);
        if (!renderer) {
          renderer = new NeuralTreeRenderer(container, bindRendererHandlers());
          renderer.render(lastGraph, false, "workflow");
        }
      } else if (view === "workflow" && !focusedDocId) {
        setEmptyState(archiveTotal ? "idle" : "vacant");
      } else if (view === "overview") {
        renderOverviewGrid(docTypes);
      }
    }

    function applyDocumentPayload(payload, append) {
      var incoming = payload.documents || [];
      archiveTotal = payload.total != null ? payload.total : incoming.length;
      listHasMore = Boolean(payload.has_more);
      listOffset = (payload.offset || 0) + incoming.length;
      if (payload.types) docTypes = payload.types;
      if (append) {
        var seen = {};
        allDocuments.forEach(function (doc) { seen[doc.doc_id] = true; });
        incoming.forEach(function (doc) {
          if (!seen[doc.doc_id]) allDocuments.push(doc);
        });
      } else {
        allDocuments = incoming.slice();
      }
      filteredDocuments = allDocuments.slice();
      renderDocTypeFilters();
      updateFocusLabel();
      renderDocumentList(focusedDocId || undefined);
      renderOverviewGrid(docTypes);
    }

    function loadDocuments(options) {
      var opts = options || {};
      var append = Boolean(opts.append);
      if (listLoading) return Promise.resolve();
      listLoading = true;
      if (!append && docCount) docCount.textContent = "Loading documents…";
      if (loadMoreBtn) {
        loadMoreBtn.disabled = true;
        loadMoreBtn.textContent = "Loading…";
      }
      return fetchArchiveDocuments({
        q: searchQuery,
        doc_type: activeDocTypeFilter,
        limit: pageSize,
        offset: append ? listOffset : 0,
      })
        .then(function (payload) {
          applyDocumentPayload(payload, append);
          if (!focusedDocId) {
            setEmptyState(archiveTotal ? "idle" : "vacant");
            renderWorkflowPanel(null);
            setStatus(archiveTotal + " documents in archive · search or select one to map");
            if (activeView !== "overview") setActiveView("overview");
          }
        })
        .catch(function (err) {
          if (docCount) docCount.textContent = err.message || "Could not load documents";
          if (!append) {
            if (docList) docList.innerHTML = "";
            if (docEmpty) {
              docEmpty.hidden = false;
              docEmpty.textContent = err.message || "Could not load documents";
            }
            setEmptyState("vacant");
          }
        })
        .finally(function () {
          listLoading = false;
        });
    }

    function loadMap() {
      if (mapAbort) {
        try { mapAbort.abort(); } catch (err) {}
      }
      if (!focusedDocId) {
        mapSeq += 1;
        if (renderer) {
          renderer.destroy();
          renderer = null;
        }
        lastGraph = null;
        lastModel = null;
        if (container) container.classList.remove("is-updating");
        setEmptyState(archiveTotal ? "idle" : "vacant");
        renderWorkflowPanel(null);
        setStatus(archiveTotal + " documents in archive · search or select one to map");
        setActiveView("overview");
        return;
      }
      var requestedFocus = focusedDocId;
      var seq = ++mapSeq;
      mapAbort = window.AbortController ? new AbortController() : null;
      setActiveView("workflow");
      paintPreviewFocus(requestedFocus);
      setStatus("Mapping " + requestedFocus + "…");
      if (!renderer) setEmptyState("loading");
      else setEmptyState(null);
      fetchGraph({
        limit: limitSelect ? Number(limitSelect.value) : 24,
        min_similarity: similaritySelect ? Number(similaritySelect.value) : 0.68,
        focus: requestedFocus,
        signal: mapAbort ? mapAbort.signal : undefined,
      })
        .then(function (graph) {
          if (seq !== mapSeq || focusedDocId !== requestedFocus) return;
          lastGraph = graph;
          if (!graph.count) {
            if (renderer) {
              renderer.destroy();
              renderer = null;
            }
            setEmptyState("disconnected");
            renderWorkflowPanel(null);
            setStatus("No connections found for " + requestedFocus);
            return;
          }
          setEmptyState(null);
          if (!renderer) {
            renderer = new NeuralTreeRenderer(container, bindRendererHandlers());
          }
          renderer.render(graph, false, "workflow");
          lastModel = buildNeuralTreeModel(graph);
          if (focusInput) focusInput.value = requestedFocus;
          updateFocusLabel();
          highlightDocumentList(requestedFocus);
          renderWorkflowPanel(lastModel);
          if (layoutEl) layoutEl.classList.add("has-workflow");
          window.requestAnimationFrame(function () {
            if (renderer && focusedDocId === requestedFocus) {
              renderer.selectedId = requestedFocus;
              renderer.nodeLayer.querySelectorAll(".neural-node-card").forEach(function (card) {
                card.classList.toggle("is-selected", card.getAttribute("data-node-id") === requestedFocus);
              });
            }
          });
          var statusBits = [
            graph.count + " on map",
            graph.edge_count + " links",
            "focused on " + requestedFocus,
          ];
          if (lastModel && !lastModel.branches.length) statusBits.push("no connections in current settings");
          setStatus(statusBits.join(" · "));
        })
        .catch(function (err) {
          if (err && (err.name === "AbortError" || err.message === "The user aborted a request.")) return;
          if (seq !== mapSeq) return;
          setStatus(err.message || "Failed to load mind map");
          setEmptyState(archiveTotal ? "idle" : "vacant");
          renderWorkflowPanel(null);
        })
        .finally(function () {
          if (seq === mapSeq && container) container.classList.remove("is-updating");
        });
    }

    if (docList) {
      docList.addEventListener("click", function (event) {
        var button = event.target.closest(".mind-map-doc-item");
        if (!button) return;
        var docId = button.getAttribute("data-doc-id") || "";
        if (docId && docId === focusedDocId) {
          inspectDocument(docId);
          setActiveView("workflow");
          return;
        }
        setFocus(docId);
        setActiveView("workflow");
      });
    }

    if (workflowGroups) {
      var lastWorkflowClick = { id: "", at: 0 };
      workflowGroups.addEventListener("click", function (event) {
        var button = event.target.closest(".mind-map-workflow-doc");
        if (!button) return;
        var docId = button.getAttribute("data-doc-id") || "";
        var now = Date.now();
        if (docId && docId === lastWorkflowClick.id && now - lastWorkflowClick.at < 400) {
          setFocus(docId);
          return;
        }
        lastWorkflowClick = { id: docId, at: now };
        inspectDocument(docId);
      });
    }

    if (overviewGrid) {
      overviewGrid.addEventListener("click", function (event) {
        var button = event.target.closest(".mind-map-overview-card");
        if (!button) return;
        var type = button.getAttribute("data-doc-type") || "";
        activeDocTypeFilter = type;
        listOffset = 0;
        renderDocTypeFilters();
        loadDocuments({ append: false });
        setActiveView("overview");
      });
    }

    if (docTypeFilters) {
      docTypeFilters.addEventListener("click", function (event) {
        var chip = event.target.closest(".mind-map-type-chip");
        if (!chip) return;
        activeDocTypeFilter = chip.getAttribute("data-type") || "";
        listOffset = 0;
        renderDocTypeFilters();
        loadDocuments({ append: false });
      });
    }

    viewTabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        setActiveView(tab.getAttribute("data-view") || "workflow");
      });
    });

    if (docSearch) {
      docSearch.addEventListener("input", function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(function () {
          searchQuery = docSearch.value.trim();
          listOffset = 0;
          loadDocuments({ append: false });
        }, 280);
      });
    }

    if (reloadBtn) {
      reloadBtn.addEventListener("click", function () {
        listOffset = 0;
        loadDocuments({ append: false }).then(function () {
          if (focusedDocId) loadMap();
        });
      });
    }
    if (limitSelect) limitSelect.addEventListener("change", function () { if (focusedDocId) loadMap(); });
    if (similaritySelect) similaritySelect.addEventListener("change", function () { if (focusedDocId) loadMap(); });
    if (clearFocusBtn) {
      clearFocusBtn.addEventListener("click", function () {
        setFocus("");
        setActiveView("overview");
      });
    }
    var closeBtn = document.getElementById(config.sidebarCloseId);
    if (closeBtn) closeBtn.addEventListener("click", hideSidebar);
    var zoomIn = document.getElementById(config.zoomInId);
    var zoomOut = document.getElementById(config.zoomOutId);
    var zoomFit = document.getElementById(config.zoomFitId);
    if (zoomIn) zoomIn.addEventListener("click", function () { if (renderer) renderer.zoomBy(1.12); });
    if (zoomOut) zoomOut.addEventListener("click", function () { if (renderer) renderer.zoomBy(0.88); });
    if (zoomFit) zoomFit.addEventListener("click", function () { if (renderer) renderer.fitToView(); });

    window.addEventListener("resize", function () {
      if (renderer) renderer.fitToView();
    });

    hideSidebar();
    renderWorkflowPanel(null);
    if (focusedDocId) {
      setEmptyState("loading");
    } else {
      setEmptyState("idle");
    }
    loadDocuments({ append: false }).then(function () {
      if (focusedDocId) loadMap();
    });
  }

  function initViewerPanel(config) {
    var panel = document.getElementById(config.panelId);
    if (!panel) return;

    var docId = panel.getAttribute("data-doc-id");
    var graphEl = document.getElementById(config.graphId);
    var listEl = document.getElementById(config.listId);
    var statusEl = document.getElementById(config.statusId);
    if (!docId || !graphEl) return;

    function setStatus(text) {
      if (statusEl) statusEl.textContent = text;
    }

    fetchRelated(docId, { limit: 10, min_similarity: 0.65 })
      .then(function (payload) {
        var graph = payload.graph || { nodes: [], edges: [], center: docId };
        graph.center = docId;
        var renderer = new NeuralTreeRenderer(graphEl, {});
        renderer.render(graph, true);

        if (listEl) {
          var items = payload.related || [];
          if (!items.length) {
            listEl.innerHTML = '<li class="muted">No related branches yet.</li>';
          } else {
            listEl.innerHTML = items.map(function (item) {
              var reasons = (item.reasons || []).slice(0, 2).join(" · ");
              return (
                '<li><a href="/view/' + encodeURIComponent(item.doc_id) +
                "?from=" + encodeURIComponent("/view/" + docId) + '">' +
                "<strong>" + escapeHtml(item.doc_id) + "</strong>" +
                "<span>" + escapeHtml([item.doc_type, reasons].filter(Boolean).join(" · ")) + "</span>" +
                (item.score ? '<em class="neural-list-score">' + Math.round(item.score * 100) + "%</em>" : "") +
                "</a></li>"
              );
            }).join("");
          }
        }
        setStatus((payload.related || []).length + " related documents");
      })
      .catch(function (err) {
        setStatus(err.message || "Could not load mind map");
      });
  }

  global.BCPArchiveGraph = {
    initMapPage: initMapPage,
    initViewerPanel: initViewerPanel,
    NeuralTreeRenderer: NeuralTreeRenderer,
    fetchGraph: fetchGraph,
    fetchArchiveDocuments: fetchArchiveDocuments,
    fetchRelated: fetchRelated,
  };
})(window);
