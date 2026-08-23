(function () {
  "use strict";

  var cache = Object.create(null);
  var navigating = false;
  var progressEl = null;

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function progress() {
    if (!progressEl) progressEl = document.getElementById("nav-progress");
    return progressEl;
  }

  function startProgress() {
    var el = progress();
    if (!el) return;
    el.classList.remove("done");
    el.classList.add("active");
  }

  function endProgress() {
    var el = progress();
    if (!el) return;
    el.classList.add("done");
    setTimeout(function () {
      el.classList.remove("active", "done");
    }, 320);
  }

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  }

  function initMoreSheet() {
    var toggle = document.getElementById("moreNavToggle");
    var sheet = document.getElementById("moreSheet");
    var backdrop = document.getElementById("moreSheetBackdrop");
    if (!toggle || !sheet) return;

    function open() {
      sheet.hidden = false;
      if (backdrop) backdrop.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      document.body.classList.add("sheet-open");
    }
    function close() {
      sheet.hidden = true;
      if (backdrop) backdrop.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      document.body.classList.remove("sheet-open");
    }

    toggle.addEventListener("click", function () {
      if (sheet.hidden) open();
      else close();
    });
    if (backdrop) backdrop.addEventListener("click", close);
    sheet.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", close);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
  }

  function initPullToRefresh() {
    var root = document.querySelector("[data-pull-refresh]");
    if (!root) return;
    var startY = 0;
    var pulling = false;
    var indicator = document.getElementById("ptrIndicator");

    root.addEventListener(
      "touchstart",
      function (e) {
        if (window.scrollY <= 0) {
          startY = e.touches[0].clientY;
          pulling = true;
        }
      },
      { passive: true }
    );

    root.addEventListener(
      "touchmove",
      function (e) {
        if (!pulling) return;
        var dy = e.touches[0].clientY - startY;
        if (dy > 70 && indicator) indicator.classList.add("visible");
      },
      { passive: true }
    );

    root.addEventListener("touchend", function (e) {
      if (!pulling) return;
      pulling = false;
      var dy = (e.changedTouches[0] && e.changedTouches[0].clientY) - startY;
      if (dy > 90) {
        if (indicator) indicator.classList.add("refreshing");
        visit(location.href, { replace: true, force: true });
      } else if (indicator) {
        indicator.classList.remove("visible");
      }
    });
  }

  var deferredInstallPrompt = null;
  var installHintBound = false;

  function isRunningAsInstalledApp() {
    try {
      if (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) return true;
      if (window.navigator.standalone === true) return true;
    } catch (e) {}
    return false;
  }

  function isIosDevice() {
    var ua = navigator.userAgent || "";
    if (/iPhone|iPad|iPod/i.test(ua)) return true;
    // iPadOS 13+ may report as Mac
    return navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1;
  }

  function isAndroidDevice() {
    return /Android/i.test(navigator.userAgent || "");
  }

  function readStorage(key, legacyKey) {
    try {
      var value = localStorage.getItem(key);
      if (value != null) return value;
      if (legacyKey) return localStorage.getItem(legacyKey);
    } catch (e) {}
    return null;
  }

  function writeStorage(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (e) {}
  }

  function installDismissed() {
    try {
      return (
        localStorage.getItem("sb-install-dismissed") === "1" ||
        sessionStorage.getItem("sb-install-dismissed") === "1" ||
        localStorage.getItem("bcp-install-dismissed") === "1" ||
        sessionStorage.getItem("bcp-install-dismissed") === "1"
      );
    } catch (e) {
      return false;
    }
  }

  function setInstallDismissed() {
    try {
      localStorage.setItem("sb-install-dismissed", "1");
      sessionStorage.setItem("sb-install-dismissed", "1");
    } catch (e) {}
  }

  function hideInstallBanner() {
    var banner = document.getElementById("installBanner");
    if (!banner) return;
    banner.hidden = true;
    banner.classList.add("is-hidden");
  }

  function setInstallCopy() {
    var text = document.getElementById("installBannerText");
    var installBtn = document.getElementById("installAppBtn");
    if (!text) return;

    if (deferredInstallPrompt) {
      text.textContent = "Install Sonali Bank Archive System on this device for faster access.";
      if (installBtn) {
        installBtn.hidden = false;
        installBtn.textContent = "Install";
      }
      return;
    }

    if (isIosDevice()) {
      text.textContent =
        "On iPhone/iPad: tap Share, then “Add to Home Screen” to install Sonali Bank Archive System.";
      if (installBtn) installBtn.hidden = true;
      return;
    }

    if (isAndroidDevice()) {
      text.textContent =
        "On Android Chrome: open the browser menu (⋮) and choose “Install app” or “Add to Home screen”.";
      if (installBtn) installBtn.hidden = true;
      return;
    }

    text.textContent =
      "Install from your browser menu (Install app / Add to Home screen) for a faster experience.";
    if (installBtn) installBtn.hidden = true;
  }

  function showInstallBanner() {
    if (isRunningAsInstalledApp() || installDismissed()) {
      hideInstallBanner();
      return;
    }
    var banner = document.getElementById("installBanner");
    if (!banner) return;
    setInstallCopy();
    banner.hidden = false;
    banner.classList.remove("is-hidden");
  }

  function initInstallHint() {
    if (isRunningAsInstalledApp() || installDismissed()) {
      hideInstallBanner();
    } else if (deferredInstallPrompt || isIosDevice() || isAndroidDevice()) {
      showInstallBanner();
    } else {
      hideInstallBanner();
    }

    if (installHintBound) return;
    installHintBound = true;

    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      deferredInstallPrompt = e;
      showInstallBanner();
    });

    window.addEventListener("appinstalled", function () {
      deferredInstallPrompt = null;
      setInstallDismissed();
      hideInstallBanner();
    });

    // Chromium may fire BIP after engagement; show platform tips sooner on phones.
    setTimeout(function () {
      if (!deferredInstallPrompt && (isIosDevice() || isAndroidDevice())) {
        showInstallBanner();
      }
    }, 2500);

    document.addEventListener(
      "click",
      function (e) {
        if (e.target.closest("#installDismissBtn")) {
          e.preventDefault();
          e.stopPropagation();
          setInstallDismissed();
          hideInstallBanner();
          return;
        }
        if (e.target.closest("#installAppBtn")) {
          e.preventDefault();
          e.stopPropagation();
          if (!deferredInstallPrompt) {
            setInstallCopy();
            return;
          }
          var promptEvent = deferredInstallPrompt;
          deferredInstallPrompt = null;
          promptEvent.prompt();
          Promise.resolve(promptEvent.userChoice)
            .catch(function () {})
            .finally(function () {
              setInstallDismissed();
              hideInstallBanner();
            });
        }
      },
      true
    );
  }

  function shouldIntercept(anchor) {
    if (!anchor || !anchor.href) return false;
    if (anchor.target && anchor.target !== "_self") return false;
    if (anchor.hasAttribute("download")) return false;
    if (anchor.getAttribute("rel") === "external") return false;
    var url;
    try {
      url = new URL(anchor.href, location.href);
    } catch (e) {
      return false;
    }
    if (url.origin !== location.origin) return false;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return false;
    if (url.pathname.indexOf("/logout") === 0) return false;
    // Full page load for viewers — pdf.js must boot with DOMContentLoaded / fresh scripts
    if (url.pathname.indexOf("/view/") === 0) return false;
    if (url.pathname.indexOf("/meetings/") === 0 && /\/documents\/\d+\/view$/.test(url.pathname)) return false;
    if (url.pathname.indexOf("/board/meetings/") === 0 && /\/documents\/\d+\/view$/.test(url.pathname)) return false;
    if (url.pathname.indexOf("/download/") === 0) return false;
    if (url.pathname.indexOf("/attendance/") !== -1 && url.pathname.indexOf("/print") !== -1) return false;
    if (url.pathname.indexOf("/signature") !== -1) return false;
    return true;
  }

  function collectPageScripts(doc) {
    // Snapshot before replaceWith — moving app-shell empties scripts out of `doc`.
    var list = [];
    doc.querySelectorAll("script").forEach(function (oldScript) {
      var src = oldScript.getAttribute("src");
      if (src && src.indexOf("/static/js/app.js") !== -1) return;
      list.push({
        src: src || "",
        text: src ? "" : oldScript.textContent || "",
      });
    });
    return list;
  }

  function runScripts(scriptSpecs) {
    var blockedPatterns = ["unpkg.com/vis-network"];
    (scriptSpecs || []).forEach(function (spec) {
      var src = spec.src;
      if (src && blockedPatterns.some(function (pattern) { return src.indexOf(pattern) !== -1; })) {
        return;
      }
      var s = document.createElement("script");
      if (src) {
        if (document.querySelector('script[src="' + src + '"]')) return;
        s.src = src;
        s.async = false;
      } else if (spec.text) {
        s.textContent = spec.text;
      } else {
        return;
      }
      document.body.appendChild(s);
      if (!src) {
        s.remove();
      }
    });
  }

  function swap(html, url, opts) {
    var parser = new DOMParser();
    var doc = parser.parseFromString(html, "text/html");
    var nextShell = doc.getElementById("app-shell");
    var currShell = document.getElementById("app-shell");
    if (!nextShell || !currShell) {
      location.href = url;
      return;
    }

    var pageScripts = collectPageScripts(doc);

    document.title = doc.title || document.title;

    var nextBodyClass = doc.body.getAttribute("class") || "";
    document.body.setAttribute("class", nextBodyClass);

    currShell.replaceWith(nextShell);

    // Move progress bar back into view if nested swap removed it
    if (!document.getElementById("nav-progress")) {
      var bar = document.createElement("div");
      bar.id = "nav-progress";
      bar.className = "nav-progress";
      bar.setAttribute("aria-hidden", "true");
      document.body.insertBefore(bar, document.body.firstChild);
      progressEl = bar;
    }

    if (opts.replace) {
      history.replaceState({ turbo: true }, "", url);
    } else {
      history.pushState({ turbo: true }, "", url);
    }

    window.scrollTo(0, 0);
    runScripts(pageScripts);
    // Defer boot so swapped DOM + page scripts are attached
    setTimeout(function () {
      bootPage();
      endProgress();
    }, 0);
  }

  function fetchPage(url, force) {
    if (!force && cache[url]) {
      return Promise.resolve({ html: cache[url], url: url });
    }
    return fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "text/html", "X-Requested-With": "BCPNav" },
    }).then(function (res) {
      if (!res.ok) throw new Error("nav failed");
      return res.text().then(function (text) {
        var finalUrl = res.url || url;
        cache[finalUrl] = text;
        cache[url] = text;
        return { html: text, url: finalUrl };
      });
    });
  }

  function visit(href, opts) {
    opts = opts || {};
    if (navigating && !opts.force) return;
    var url = new URL(href, location.href).href;
    if (!opts.force && url === location.href) return;

    navigating = true;
    startProgress();

    // Always fetch fresh HTML on navigate so prefetch/spa cache cannot
    // serve a stale Search page (e.g. missing metadata panel markup).
    fetchPage(url, true)
      .then(function (payload) {
        swap(payload.html, payload.url, opts);
      })
      .catch(function () {
        location.href = href;
      })
      .finally(function () {
        navigating = false;
      });
  }

  function prefetch(href) {
    try {
      var url = new URL(href, location.href).href;
      if (cache[url]) return;
      fetchPage(url, false).catch(function () {});
    } catch (e) {}
  }

  function onClick(e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var anchor = e.target.closest("a");
    if (!shouldIntercept(anchor)) return;
    e.preventDefault();
    visit(anchor.href);
  }

  function onPrefetch(e) {
    var anchor = e.target.closest && e.target.closest("a");
    if (!shouldIntercept(anchor)) return;
    prefetch(anchor.href);
  }

  function onPopState() {
    visit(location.href, { replace: true, force: true });
  }

  function showSearchPanel(which) {
    var root = document.querySelector('[data-page="search"]');
    if (!root) return;
    var showMeta = which === "metadata";
    var tabKeyword = document.getElementById("tabKeyword");
    var tabMetadata = document.getElementById("tabMetadata");
    var keywordPanel = document.getElementById("keywordPanel");
    var metadataPanel = document.getElementById("metadataPanel");
    var searchResults = document.getElementById("searchResults");
    if (!tabKeyword || !tabMetadata || !keywordPanel || !metadataPanel) return;

    tabKeyword.classList.toggle("active", !showMeta);
    tabMetadata.classList.toggle("active", showMeta);
    tabKeyword.setAttribute("aria-selected", showMeta ? "false" : "true");
    tabMetadata.setAttribute("aria-selected", showMeta ? "true" : "false");

    // Class-only toggle (avoid inline display fighting CSS !important)
    keywordPanel.classList.toggle("is-hidden", showMeta);
    metadataPanel.classList.toggle("is-hidden", !showMeta);
    keywordPanel.removeAttribute("hidden");
    metadataPanel.removeAttribute("hidden");
    keywordPanel.style.removeProperty("display");
    metadataPanel.style.removeProperty("display");
    keywordPanel.setAttribute("aria-hidden", showMeta ? "true" : "false");
    metadataPanel.setAttribute("aria-hidden", showMeta ? "false" : "true");

    if (searchResults && searchResults.dataset.idleHtml) {
      searchResults.innerHTML = searchResults.dataset.idleHtml;
    }
  }

  function skeletonHtml() {
    return (
      '<div class="skeleton-list" aria-hidden="true">' +
      '<div class="skeleton-row"><div class="skeleton skeleton-line" style="width:55%"></div><div class="skeleton skeleton-line short" style="width:35%"></div></div>' +
      '<div class="skeleton-row"><div class="skeleton skeleton-line" style="width:60%"></div><div class="skeleton skeleton-line short" style="width:40%"></div></div>' +
      "</div>"
    );
  }

  function emptyHtml(title, message) {
    return '<div class="empty-state"><h3>' + title + "</h3><p>" + message + "</p></div>";
  }

  var THEME_META = {
    green: "#1a1a54",
    red: "#8e101c",
    yellow: "#1a1810",
    white: "#ffffff",
    black: "#0b1210",
  };

  function applyTheme(theme) {
    if (!/^(green|red|yellow|white|black)$/.test(theme)) theme = "green";
    document.documentElement.setAttribute("data-theme", theme);
    try {
    writeStorage("sb-theme", theme);
    } catch (e) {}
    var meta = document.getElementById("metaThemeColor");
    if (meta) meta.setAttribute("content", THEME_META[theme] || THEME_META.green);
    document.querySelectorAll("[data-theme-choice]").forEach(function (btn) {
      var active = btn.getAttribute("data-theme-choice") === theme;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function initTheme() {
    var stored = "green";
    try {
      stored = readStorage("sb-theme", "bcb-theme") || "green";
    } catch (e) {}
    applyTheme(stored);
  }

  function initAppearancePage() {
    var root = document.querySelector('[data-page="appearance"]');
    if (!root) return;
    var current = document.documentElement.getAttribute("data-theme") || "green";
    applyTheme(current);
  }

  function onThemeClick(e) {
    var btn = e.target.closest("[data-theme-choice]");
    if (!btn) return;
    e.preventDefault();
    applyTheme(btn.getAttribute("data-theme-choice"));
  }

  function setSearchLang(lang) {
    var query = document.getElementById("searchQuery");
    var hint = document.getElementById("searchLangHint");
    var en = document.getElementById("langEn");
    var bn = document.getElementById("langBn");
    var isBn = lang === "bn";
    if (en) en.classList.toggle("is-active", !isBn);
    if (bn) bn.classList.toggle("is-active", isBn);
    if (query) {
      query.setAttribute("lang", isBn ? "bn" : "en");
      query.placeholder = isBn
        ? "অনুসন্ধান লিখুন… (Bangla or English)"
        : "Keyword, summary, or বাংলা phrase…";
    }
    if (hint) {
      hint.textContent = isBn
        ? "বাংলা ও ইংরেজি — দুই ভাষাতেই খোঁজা যাবে।"
        : "Type in English or Bangla — both are supported.";
    }
    try {
    writeStorage("sb-search-lang", isBn ? "bn" : "en");
    } catch (e) {}
  }

  function initSearchLang() {
    if (!document.querySelector('[data-page="search"]')) return;
    var lang = "en";
    try {
      lang = readStorage("sb-search-lang", "bcb-search-lang") || "en";
    } catch (e) {}
    setSearchLang(lang);
  }

  function onSearchLangClick(e) {
    var btn = e.target.closest("[data-search-lang]");
    if (!btn || !document.querySelector('[data-page="search"]')) return;
    e.preventDefault();
    setSearchLang(btn.getAttribute("data-search-lang"));
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderAccessActions(result) {
    var status = result.access_status || "none";
    if (result.can_view) {
      var links =
        '<a class="doc-link" href="/view/' +
        encodeURIComponent(result.doc_id) +
        '?from=/search">' +
        escapeHtml(result.doc_id) +
        "</a>";
      if (result.can_download) {
        links +=
          ' <a class="button button-secondary result-download" href="/download/' +
          encodeURIComponent(result.doc_id) +
          '">Download (watermarked)</a>';
      }
      return links;
    }
    if (status === "pending") {
      return (
        "<strong>" +
        escapeHtml(result.doc_id) +
        '</strong> <span class="badge">Request pending</span> · <a href="/access-requests">Track request</a>'
      );
    }
    return (
      "<strong>" +
      escapeHtml(result.doc_id) +
      '</strong> <span class="badge">Access required</span>' +
      ' <button type="button" class="button button-secondary access-request-btn" data-doc-id="' +
      encodeURIComponent(result.doc_id) +
      '" data-mode="view_only">Request view</button>'
    );
  }

  function renderKeywordResults(results) {
    return results
      .map(function (result) {
        var keywords = Array.isArray(result.searchable_keywords)
          ? result.searchable_keywords.join(", ")
          : "";
        var score =
          result.score != null && !isNaN(Number(result.score))
            ? Number(result.score).toFixed(3)
            : "N/A";
        var source = result.source || "summary";
        return (
          '<article class="result-card list-row">' +
          '<div class="result-header">' +
          "<h3>" +
          renderAccessActions(result) +
          "</h3>" +
          '<span class="badge">' +
          escapeHtml(result.doc_type || "Document") +
          "</span></div>" +
          "<p><strong>Match type:</strong> " +
          escapeHtml(source) +
          "</p>" +
          "<p><strong>Score:</strong> " +
          escapeHtml(score) +
          "</p>" +
          "<p><strong>Keywords:</strong> " +
          escapeHtml(keywords) +
          "</p>" +
          "</article>"
        );
      })
      .join("");
  }

  function renderMetadataResults(results) {
    return results
      .map(function (result) {
        var keywords = Array.isArray(result.searchable_keywords)
          ? result.searchable_keywords.slice(0, 8).join(", ")
          : "";
        return (
          '<article class="result-card list-row">' +
          '<div class="result-header">' +
          "<h3>" +
          renderAccessActions(result) +
          "</h3>" +
          '<span class="badge">' +
          escapeHtml(result.doc_type || "Document") +
          "</span></div>" +
          '<p class="result-meta">Date: ' +
          escapeHtml(result.doc_date || "—") +
          " · Uploaded by: " +
          escapeHtml(result.uploaded_by || "—") +
          "</p>" +
          (keywords ? '<p class="result-meta">Topics: ' + escapeHtml(keywords) + "</p>" : "") +
          "</article>"
        );
      })
      .join("");
  }

  function initSearchPage() {
    var root = document.querySelector('[data-page="search"]');
    var searchResults = document.getElementById("searchResults");
    if (root && searchResults && !searchResults.dataset.idleHtml) {
      searchResults.dataset.idleHtml = searchResults.innerHTML;
    }
    // Ensure keyword is the default visible panel after SPA navigation
    if (root) {
      var active = document.querySelector(".search-tab.active");
      var which = active && active.getAttribute("data-search-tab") === "metadata" ? "metadata" : "keyword";
      showSearchPanel(which);
    }
  }

  function onSearchUiClick(e) {
    var tab = e.target.closest("[data-search-tab]");
    if (tab && document.querySelector('[data-page="search"]')) {
      e.preventDefault();
      e.stopPropagation();
      showSearchPanel(tab.getAttribute("data-search-tab"));
      return;
    }

    if (e.target.closest("#searchButton")) {
      e.preventDefault();
      var searchQuery = document.getElementById("searchQuery");
      var searchResults = document.getElementById("searchResults");
      var searchButton = document.getElementById("searchButton");
      if (!searchResults || !searchButton) return;
      var query = (searchQuery && searchQuery.value.trim()) || "";
      if (!query) {
        searchResults.innerHTML = emptyHtml("Enter a query", "Type a keyword or phrase to search.");
        return;
      }
      searchButton.disabled = true;
      searchResults.innerHTML = skeletonHtml();
      var lang = "en";
      try {
        lang = readStorage("sb-search-lang", "bcb-search-lang") || "en";
      } catch (e) {}
      var searchUrl =
        "/api/search?q=" + encodeURIComponent(query) + "&lang=" + encodeURIComponent(lang);
      fetch(searchUrl, { credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) throw new Error("Search failed");
          return response.json();
        })
        .then(function (payload) {
          if (!payload.results.length) {
            searchResults.innerHTML = emptyHtml("No results", 'Nothing matched “' + payload.query + '”.');
            return;
          }
          searchResults.innerHTML = renderKeywordResults(payload.results);
        })
        .catch(function () {
          searchResults.innerHTML = emptyHtml("Search unavailable", "Unable to complete search. Please try again.");
        })
        .finally(function () {
          searchButton.disabled = false;
        });
      return;
    }

    if (e.target.closest(".access-request-btn")) {
      e.preventDefault();
      var btn = e.target.closest(".access-request-btn");
      var docId = decodeURIComponent(btn.getAttribute("data-doc-id") || "");
      if (!docId) return;
      var purpose = window.prompt("Why do you need to view " + docId + "?", "Board affairs review");
      if (!purpose) return;
      var body = new FormData();
      body.append("purpose", purpose);
      body.append("requested_mode", "view_only");
      var headers = window.BCPCsrf ? window.BCPCsrf.headers() : {};
      btn.disabled = true;
      fetch("/api/documents/" + encodeURIComponent(docId) + "/access-requests", {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: body,
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Request failed");
          return response.json();
        })
        .then(function () {
          window.location.href = "/access-requests?status=submitted";
        })
        .catch(function () {
          btn.disabled = false;
          alert("Could not submit access request.");
        });
      return;
    }

    if (e.target.closest("#metadataSearchButton")) {
      e.preventDefault();
      var searchResults2 = document.getElementById("searchResults");
      var metadataSearchButton = document.getElementById("metadataSearchButton");
      if (!searchResults2 || !metadataSearchButton) return;
      var params = new URLSearchParams();
      var docId = ((document.getElementById("metaDocId") || {}).value || "").trim();
      var docType = ((document.getElementById("metaDocType") || {}).value || "").trim();
      var uploadedBy = ((document.getElementById("metaUploadedBy") || {}).value || "").trim();
      var dateFrom = (document.getElementById("metaDateFrom") || {}).value || "";
      var dateTo = (document.getElementById("metaDateTo") || {}).value || "";

      if (!docId && !docType && !uploadedBy && !dateFrom && !dateTo) {
        searchResults2.innerHTML = emptyHtml("Add a filter", "Enter at least one metadata field.");
        return;
      }
      if (docId) params.set("doc_id", docId);
      if (docType) params.set("doc_type", docType);
      if (uploadedBy) params.set("uploaded_by", uploadedBy);
      if (dateFrom) params.set("date_from", dateFrom);
      if (dateTo) params.set("date_to", dateTo);

      metadataSearchButton.disabled = true;
      searchResults2.innerHTML = skeletonHtml();
      fetch("/api/search/metadata?" + params.toString(), { credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) throw new Error("Search failed");
          return response.json();
        })
        .then(function (payload) {
          if (!payload.results.length) {
            searchResults2.innerHTML = emptyHtml("No results", "No documents matched those filters.");
            return;
          }
          searchResults2.innerHTML = renderMetadataResults(payload.results);
        })
        .catch(function () {
          searchResults2.innerHTML = emptyHtml("Search unavailable", "Unable to complete search. Please try again.");
        })
        .finally(function () {
          metadataSearchButton.disabled = false;
        });
    }
  }

  function onSearchKeydown(e) {
    if (e.key !== "Enter") return;
    if (e.target && e.target.id === "searchQuery") {
      e.preventDefault();
      var btn = document.getElementById("searchButton");
      if (btn) btn.click();
    }
  }

  function warmNavPrefetch() {
    var seen = Object.create(null);
    document.querySelectorAll(".sidebar-link[href], .bottom-nav a[href]").forEach(function (a) {
      var href = a.getAttribute("href");
      if (!href || href.charAt(0) !== "/" || seen[href]) return;
      seen[href] = true;
      setTimeout(function () {
        prefetch(href);
      }, 600);
    });
  }

  function onAdminUsersClick(e) {
    var editBtn = e.target.closest("[data-edit-user]");
    if (editBtn) {
      e.preventDefault();
      var id = editBtn.getAttribute("data-edit-user");
      var row = document.getElementById("edit-row-" + id);
      var card = document.getElementById("edit-card-" + id);
      var open = false;
      if (row) {
        row.hidden = !row.hidden;
        open = !row.hidden;
      }
      if (card) {
        card.hidden = row ? row.hidden : !card.hidden;
        open = !card.hidden;
      }
      editBtn.setAttribute("aria-expanded", open ? "true" : "false");
      editBtn.textContent = open ? "Close" : "Edit";
      return;
    }
    var cancel = e.target.closest("[data-cancel-edit]");
    if (cancel) {
      e.preventDefault();
      var cid = cancel.getAttribute("data-cancel-edit");
      var r = document.getElementById("edit-row-" + cid);
      var c = document.getElementById("edit-card-" + cid);
      if (r) r.hidden = true;
      if (c) c.hidden = true;
      document.querySelectorAll('[data-edit-user="' + cid + '"]').forEach(function (btn) {
        btn.setAttribute("aria-expanded", "false");
        btn.textContent = "Edit";
      });
    }
  }

  function bootPage() {
    if (window.BCPCsrf && typeof window.BCPCsrf.injectForms === "function") {
      window.BCPCsrf.injectForms();
    }
    initTheme();
    initMoreSheet();
    initPullToRefresh();
    initInstallHint();
    initSearchPage();
    initSearchLang();
    initAppearancePage();
    document.body.classList.add("ready");
  }

  // Capture phase so it wins even if other handlers stop bubbling
  document.addEventListener("click", onSearchUiClick, true);
  document.addEventListener("click", onThemeClick, true);
  document.addEventListener("click", onSearchLangClick, true);
  document.addEventListener("click", onAdminUsersClick, true);
  document.addEventListener("keydown", onSearchKeydown, true);
  document.addEventListener("click", onClick);
  document.addEventListener("mouseover", onPrefetch, { passive: true });
  document.addEventListener("touchstart", onPrefetch, { passive: true });
  window.addEventListener("popstate", onPopState);

  document.addEventListener("DOMContentLoaded", function () {
    registerServiceWorker();
    bootPage();
    warmNavPrefetch();
  });

  // Expose for pull-to-refresh
  window.BCPNav = {
    visit: visit,
    prefetch: prefetch,
    showSearchPanel: showSearchPanel,
    applyTheme: applyTheme,
  };
})();
