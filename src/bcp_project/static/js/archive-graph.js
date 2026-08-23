(function (global) {
  "use strict";

  var BRANCH_META = {
    semantic: { label: "Semantic similarity", short: "Semantic", color: "#c5922f" },
    project: { label: "Shared project", short: "Project", color: "#2d9a72" },
    person: { label: "Shared people", short: "People", color: "#5b7cfa" },
    organization: { label: "Organization", short: "Org", color: "#1a1a54" },
    keyword: { label: "Shared keywords", short: "Keywords", color: "#8a7010" },
    type: { label: "Document type", short: "Type", color: "#6b7280" },
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
    this.shell.addEventListener("wheel", function (event) {
      event.preventDefault();
      var delta = event.deltaY > 0 ? 0.92 : 1.08;
      self.scale = Math.max(0.35, Math.min(2.4, self.scale * delta));
      self._applyTransform();
    }, { passive: false });

    this.shell.addEventListener("pointerdown", function (event) {
      if (event.target.closest(".neural-node-card")) return;
      self._drag = { x: event.clientX, y: event.clientY, ox: self.offsetX, oy: self.offsetY };
      self.shell.classList.add("is-panning");
    });

    window.addEventListener("pointermove", function (event) {
      if (!self._drag) return;
      self.offsetX = self._drag.ox + (event.clientX - self._drag.x);
      self.offsetY = self._drag.oy + (event.clientY - self._drag.y);
      self._applyTransform();
    });

    window.addEventListener("pointerup", function () {
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
    var centerX = compact ? 70 : 100;
    var hubX = compact ? 210 : 300;
    var leafX = compact ? 360 : 520;
    var leafSpacing = compact ? 48 : 58;
    var branchGap = compact ? 24 : 32;
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
        width: compact ? 118 : 168,
        height: compact ? 52 : 68,
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
        width: compact ? 96 : 132,
        height: compact ? 40 : 48,
      });

      if (model.center) {
        layoutLinks.push({
          from: model.center.id,
          to: hubId,
          type: branch.type,
          stroke: branch.meta.color,
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
          width: compact ? 108 : 148,
          height: compact ? 46 : 56,
        });
        layoutLinks.push({
          from: hubId,
          to: docNode.id,
          type: branch.type,
          stroke: branch.meta.color,
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
    var colWidth = compact ? 130 : 170;
    var rowHeight = compact ? 52 : 62;
    var colGap = compact ? 24 : 36;
    var rowGap = compact ? 10 : 12;
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

    this.layout.links.forEach(function (link, index) {
      var from = positions[link.from];
      var to = positions[link.to];
      if (!from || !to) return;
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", self._curvePath(from.x, from.y, to.x, to.y, self.layout.mode));
      path.setAttribute("class", "neural-tree-link neural-link-" + link.type);
      path.setAttribute("stroke", link.stroke);
      path.style.animationDelay = (index * 35) + "ms";
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
      card.className = "neural-node-card neural-node-" + item.kind + (item.kind === "doc" ? " neural-node-" + (item.branch && item.branch.type) : "");
      card.style.animationDelay = (index * 45) + "ms";
      card.setAttribute("data-node-id", item.kind === "hub" && String(item.id).indexOf("type-") === 0 ? "" : (item.node && item.node.id) || "");

      if (item.kind === "center") {
        card.innerHTML =
          '<span class="neural-node-eyebrow">Step 1 · Focus</span>' +
          '<strong>' + escapeHtml(item.node.label) + '</strong>' +
          '<span class="neural-node-sub">' + escapeHtml(item.node.title || item.node.doc_type) + '</span>';
      } else if (item.kind === "hub") {
        card.innerHTML =
          '<span class="neural-node-eyebrow">Step 2 · ' + escapeHtml(item.branch.meta.short) + "</span>" +
          '<strong>' + escapeHtml(item.branch.meta.label) + "</strong>" +
          '<span class="neural-node-sub">' + (item.branch.docs ? item.branch.docs.length : 0) + " linked</span>";
      } else {
        card.innerHTML =
          '<span class="neural-node-eyebrow">Step 3 · Related</span>' +
          "<strong>" + escapeHtml(item.node.label) + "</strong>" +
          '<span class="neural-node-sub">' + escapeHtml(item.node.title || item.node.doc_type) + "</span>" +
          (item.reason ? '<span class="neural-node-reason">' + escapeHtml(item.reason) + "</span>" : "") +
          (item.weight ? '<span class="neural-node-score">' + Math.round(item.weight * 100) + "% match</span>" : "");
      }

      if (item.kind !== "hub" || (item.node && item.node.id)) {
        if (item.node && item.node.id) {
          card.tabIndex = 0;
          card.addEventListener("click", function (event) {
            event.stopPropagation();
            self.selectNode(item.node.id);
          });
        }
      }

      group.appendChild(card);
      self.nodeLayer.appendChild(group);
    });

    this.fitToView();
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
    var graphW = maxX - minX + 80;
    var graphH = maxY - minY + 80;
    var scale = Math.min(rect.width / graphW, rect.height / graphH, 1.2);
    this.scale = Math.max(0.45, Math.min(1.15, scale));
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

  function fetchGraph(params) {
    var query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.min_similarity) query.set("min_similarity", String(params.min_similarity));
    if (params.focus) query.set("focus", params.focus);
    return fetch("/api/archive/graph?" + query.toString(), {
      credentials: "same-origin",
      headers: csrfHeaders(),
    }).then(function (res) {
      if (!res.ok) throw new Error("Could not load mind map (" + res.status + ")");
      return res.json();
    });
  }

  function fetchArchiveDocuments(query) {
    var params = new URLSearchParams();
    params.set("limit", "500");
    if (query) params.set("q", query);
    return fetch("/api/archive/documents?" + params.toString(), {
      credentials: "same-origin",
      headers: csrfHeaders(),
    }).then(function (res) {
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
    }).then(function (res) {
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
    var activeDocTypeFilter = "";
    var activeView = "workflow";
    var focusedDocId = (config.initialFocus || "").trim();
    var searchTimer = null;

    if (focusInput && focusedDocId) {
      focusInput.value = focusedDocId;
    }

    function setStatus(text) {
      if (statusEl) statusEl.textContent = text;
    }

    function updateFocusLabel() {
      if (!focusLabel) return;
      if (!focusedDocId) {
        focusLabel.textContent = "Select a document to start";
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
        connectionStatsEl.innerHTML = focusedDocId
          ? '<span class="mind-map-stat muted">No connections found for this document</span>'
          : "";
        return;
      }
      connectionStatsEl.innerHTML = stats.map(function (stat) {
        return (
          '<span class="mind-map-stat" style="--stat-color:' + stat.color + '">' +
          '<i class="edge-dot"></i>' + escapeHtml(stat.label) + " · " + stat.count +
          "</span>"
        );
      }).join("");
    }

    function renderWorkflowPanel(model) {
      if (!workflowGroups) return;
      if (!model || !model.center) {
        if (workflowTitle) workflowTitle.textContent = "Select a document";
        if (workflowLead) workflowLead.textContent = "Choose a document to see organized connection paths.";
        workflowGroups.innerHTML =
          '<div class="mind-map-workflow-placeholder">' +
          "<p>Workflow path</p>" +
          "<ol><li>Select a document from the list</li><li>Review connection types</li><li>Open related documents</li></ol>" +
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
        workflowGroups.innerHTML = '<p class="mind-map-workflow-empty">No related documents found for the current scope and similarity settings.</p>';
      } else {
        workflowGroups.innerHTML = model.branches.map(function (branch) {
          var docsHtml = branch.docs.map(function (docRef) {
            var node = model.nodeById[docRef.id] || {};
            return (
              '<button type="button" class="mind-map-workflow-doc" data-doc-id="' + escapeHtml(docRef.id) + '">' +
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

    function renderOverviewGrid(documents) {
      if (!overviewGrid) return;
      var groups = groupDocumentsByType(documents);
      if (!groups.length) {
        overviewGrid.innerHTML = '<p class="mind-map-overview-empty">No documents in the archive yet.</p>';
        return;
      }
      overviewGrid.innerHTML = groups.map(function (group) {
        var cards = group.docs.map(function (doc) {
          var focused = doc.doc_id === focusedDocId ? " is-focused" : "";
          return (
            '<button type="button" class="mind-map-overview-card' + focused + '" data-doc-id="' + escapeHtml(doc.doc_id) + '">' +
            '<span class="mind-map-doc-id">' + escapeHtml(doc.doc_id) + "</span>" +
            '<span class="mind-map-doc-title">' + escapeHtml(doc.title || doc.doc_type) + "</span>" +
            '<span class="mind-map-doc-meta">' + escapeHtml(doc.doc_date || "") + "</span></button>"
          );
        }).join("");
        return (
          '<section class="mind-map-overview-section"><h3>' + escapeHtml(group.type) +
          ' <span>' + group.docs.length + "</span></h3><div class=\"mind-map-overview-cards\">" +
          cards + "</div></section>"
        );
      }).join("");
    }

    function renderDocTypeFilters() {
      if (!docTypeFilters) return;
      var types = [];
      allDocuments.forEach(function (doc) {
        var type = doc.doc_type || "Other";
        if (types.indexOf(type) === -1) types.push(type);
      });
      types.sort();
      docTypeFilters.innerHTML =
        '<button type="button" class="mind-map-type-chip' + (activeDocTypeFilter ? "" : " is-active") + '" data-type="">All types</button>' +
        types.map(function (type) {
          var active = activeDocTypeFilter === type ? " is-active" : "";
          return '<button type="button" class="mind-map-type-chip' + active + '" data-type="' + escapeHtml(type) + '">' + escapeHtml(type) + "</button>";
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
      focusedDocId = (docId || "").trim();
      if (focusInput) focusInput.value = focusedDocId;
      updateFocusLabel();
      syncFocusUrl();
      renderDocumentList();
      if (reload !== false) loadMap();
    }

    function hideSidebar() {
      if (sidebar) sidebar.hidden = true;
      if (layoutEl) layoutEl.classList.remove("has-detail");
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
          : "<li class='muted'>No direct connections in current scope</li>";
      }
      if (view) view.href = "/view/" + encodeURIComponent(node.id) + "?from=/archive/map";
      if (focusBtn) {
        focusBtn.onclick = function () {
          setFocus(node.id);
        };
      }
      renderDocumentList(node.id);
    }

    function filterDocuments() {
      var needle = docSearch ? docSearch.value.trim().toLowerCase() : "";
      filteredDocuments = allDocuments.filter(function (doc) {
        if (activeDocTypeFilter && (doc.doc_type || "Other") !== activeDocTypeFilter) return false;
        if (!needle) return true;
        return (
          doc.doc_id.toLowerCase().indexOf(needle) !== -1 ||
          (doc.title || "").toLowerCase().indexOf(needle) !== -1 ||
          (doc.doc_type || "").toLowerCase().indexOf(needle) !== -1 ||
          (doc.organization || "").toLowerCase().indexOf(needle) !== -1
        );
      });
      renderDocumentList();
      renderOverviewGrid(filteredDocuments);
    }

    function renderDocumentList(activeId) {
      if (!docList) return;
      var highlightId = activeId || focusedDocId;
      var groups = groupDocumentsByType(filteredDocuments);
      docList.innerHTML = groups.map(function (group) {
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

      if (docCount) {
        docCount.textContent = filteredDocuments.length + " of " + allDocuments.length + " documents visible";
      }
      if (docEmpty) {
        docEmpty.hidden = filteredDocuments.length > 0;
      }

      if (highlightId) {
        var activeBtn = docList.querySelector('.mind-map-doc-item[data-doc-id="' + highlightId + '"]');
        if (activeBtn && typeof activeBtn.scrollIntoView === "function") {
          activeBtn.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
        if (!renderer) {
          renderer = new NeuralTreeRenderer(container, {
            onSelect: function (node, model) {
              lastModel = model;
              showSidebar(node, model);
            },
          });
        }
        renderer.render(lastGraph, false, "workflow");
      }
    }

    function loadDocuments() {
      if (docCount) docCount.textContent = "Loading documents…";
      return fetchArchiveDocuments("")
        .then(function (payload) {
          allDocuments = payload.documents || [];
          filteredDocuments = allDocuments.slice();
          renderDocTypeFilters();
          if (focusedDocId && !allDocuments.some(function (d) { return d.doc_id === focusedDocId; })) {
            updateFocusLabel();
          }
          filterDocuments();
          updateFocusLabel();
          renderOverviewGrid(filteredDocuments);
        })
        .catch(function (err) {
          if (docCount) docCount.textContent = err.message || "Could not load documents";
        });
    }

    function loadMap() {
      setStatus("Building mind map…");
      if (emptyEl) emptyEl.hidden = true;
      fetchGraph({
        limit: limitSelect ? Number(limitSelect.value) : 80,
        min_similarity: similaritySelect ? Number(similaritySelect.value) : 0.68,
        focus: focusedDocId,
      })
        .then(function (graph) {
          lastGraph = graph;
          if (!graph.count) {
            if (renderer) renderer.destroy();
            renderer = null;
            if (emptyEl) emptyEl.hidden = false;
            hideSidebar();
            renderWorkflowPanel(null);
            setStatus("No documents in archive yet");
            return;
          }
          if (emptyEl) emptyEl.hidden = Boolean(focusedDocId && graph.count);
          if (focusedDocId) {
            if (!renderer) {
              renderer = new NeuralTreeRenderer(container, {
                onSelect: function (node, model) {
                  lastModel = model;
                  showSidebar(node, model);
                },
              });
            }
            renderer.render(graph, false, "workflow");
          } else if (renderer) {
            renderer.destroy();
            renderer = null;
          }
          lastModel = buildNeuralTreeModel(graph);
          if (graph.center) {
            focusedDocId = graph.center;
            if (focusInput) focusInput.value = focusedDocId;
            updateFocusLabel();
            renderDocumentList();
          }
          renderWorkflowPanel(lastModel);
          renderOverviewGrid(filteredDocuments.length ? filteredDocuments : allDocuments);
          if (layoutEl) layoutEl.classList.add("has-workflow");
          if (focusedDocId) setActiveView("workflow");
          else if (activeView !== "overview") setActiveView("overview");
          setStatus(
            graph.count + " documents · " + graph.edge_count + " connections" +
            (graph.center ? " · workflow focused on " + graph.center : " · archive overview")
          );
        })
        .catch(function (err) {
          setStatus(err.message || "Failed to load mind map");
        });
    }

    if (docList) {
      docList.addEventListener("click", function (event) {
        var button = event.target.closest(".mind-map-doc-item");
        if (!button) return;
        setFocus(button.getAttribute("data-doc-id") || "");
        setActiveView("workflow");
      });
    }

    if (workflowGroups) {
      workflowGroups.addEventListener("click", function (event) {
        var button = event.target.closest(".mind-map-workflow-doc");
        if (!button) return;
        var docId = button.getAttribute("data-doc-id") || "";
        if (renderer) renderer.selectNode(docId);
        var node = lastModel && lastModel.nodeById ? lastModel.nodeById[docId] : null;
        if (node) showSidebar(node, lastModel);
      });
    }

    if (overviewGrid) {
      overviewGrid.addEventListener("click", function (event) {
        var button = event.target.closest(".mind-map-overview-card");
        if (!button) return;
        setFocus(button.getAttribute("data-doc-id") || "");
        setActiveView("workflow");
      });
    }

    if (docTypeFilters) {
      docTypeFilters.addEventListener("click", function (event) {
        var chip = event.target.closest(".mind-map-type-chip");
        if (!chip) return;
        activeDocTypeFilter = chip.getAttribute("data-type") || "";
        renderDocTypeFilters();
        filterDocuments();
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
        searchTimer = window.setTimeout(filterDocuments, 120);
      });
    }

    if (reloadBtn) reloadBtn.addEventListener("click", loadMap);
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

    loadDocuments().then(function () {
      if (focusedDocId) setActiveView("workflow");
      else setActiveView("overview");
      loadMap();
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
