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

  function showUpdateBanner(worker) {
    var banner = document.getElementById("updateBanner");
    var btn = document.getElementById("updateReloadBtn");
    if (!banner || !btn || !worker) return;
    banner.hidden = false;
    btn.onclick = function () {
      btn.disabled = true;
      worker.postMessage({ type: "SKIP_WAITING" });
    };
  }

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    var reloading = false;
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (reg) {
      if (!reg) return;
      // A new version is waiting only when an older one already controls this page.
      if (reg.waiting && navigator.serviceWorker.controller) showUpdateBanner(reg.waiting);
      reg.addEventListener("updatefound", function () {
        var next = reg.installing;
        if (!next) return;
        next.addEventListener("statechange", function () {
          if (next.state === "installed" && navigator.serviceWorker.controller) showUpdateBanner(next);
        });
      });
      try { reg.update(); } catch (e) {}
    }).catch(function () {});
  }

  function markInstalledApp() {
    if (isRunningAsInstalledApp()) {
      document.documentElement.classList.add("is-pwa");
      document.body.classList.add("is-pwa");
    }
  }

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function sheetParts() {
    return {
      toggle: document.getElementById("moreNavToggle"),
      sheet: document.getElementById("moreSheet"),
      backdrop: document.getElementById("moreSheetBackdrop"),
    };
  }

  function closeMoreSheet(restoreFocus) {
    var p = sheetParts();
    if (!p.sheet || p.sheet.hidden) return;
    p.sheet.hidden = true;
    if (p.backdrop) p.backdrop.hidden = true;
    if (p.toggle) p.toggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("sheet-open");
    if (restoreFocus && p.toggle) p.toggle.focus();
  }

  function openMoreSheet() {
    var p = sheetParts();
    if (!p.sheet) return;
    p.sheet.hidden = false;
    if (p.backdrop) p.backdrop.hidden = false;
    if (p.toggle) p.toggle.setAttribute("aria-expanded", "true");
    document.body.classList.add("sheet-open");
    var first = p.sheet.querySelector(FOCUSABLE);
    (first || p.sheet).focus();
  }

  function initMoreSheet() {
    var p = sheetParts();
    if (!p.toggle || !p.sheet) return;
    p.toggle.addEventListener("click", function () {
      if (p.sheet.hidden) openMoreSheet();
      else closeMoreSheet(true);
    });
    if (p.backdrop) p.backdrop.addEventListener("click", function () { closeMoreSheet(true); });
    p.sheet.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () { closeMoreSheet(false); });
    });
    p.sheet.querySelectorAll("[data-sheet-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { closeMoreSheet(true); });
    });
  }

  function onSheetKeydown(e) {
    var p = sheetParts();
    if (!p.sheet || p.sheet.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeMoreSheet(true);
      return;
    }
    if (e.key !== "Tab") return;
    var items = Array.prototype.filter.call(p.sheet.querySelectorAll(FOCUSABLE), function (el) {
      return el.offsetParent !== null;
    });
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function announce(text) {
    var region = document.getElementById("routeAnnouncer");
    if (!region) return;
    region.textContent = "";
    setTimeout(function () { region.textContent = text; }, 50);
  }

  function focusNewPage() {
    var heading = document.querySelector("#main-content h1");
    var target = heading || document.getElementById("main-content");
    if (!target) return;
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
    announce((heading && heading.textContent.trim()) || document.title);
  }

  function toast(message, ms) {
    var region = document.getElementById("toastRegion");
    if (!region || !message) return;
    var el = document.createElement("div");
    el.className = "ui-toast";
    el.textContent = message;
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, ms || 4200);
  }

  /**
   * Plain confirm dialog. opts: { title, text, ok, danger, typeToConfirm }.
   * Resolves true when the person confirms.
   */
  function confirmAction(opts) {
    opts = opts || {};
    var dialog = document.getElementById("confirmDialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(opts.text || opts.title || "Are you sure?"));
    }
    var title = document.getElementById("confirmDialogTitle");
    var text = document.getElementById("confirmDialogText");
    var extra = document.getElementById("confirmDialogExtra");
    var ok = document.getElementById("confirmDialogOk");
    title.textContent = opts.title || "Are you sure?";
    text.textContent = opts.text || "";
    ok.textContent = opts.ok || "Confirm";
    ok.className = "button" + (opts.danger ? " button-danger" : "");
    extra.innerHTML = "";
    var typed = null;
    if (opts.typeToConfirm) {
      var label = document.createElement("label");
      label.textContent = 'Type "' + opts.typeToConfirm + '" to confirm';
      label.setAttribute("for", "confirmDialogInput");
      typed = document.createElement("input");
      typed.id = "confirmDialogInput";
      typed.autocomplete = "off";
      extra.appendChild(label);
      extra.appendChild(typed);
      ok.disabled = true;
      typed.addEventListener("input", function () {
        ok.disabled = typed.value.trim() !== opts.typeToConfirm;
      });
    } else {
      ok.disabled = false;
    }
    return new Promise(function (resolve) {
      dialog.addEventListener(
        "close",
        function () {
          resolve(dialog.returnValue === "ok");
        },
        { once: true }
      );
      dialog.returnValue = "";
      dialog.showModal();
      (typed || ok).focus();
    });
  }

  /** Dialog with one text box. opts: { title, text, label, value, ok }. Resolves the text, or null if cancelled. */
  function promptAction(opts) {
    opts = opts || {};
    var dialog = document.getElementById("confirmDialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      var answer = window.prompt(opts.label || opts.title || "", opts.value || "");
      return Promise.resolve(answer && answer.trim() ? answer.trim() : null);
    }
    var extra = document.getElementById("confirmDialogExtra");
    var ok = document.getElementById("confirmDialogOk");
    document.getElementById("confirmDialogTitle").textContent = opts.title || "";
    document.getElementById("confirmDialogText").textContent = opts.text || "";
    ok.textContent = opts.ok || "OK";
    ok.className = "button";
    extra.innerHTML = "";
    var label = document.createElement("label");
    label.setAttribute("for", "confirmDialogInput");
    label.textContent = opts.label || "";
    var input = document.createElement("textarea");
    input.id = "confirmDialogInput";
    input.rows = 2;
    input.maxLength = 500;
    input.value = opts.value || "";
    extra.appendChild(label);
    extra.appendChild(input);
    ok.disabled = !input.value.trim();
    input.addEventListener("input", function () {
      ok.disabled = !input.value.trim();
    });
    return new Promise(function (resolve) {
      dialog.addEventListener(
        "close",
        function () {
          resolve(dialog.returnValue === "ok" && input.value.trim() ? input.value.trim() : null);
        },
        { once: true }
      );
      dialog.returnValue = "";
      dialog.showModal();
      input.focus();
      input.select();
    });
  }

  function confirmOptionsFrom(el) {
    return {
      title: el.getAttribute("data-confirm-title") || el.getAttribute("data-confirm"),
      text: el.getAttribute("data-confirm-title") ? el.getAttribute("data-confirm") : el.getAttribute("data-confirm-text") || "",
      ok: el.getAttribute("data-confirm-ok") || "",
      danger: el.hasAttribute("data-confirm-danger"),
      typeToConfirm: el.getAttribute("data-confirm-type") || "",
    };
  }

  // Forms or submit buttons marked data-confirm ask first; the dialog is the same everywhere.
  function onConfirmSubmit(e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    var submitter = e.submitter;
    var source = submitter && submitter.hasAttribute("data-confirm") ? submitter : form.hasAttribute("data-confirm") ? form : null;
    if (!source || form.dataset.confirmed === "1") {
      delete form.dataset.confirmed;
      markBusy(e, form, submitter);
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    confirmAction(confirmOptionsFrom(source)).then(function (yes) {
      if (!yes) return;
      form.dataset.confirmed = "1";
      if (typeof form.requestSubmit === "function") form.requestSubmit(submitter || undefined);
      else form.submit();
    });
  }

  // Prevent double submits on ordinary forms: the pressed button shows it is working.
  // Forms handled by page scripts (they call preventDefault) manage their own buttons.
  function markBusy(e, form, submitter) {
    if (form.hasAttribute("data-no-busy") || (form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    var btn = submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (!btn) return;
    setTimeout(function () {
      if (e.defaultPrevented) return;
      btn.setAttribute("aria-busy", "true");
      btn.disabled = true;
    }, 0);
  }

  function releaseBusyButtons() {
    document.querySelectorAll('button[aria-busy="true"]').forEach(function (btn) {
      btn.removeAttribute("aria-busy");
      btn.disabled = false;
    });
  }

  function onConfirmClick(e) {
    var link = e.target.closest && e.target.closest("a[data-confirm]");
    if (!link) return;
    if (link.dataset.confirmed === "1") {
      delete link.dataset.confirmed;
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    confirmAction(confirmOptionsFrom(link)).then(function (yes) {
      if (!yes) return;
      link.dataset.confirmed = "1";
      link.click();
    });
  }

  var ERROR_PATTERNS = [
    [/already exists/i, "duplicate_id"],
    [/disposed record/i, "doc_id_reserved"],
    [/(413|too large|exceeds)/i, "too_large"],
    [/(could not extract|no text|pdf.*(invalid|corrupt|damaged)|EOF marker|PdfReadError|not a pdf)/i, "unreadable_pdf"],
    [/(unsupported|content type|file type)/i, "unsupported_file"],
    [/(timed? ?out|connection|unreachable|temporarily|failed to fetch|networkerror)/i, "network"],
  ];
  var errorCopy = null;

  /** Plain message for a server or network error; mirrors ui.friendly_error on the server. */
  function friendlyError(raw, fallbackKey) {
    if (!errorCopy) {
      try {
        errorCopy = JSON.parse((document.getElementById("uiErrorCopy") || {}).textContent || "{}");
      } catch (e) {
        errorCopy = {};
      }
    }
    var text = String(raw || "");
    for (var i = 0; i < ERROR_PATTERNS.length; i += 1) {
      if (ERROR_PATTERNS[i][0].test(text)) return errorCopy[ERROR_PATTERNS[i][1]] || text;
    }
    return errorCopy[fallbackKey || "processing_failed"] || "Something went wrong. Please try again.";
  }

  function updateOnlineState() {
    var banner = document.getElementById("offlineBanner");
    if (banner) banner.hidden = navigator.onLine !== false;
  }

  var unreadFetchedAt = 0;
  function refreshUnreadCount(force) {
    var badges = document.querySelectorAll("[data-unread-count]");
    if (!badges.length) return;
    var now = Date.now();
    if (!force && now - unreadFetchedAt < 60000 && window.__bcpUnread != null) {
      paintUnread(window.__bcpUnread);
      return;
    }
    unreadFetchedAt = now;
    fetch("/api/notifications/unread-count", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body) return;
        window.__bcpUnread = body.unread || 0;
        paintUnread(window.__bcpUnread);
      })
      .catch(function () {});
  }

  function paintUnread(count) {
    document.querySelectorAll("[data-unread-count]").forEach(function (el) {
      el.hidden = !count;
      el.textContent = count > 99 ? "99+" : String(count || "");
      var host = el.closest("a");
      if (host) host.setAttribute("aria-label", "Notifications" + (count ? " (" + count + " unread)" : ""));
    });
  }

  function onGlobalSearchSubmit(e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.classList.contains("app-topbar-search")) return;
    e.preventDefault();
    var q = ((form.querySelector('input[name="q"]') || {}).value || "").trim();
    var pageInput = document.getElementById("searchQuery");
    if (pageInput && document.querySelector('[data-page="search"]')) {
      pageInput.value = q;
      runSearch(true);
      return;
    }
    visit("/search" + (q ? "?q=" + encodeURIComponent(q) : ""));
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
    // The organizer page needs its own Permissions-Policy (microphone), which only a full load applies.
    if (/^\/meetings\/\d+\/?$/.test(url.pathname)) return false;
    if (url.pathname.indexOf("/attendance/") !== -1 && url.pathname.indexOf("/print") !== -1) return false;
    if (url.pathname.indexOf("/signature") !== -1) return false;
    return true;
  }

  function collectPageScripts(doc) {
    // Snapshot before replaceWith — moving app-shell empties scripts out of `doc`.
    var list = [];
    doc.querySelectorAll("script").forEach(function (oldScript) {
      var type = (oldScript.getAttribute("type") || "").trim().toLowerCase();
      if (type && type !== "text/javascript" && type !== "module") return;
      var src = oldScript.getAttribute("src");
      if (src && src.indexOf("/static/js/app.js") !== -1) return;
      list.push({
        src: src || "",
        text: src ? "" : oldScript.textContent || "",
      });
    });
    return list;
  }

  function loadExternalScript(src) {
    var existing = document.querySelector('script[src="' + src + '"]');
    if (existing) {
      if (existing.getAttribute("data-bcp-loaded") === "1") return Promise.resolve();
      return new Promise(function (resolve) {
        var settled = false;
        var done = function () {
          if (settled) return;
          settled = true;
          existing.setAttribute("data-bcp-loaded", "1");
          resolve();
        };
        existing.addEventListener("load", done, { once: true });
        existing.addEventListener("error", done, { once: true });
        if (src.indexOf("archive-graph.js") !== -1 && window.BCPArchiveGraph) {
          done();
          return;
        }
        setTimeout(done, 400);
      });
    }
    return new Promise(function (resolve) {
      var s = document.createElement("script");
      s.src = src;
      s.async = false;
      s.onload = function () {
        s.setAttribute("data-bcp-loaded", "1");
        resolve();
      };
      s.onerror = function () { resolve(); };
      document.body.appendChild(s);
    });
  }

  function runScripts(scriptSpecs) {
    var blockedPatterns = ["unpkg.com/vis-network"];
    var chain = Promise.resolve();
    (scriptSpecs || []).forEach(function (spec) {
      chain = chain.then(function () {
        var src = spec.src;
        if (src && blockedPatterns.some(function (pattern) { return src.indexOf(pattern) !== -1; })) {
          return;
        }
        if (src) {
          return loadExternalScript(src);
        }
        if (spec.text) {
          var s = document.createElement("script");
          s.textContent = spec.text;
          document.body.appendChild(s);
          s.remove();
        }
      });
    });
    return chain;
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
    leaveGuard = null;

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
    runScripts(pageScripts)
      .catch(function () {})
      .then(function () {
        bootPage();
        endProgress();
        if (!opts.replace) focusNewPage();
      });
  }

  function fetchPage(url, force) {
    if (!force && cache[url]) {
      return Promise.resolve({ html: cache[url], url: url });
    }
    return fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "text/html", "X-Requested-With": "BCPNav" },
      redirect: "manual",
    }).then(function (res) {
      if (res.status === 401 || res.status === 302 || res.status === 303) {
        var loc = res.headers.get("Location") || "/login";
        if (res.status === 401 || (loc && loc.indexOf("/login") !== -1)) {
          window.location.href = "/login";
          throw new Error("auth");
        }
      }
      if (res.type === "opaqueredirect") {
        window.location.href = "/login";
        throw new Error("auth");
      }
      if (!res.ok) throw new Error("nav failed");
      return res.text().then(function (text) {
        var finalUrl = res.url || url;
        cache[finalUrl] = text;
        cache[url] = text;
        return { html: text, url: finalUrl };
      });
    });
  }

  /**
   * A page doing work that would be lost on navigation (for example a live recording) can ask
   * before the person leaves. guard: { title, message, ok, onLeave } or null to clear.
   */
  var leaveGuard = null;

  function setLeaveGuard(guard) {
    leaveGuard = guard ? { title: guard.title, message: guard.message, ok: guard.ok, onLeave: guard.onLeave, url: location.href } : null;
  }

  function checkLeave(proceed, cancel) {
    var guard = leaveGuard;
    if (!guard) {
      proceed();
      return;
    }
    confirmAction({
      title: guard.title || "Leave this page?",
      text: guard.message || "Work on this page will be lost.",
      ok: guard.ok || "Leave",
      danger: true,
    }).then(function (yes) {
      if (!yes) {
        if (cancel) cancel(guard);
        return;
      }
      leaveGuard = null;
      try {
        if (guard.onLeave) guard.onLeave();
      } catch (e) {}
      proceed();
    });
  }

  function visit(href, opts) {
    opts = opts || {};
    if (leaveGuard && !opts.leaving) {
      checkLeave(function () {
        opts.leaving = true;
        visit(href, opts);
      });
      return;
    }
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

  function onPopState() {
    if (leaveGuard) {
      checkLeave(
        function () {
          visit(location.href, { replace: true, force: true, leaving: true });
        },
        function (guard) {
          history.pushState({ turbo: true }, "", guard.url);
        }
      );
      return;
    }
    visit(location.href, { replace: true, force: true });
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
    return '<div class="empty-state"><h3>' + escapeHtml(title) + "</h3><p>" + escapeHtml(message) + "</p></div>";
  }

  var THEME_META = {
    green: "#1a1a54",
    red: "#8e101c",
    yellow: "#1a1810",
    white: "#ffffff",
  };
  var DARK_META = "#0e1118";
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  /** Reads the saved appearance: brand theme, light/dark/system mode and text size. */
  function readAppearance() {
    var theme = readStorage("sb-theme", "bcb-theme") || "green";
    var mode = readStorage("sb-mode");
    if (theme === "black") {
      theme = "white";
      if (!mode) mode = "dark";
    }
    if (!/^(green|red|yellow|white)$/.test(theme)) theme = "green";
    if (!/^(light|dark|system)$/.test(mode || "")) mode = "system";
    var text = readStorage("sb-text");
    if (!/^(standard|large|xlarge)$/.test(text || "")) text = "standard";
    return { theme: theme, mode: mode, text: text };
  }

  function applyAppearance(next, save) {
    var current = readAppearance();
    var a = {
      theme: (next && next.theme) || current.theme,
      mode: (next && next.mode) || current.mode,
      text: (next && next.text) || current.text,
    };
    var root = document.documentElement;
    var dark = a.mode === "dark" || (a.mode === "system" && darkQuery && darkQuery.matches);
    root.setAttribute("data-theme", a.theme);
    root.setAttribute("data-mode", dark ? "dark" : "light");
    if (a.text === "standard") root.removeAttribute("data-text-size");
    else root.setAttribute("data-text-size", a.text);
    if (save) {
      writeStorage("sb-theme", a.theme);
      writeStorage("sb-mode", a.mode);
      writeStorage("sb-text", a.text);
    }
    var meta = document.getElementById("metaThemeColor");
    if (meta) meta.setAttribute("content", dark ? DARK_META : THEME_META[a.theme] || THEME_META.green);
    syncAppearanceControls(a);
    return a;
  }

  function syncAppearanceControls(a) {
    document.querySelectorAll("input[data-appearance]").forEach(function (input) {
      input.checked = a[input.getAttribute("data-appearance")] === input.value;
    });
  }

  // Back-compat for callers of the old API.
  function applyTheme(theme) {
    if (theme === "black") return applyAppearance({ theme: "white", mode: "dark" }, true);
    return applyAppearance({ theme: theme }, true);
  }

  function initTheme() {
    applyAppearance(null, false);
  }

  function initAppearancePage() {
    if (!document.querySelector('[data-page="appearance"]')) return;
    syncAppearanceControls(readAppearance());
  }

  function onAppearanceChange(e) {
    var input = e.target;
    if (!input || !input.matches || !input.matches("input[data-appearance]")) return;
    var change = {};
    change[input.getAttribute("data-appearance")] = input.value;
    applyAppearance(change, true);
    toast("Saved on this device.");
  }

  function onThemeClick(e) {
    var btn = e.target.closest("[data-theme-choice]");
    if (!btn) return;
    e.preventDefault();
    applyTheme(btn.getAttribute("data-theme-choice"));
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function viewerHref(docId, page, q) {
    return (
      "/view/" +
      encodeURIComponent(docId) +
      "?from=" +
      encodeURIComponent(location.pathname + location.search) +
      (page > 0 ? "&page=" + page : "") +
      (q ? "&hl=" + encodeURIComponent(q.slice(0, 160)) : "")
    );
  }

  function renderAccessActions(result, q) {
    if (result.can_view) {
      return (
        '<a class="doc-link" href="' +
        viewerHref(result.doc_id, parseInt(result.page, 10), q) +
        '" data-nav>' +
        escapeHtml(result.title || result.doc_id) +
        "</a>"
      );
    }
    return "<span>" + escapeHtml(result.title || result.doc_id) + "</span>";
  }

  function renderPageLinks(result, q) {
    var pages = Array.isArray(result.pages) ? result.pages : [];
    if (!result.can_view || !pages.length) return "";
    var links = pages.map(function (p) {
      return '<a href="' + viewerHref(result.doc_id, p, q) + '" data-nav aria-label="Open page ' + p + '">' + p + "</a>";
    });
    var more = (result.pages_matched || pages.length) - pages.length;
    return (
      '<p class="result-pages">' +
      (pages.length === 1 ? "Found on page " : "Found on pages ") +
      links.join(", ") +
      (more > 0 ? " and " + more + " more" : "") +
      "</p>"
    );
  }

  var MATCH_LABELS = {
    summary: "the summary",
    chunk: "the page text",
    keyword: "the keywords",
    project: "the project name",
    doc_id: "the archive number",
    title: "the title",
  };

  function shortDate(iso) {
    if (!iso) return "";
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
    if (!m) return String(iso);
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return m[3] + " " + months[parseInt(m[2], 10) - 1] + " " + m[1];
  }

  function accessHtml(result) {
    if (result.can_view) {
      if (!result.can_download) return "";
      return '<a class="button button-secondary" href="/download/' + encodeURIComponent(result.doc_id) + '">Download a copy</a>';
    }
    if (result.access_status === "pending") {
      return '<span class="status-chip status-chip-warn">Access requested</span> <a href="/access-requests" data-nav>See my requests</a>';
    }
    return (
      '<button type="button" class="button button-secondary access-request-btn" data-doc-id="' +
      escapeHtml(result.doc_id) +
      '" data-doc-title="' +
      escapeHtml(result.title || result.doc_id) +
      '">Ask for access</button>'
    );
  }

  function renderResults(results, q) {
    return (
      '<ul class="card-list search-result-list" role="list">' +
      results
        .map(function (result) {
          var meta = [escapeHtml(result.doc_id)];
          if (result.doc_date) meta.push(escapeHtml(shortDate(result.doc_date)));
          if (result.department) meta.push(escapeHtml(result.department));
          if (result.reference_number) meta.push("Ref. " + escapeHtml(result.reference_number));
          if (result.metadata_check_pending) {
            meta.push('<span class="status-chip status-chip-warn" title="Filled in by the AI and not yet checked by a person">Details not yet checked</span>');
          }
          var page = parseInt(result.page, 10);
          var reasons = (Array.isArray(result.match_reasons) ? result.match_reasons : [result.source])
            .map(function (r) { return MATCH_LABELS[r]; })
            .filter(Boolean);
          var found = "";
          var pageLinks = renderPageLinks(result, q);
          if (result.can_view !== false && reasons.length) {
            found =
              '<p class="list-card-meta">Found in ' +
              escapeHtml(reasons.join(", ")) +
              (page > 0 && !pageLinks ? " · page " + page : "") +
              "</p>";
          }
          found += pageLinks;
          var snippet = "";
          if (result.can_view === false) {
            snippet = '<p class="muted">You can see this document exists. Ask for access to read it.</p>';
          } else if (result.snippet) {
            snippet = '<p class="result-snippet">“' + escapeHtml(result.snippet) + '”</p>';
          }
          var actions = accessHtml(result);
          return (
            '<li class="list-card search-result">' +
            '<div class="list-card-top"><h3>' +
            renderAccessActions(result, q) +
            '</h3><span class="status-chip status-chip-neutral">' +
            escapeHtml(result.doc_type || "Document") +
            "</span></div>" +
            '<p class="list-card-meta">' + meta.join(" · ") + "</p>" +
            snippet +
            found +
            (actions ? '<div class="list-card-actions">' + actions + "</div>" : "") +
            "</li>"
          );
        })
        .join("") +
      "</ul>"
    );
  }

  function loadSearchFacets(root) {
    var selects = root.querySelectorAll("select[data-facet]");
    if (!selects.length || root.dataset.facetsLoaded) return Promise.resolve();
    root.dataset.facetsLoaded = "1";
    return fetch("/api/search/facets", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("facets");
        return response.json();
      })
      .then(function (facets) {
        Array.prototype.forEach.call(selects, function (select) {
          var values = facets[select.getAttribute("data-facet")] || [];
          var wanted = select.getAttribute("data-value") || "";
          values.forEach(function (entry) {
            var option = document.createElement("option");
            var value = entry && typeof entry === "object" ? entry.value : entry;
            option.value = value;
            option.textContent = entry && typeof entry === "object" ? entry.label : entry;
            if (value === wanted) option.selected = true;
            select.appendChild(option);
          });
          if (wanted && select.value !== wanted) {
            var extra = document.createElement("option");
            extra.value = wanted;
            extra.textContent = wanted;
            extra.selected = true;
            select.appendChild(extra);
          }
        });
      })
      .catch(function () {
        delete root.dataset.facetsLoaded;
      });
  }

  var FILTER_FIELDS = ["doc_type", "department", "category", "subject", "title", "language", "tags", "metadata_check", "date_from", "date_to", "reference_number", "doc_id", "uploaded_by"];
  var REFINE_FIELDS = ["doc_type", "department", "category", "subject", "title", "language", "tags", "metadata_check", "date_from", "date_to", "reference_number"];

  function fieldValue(id) {
    var el = document.getElementById(id);
    return el && el.value ? String(el.value).trim() : "";
  }

  function currentFilters() {
    var out = {};
    FILTER_FIELDS.forEach(function (key) {
      var value = fieldValue("f_" + key);
      if (value) out[key] = value;
    });
    return out;
  }

  function paintFilterCount() {
    var badge = document.getElementById("filterCount");
    if (!badge) return;
    var n = Object.keys(currentFilters()).length;
    badge.hidden = !n;
    badge.textContent = n ? String(n) : "";
  }

  function openFilters(open) {
    var panel = document.getElementById("filtersPanel");
    var toggle = document.getElementById("filtersToggle");
    if (!panel || !toggle) return;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function showSearchPanel(which) {
    openFilters(which === "metadata");
  }

  function searchLang() {
    var checked = document.querySelector('input[data-search-lang]:checked');
    return checked ? checked.value : readStorage("sb-search-lang", "bcb-search-lang") || "en";
  }

  function initSearchLang() {
    if (!document.querySelector('[data-page="search"]')) return;
    var params = new URLSearchParams(location.search);
    var lang = params.get("lang") || readStorage("sb-search-lang", "bcb-search-lang") || "en";
    document.querySelectorAll("input[data-search-lang]").forEach(function (input) {
      input.checked = input.value === lang;
    });
    var query = document.getElementById("searchQuery");
    if (query) query.setAttribute("lang", lang === "bn" ? "bn" : "en");
  }

  function onSearchLangClick(e) {
    var input = e.target.closest && e.target.closest("input[data-search-lang]");
    if (!input || !document.querySelector('[data-page="search"]')) return;
    writeStorage("sb-search-lang", input.value === "bn" ? "bn" : "en");
    var query = document.getElementById("searchQuery");
    if (query) query.setAttribute("lang", input.value === "bn" ? "bn" : "en");
  }

  var searchSeq = 0;

  function runSearch(pushUrl) {
    var results = document.getElementById("searchResults");
    var button = document.getElementById("searchButton");
    if (!results) return;
    var q = fieldValue("searchQuery");
    var filters = currentFilters();
    var lang = searchLang();
    paintFilterCount();

    var urlParams = new URLSearchParams();
    if (q) urlParams.set("q", q);
    Object.keys(filters).forEach(function (k) { urlParams.set(k, filters[k]); });
    if (lang === "bn") urlParams.set("lang", "bn");
    var nextUrl = "/search" + (urlParams.toString() ? "?" + urlParams.toString() : "");
    if (pushUrl && nextUrl !== location.pathname + location.search) {
      history.replaceState({ turbo: true }, "", nextUrl);
    }

    if (!q && !Object.keys(filters).length) {
      results.innerHTML = results.dataset.idleHtml || "";
      return;
    }

    var apiUrl;
    if (q) {
      var p = new URLSearchParams({ q: q, lang: lang });
      REFINE_FIELDS.forEach(function (k) { if (filters[k]) p.set(k, filters[k]); });
      if (filters.doc_id || filters.uploaded_by) {
        // These two only narrow the filter-only search; mention it rather than silently ignoring them.
        results.dataset.note = "Archive number and uploader filters apply when the search box is empty.";
      } else {
        delete results.dataset.note;
      }
      apiUrl = "/api/search?" + p.toString();
    } else {
      delete results.dataset.note;
      apiUrl = "/api/search/metadata?" + new URLSearchParams(filters).toString();
    }

    var seq = (searchSeq += 1);
    if (button) button.disabled = true;
    results.setAttribute("aria-busy", "true");
    results.innerHTML = skeletonHtml();
    fetch(apiUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 400) {
          return response.json().then(function (body) {
            var err = new Error(body.detail || "Check the filters");
            err.plain = true;
            throw err;
          });
        }
        if (response.status === 401) {
          window.location.href = "/login";
          throw new Error("auth");
        }
        if (!response.ok) throw new Error("Search failed");
        return response.json();
      })
      .then(function (payload) {
        if (seq !== searchSeq) return;
        var list = payload.results || [];
        if (!list.length) {
          results.innerHTML = emptyHtml(
            "No documents found",
            q ? "Nothing matched “" + q + "”. Try fewer or different words, or remove some filters." : "No documents match these filters."
          );
          announce("No documents found");
          return;
        }
        var head =
          '<p class="search-count" role="status">' +
          list.length +
          (list.length === 1 ? " document" : " documents") +
          "</p>";
        if (payload.degraded) {
          head += '<p class="banner info">Showing simple word matches for now. Results may be less complete than usual.</p>';
        }
        if (results.dataset.note) head += '<p class="muted small">' + escapeHtml(results.dataset.note) + "</p>";
        results.innerHTML = head + renderResults(list, q);
        announce(list.length + (list.length === 1 ? " document found" : " documents found"));
      })
      .catch(function (err) {
        if (seq !== searchSeq || (err && err.message === "auth")) return;
        var message = err && err.plain ? err.message : friendlyError(err && err.message, "search_unavailable");
        results.innerHTML = emptyHtml("Search didn't work", message);
      })
      .finally(function () {
        if (seq !== searchSeq) return;
        if (button) button.disabled = false;
        results.removeAttribute("aria-busy");
      });
  }

  function initSearchPage() {
    var root = document.querySelector('[data-page="search"]');
    if (!root) return;
    var results = document.getElementById("searchResults");
    if (results && !results.dataset.idleHtml) results.dataset.idleHtml = results.innerHTML;
    var params = new URLSearchParams(location.search);
    var hasFilters = FILTER_FIELDS.some(function (k) { return params.get(k); });
    if (hasFilters) openFilters(true);
    loadSearchFacets(root).then(function () {
      paintFilterCount();
      if ((params.get("q") || "").trim() || hasFilters) runSearch(false);
    });
    var form = document.getElementById("searchForm");
    if (form && !form.dataset.bound) {
      form.dataset.bound = "1";
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        runSearch(true);
      });
      form.addEventListener("change", function (e) {
        if (e.target && e.target.matches && e.target.matches("select, input[type=date]")) paintFilterCount();
      });
    }
    if (!(params.get("q") || "").trim() && !hasFilters) {
      var input = document.getElementById("searchQuery");
      if (input && window.matchMedia && window.matchMedia("(min-width: 900px)").matches) input.focus();
    }
  }

  function requestAccess(btn) {
    var docId = btn.getAttribute("data-doc-id") || "";
    var title = btn.getAttribute("data-doc-title") || docId;
    if (!docId) return;
    promptAction({
      title: "Ask for access to " + title,
      text: "An administrator or the board secretary will look at your request. You'll get a notification when they decide.",
      label: "Why do you need it?",
      value: "Board affairs review",
      ok: "Send request",
    }).then(function (purpose) {
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
          var wrap = btn.parentNode;
          if (wrap) wrap.innerHTML = '<span class="status-chip status-chip-warn">Access requested</span> <a href="/access-requests" data-nav>See my requests</a>';
          toast("Request sent. You'll be notified when it's decided.");
        })
        .catch(function () {
          btn.disabled = false;
          toast("We couldn't send the request. Please try again.");
        });
    });
  }

  function onIngestRetryClick(e) {
    var btn = e.target.closest && e.target.closest("[data-ingest-retry]");
    if (!btn || btn.disabled) return;
    e.preventDefault();
    btn.disabled = true;
    fetch("/api/documents/" + encodeURIComponent(btn.getAttribute("data-ingest-retry")) + "/ingest/retry", {
      method: "POST",
      credentials: "same-origin",
      headers: window.BCPCsrf ? window.BCPCsrf.headers() : {},
    })
      .then(function (response) {
        if (!response.ok) throw new Error("retry");
        toast("Trying again. This page will update in a moment.");
        setTimeout(function () { visit(location.pathname + location.search); }, 2500);
      })
      .catch(function () {
        btn.disabled = false;
        toast("We couldn't start it again. Please try once more.");
      });
  }

  function onSearchUiClick(e) {
    if (!document.querySelector('[data-page="search"]')) return;
    var toggle = e.target.closest("#filtersToggle");
    if (toggle) {
      e.preventDefault();
      var panel = document.getElementById("filtersPanel");
      openFilters(panel && panel.hidden);
      return;
    }
    if (e.target.closest("#filtersClear")) {
      e.preventDefault();
      FILTER_FIELDS.forEach(function (k) {
        var el = document.getElementById("f_" + k);
        if (el) el.value = "";
      });
      runSearch(true);
      return;
    }
    var example = e.target.closest("[data-search-example]");
    if (example) {
      e.preventDefault();
      var input = document.getElementById("searchQuery");
      if (input) input.value = example.textContent.trim();
      runSearch(true);
      return;
    }
    var accessBtn = e.target.closest(".access-request-btn");
    if (accessBtn) {
      e.preventDefault();
      requestAccess(accessBtn);
    }
  }

  function onSearchKeydown() {}

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

  var sectionObserver = null;

  /** Section tabs (`.section-tabs a[href="#id"]`) show which part of the page is on screen. */
  function initSectionTabs() {
    if (sectionObserver) {
      sectionObserver.disconnect();
      sectionObserver = null;
    }
    var nav = document.querySelector(".section-tabs");
    if (!nav) return;
    var links = Array.prototype.slice.call(nav.querySelectorAll('a[href^="#"]'));
    var targets = links
      .map(function (link) { return document.getElementById(link.getAttribute("href").slice(1)); })
      .filter(Boolean);
    if (!targets.length) return;

    function mark(id) {
      links.forEach(function (link) {
        var on = link.getAttribute("href") === "#" + id;
        link.classList.toggle("is-active", on);
        if (on) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
    }
    nav.addEventListener("click", function (e) {
      var link = e.target.closest && e.target.closest('a[href^="#"]');
      if (link) mark(link.getAttribute("href").slice(1));
    });
    var initial = location.hash ? document.getElementById(location.hash.slice(1)) : null;
    var owner = initial && targets.filter(function (t) { return t === initial || t.contains(initial); })[0];
    mark((owner || targets[0]).id);
    if (typeof IntersectionObserver !== "function") return;
    var visible = {};
    sectionObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) { visible[entry.target.id] = entry.isIntersecting; });
        for (var i = 0; i < targets.length; i += 1) {
          if (visible[targets[i].id]) {
            mark(targets[i].id);
            return;
          }
        }
      },
      { rootMargin: "-30% 0px -60% 0px" }
    );
    targets.forEach(function (t) { sectionObserver.observe(t); });
  }

  function initTagInputs() {
    document.querySelectorAll("[data-tag-input]").forEach(function (root) {
      if (root.dataset.bound) return;
      root.dataset.bound = "1";
      var chips = root.querySelector("[data-tag-chips]");
      var entry = root.querySelector("[data-tag-entry]");
      var hidden = root.querySelector("[data-tag-value]");
      var options = entry ? document.getElementById(entry.getAttribute("list")) : null;
      var lookup = null;

      function labels() {
        return Array.prototype.map.call(chips.querySelectorAll(".tag-chip"), function (li) {
          return li.getAttribute("data-label") || "";
        });
      }
      function sync() { hidden.value = labels().join(", "); }
      function add(raw) {
        var label = (raw || "").replace(/\s+/g, " ").trim().slice(0, 120);
        if (!label) return;
        var key = label.toLowerCase();
        if (labels().some(function (l) { return l.toLowerCase() === key; })) return;
        var li = document.createElement("li");
        li.className = "tag-chip";
        li.setAttribute("data-label", label);
        li.innerHTML = "<span>" + escapeHtml(label) + "</span>" +
          '<button type="button" class="tag-chip-remove" aria-label="Remove ' + escapeHtml(label) + '">&times;</button>';
        chips.appendChild(li);
        sync();
      }
      chips.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest(".tag-chip-remove") : null;
        if (!btn) return;
        var li = btn.closest(".tag-chip");
        if (li) li.remove();
        sync();
        if (entry) entry.focus();
      });
      if (!entry) return;
      entry.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === ",") {
          e.preventDefault();
          add(entry.value);
          entry.value = "";
        } else if (e.key === "Backspace" && !entry.value) {
          var last = chips.querySelector(".tag-chip:last-child");
          if (last) { last.remove(); sync(); }
        }
      });
      entry.addEventListener("change", function () {
        if (options && Array.prototype.some.call(options.options, function (o) { return o.value === entry.value; })) {
          add(entry.value);
          entry.value = "";
        }
      });
      entry.addEventListener("blur", function () {
        if (entry.value.trim()) { add(entry.value); entry.value = ""; }
      });
      entry.addEventListener("input", function () {
        if (!options) return;
        clearTimeout(lookup);
        var q = entry.value.trim();
        lookup = setTimeout(function () {
          fetch("/api/tags?q=" + encodeURIComponent(q), { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then(function (r) { return r.ok ? r.json() : { tags: [] }; })
            .then(function (data) {
              options.innerHTML = (data.tags || []).map(function (t) {
                return '<option value="' + escapeHtml(t.label) + '"></option>';
              }).join("");
            })
            .catch(function () {});
        }, 180);
      });
    });
  }

  function bootPage() {
    if (window.BCPCsrf && typeof window.BCPCsrf.injectForms === "function") {
      window.BCPCsrf.injectForms();
    }
    markInstalledApp();
    initTheme();
    initSectionTabs();
    initMoreSheet();
    initPullToRefresh();
    initInstallHint();
    initSearchPage();
    initSearchLang();
    initTagInputs();
    initAppearancePage();
    updateOnlineState();
    refreshUnreadCount(false);
    document.body.classList.add("ready");
  }

  // Capture phase so it wins even if other handlers stop bubbling
  document.addEventListener("click", onConfirmClick, true);
  document.addEventListener("submit", onConfirmSubmit, true);
  document.addEventListener("submit", onGlobalSearchSubmit);
  document.addEventListener("click", onSearchUiClick, true);
  document.addEventListener("click", onThemeClick, true);
  document.addEventListener("change", onAppearanceChange, true);
  document.addEventListener("click", onSearchLangClick, true);
  document.addEventListener("click", onAdminUsersClick, true);
  document.addEventListener("click", onIngestRetryClick, true);
  document.addEventListener("keydown", onSearchKeydown, true);
  document.addEventListener("keydown", onSheetKeydown);
  document.addEventListener("click", onClick);
  window.addEventListener("popstate", onPopState);
  window.addEventListener("online", updateOnlineState);
  window.addEventListener("offline", updateOnlineState);
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) releaseBusyButtons();
  });
  if (darkQuery && typeof darkQuery.addEventListener === "function") {
    darkQuery.addEventListener("change", function () { applyAppearance(null, false); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    registerServiceWorker();
    bootPage();
  });

  window.BCPNav = {
    visit: visit,
    prefetch: prefetch,
    showSearchPanel: showSearchPanel,
    applyTheme: applyTheme,
    applyAppearance: applyAppearance,
  };
  window.BCPUI = {
    toast: toast,
    confirm: confirmAction,
    prompt: promptAction,
    escapeHtml: escapeHtml,
    friendlyError: friendlyError,
    refreshUnreadCount: refreshUnreadCount,
    setLeaveGuard: setLeaveGuard,
  };
})();
