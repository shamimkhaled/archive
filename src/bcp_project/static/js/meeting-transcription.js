(function () {
  var root = document.getElementById("transcription");
  var minutesRoot = document.getElementById("minutes");
  var meetingId = (root && root.getAttribute("data-meeting-id")) ||
    (minutesRoot && minutesRoot.getAttribute("data-meeting-id"));
  var anchor = root || minutesRoot;
  var ui = window.BCPUI || {};

  function csrfHeaders() {
    return window.BCPCsrf && typeof window.BCPCsrf.headers === "function"
      ? window.BCPCsrf.headers()
      : {};
  }

  function onPage() {
    return Boolean(anchor && anchor.isConnected);
  }

  function refreshPage() {
    if (!onPage()) return;
    if (window.BCPNav && typeof window.BCPNav.visit === "function") {
      window.BCPNav.visit(location.pathname + location.search, { force: true, replace: true });
    } else {
      window.location.reload();
    }
  }

  /** Poll while the page is open; stops by itself once the page is replaced. */
  function every(ms, fn) {
    var timer = window.setInterval(function () {
      if (!onPage()) {
        window.clearInterval(timer);
        return;
      }
      if (!document.hidden) fn();
    }, ms);
    return timer;
  }

  if (
    minutesRoot &&
    meetingId &&
    (minutesRoot.getAttribute("data-minutes-status") === "pending" ||
      minutesRoot.getAttribute("data-minutes-status") === "generating")
  ) {
    every(5000, function () {
      fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/minutes", {
        credentials: "same-origin",
        headers: csrfHeaders(),
      })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (payload) {
          if (payload && payload.ready) refreshPage();
        })
        .catch(function () {});
    });
  }

  if (!root) return;

  var status = root.getAttribute("data-transcription-status") || "off";
  meetingId = root.getAttribute("data-meeting-id") || meetingId;
  if (!meetingId) return;

  var seenIds = {};

  function ensureLog() {
    var log = document.getElementById("transcriptionLog");
    if (!log) return null;
    log.hidden = false;
    return log;
  }

  function appendSegment(line) {
    if (!line) return;
    var id = line.id != null ? String(line.id) : "";
    if (id && seenIds[id]) return;
    if (id) seenIds[id] = true;
    var log = ensureLog();
    if (!log) return;
    var li = document.createElement("li");
    var speaker = document.createElement("span");
    speaker.className = "transcription-speaker";
    speaker.textContent = line.speaker || "";
    var text = document.createElement("span");
    text.className = "transcription-text";
    text.textContent = line.text || "";
    li.appendChild(speaker);
    li.appendChild(text);
    log.appendChild(li);
    log.scrollTop = log.scrollHeight;
  }

  function render(segments) {
    var log = ensureLog();
    if (!log) return;
    seenIds = {};
    log.innerHTML = "";
    (segments || []).forEach(appendSegment);
  }

  function pollUntilReady() {
    fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/transcription", {
      credentials: "same-origin",
      headers: csrfHeaders(),
    })
      .then(function (res) {
        if (res.status === 401) {
          window.location.href = "/login";
          return null;
        }
        return res.ok ? res.json() : null;
      })
      .then(function (payload) {
        if (!payload) return;
        if (payload.processing) {
          render(payload.segments || []);
          return;
        }
        if (payload.status === "stopped" || (!payload.live && !payload.processing)) refreshPage();
      })
      .catch(function () {});
  }

  if (status === "processing") {
    pollUntilReady();
    every(5000, pollUntilReady);
    return;
  }

  if (status !== "live") return;

  var stopBtn = document.getElementById("txStopBtn");
  var micStatus = document.getElementById("txMicStatus");
  var chunkCountEl = document.getElementById("txChunkCount");
  var warnEl = document.getElementById("txUploadWarn");
  var retryBtn = document.getElementById("txRetryBtn");
  var micRetryBtn = document.getElementById("txMicRetry");
  var mediaRecorder = null;
  var mediaStream = null;
  var seq = 0;
  var uploading = 0;
  var failed = {};
  var stopping = false;
  var socket = null;
  var pollTimer = null;

  function setMic(text, tone) {
    if (!micStatus) return;
    micStatus.textContent = text;
    micStatus.className = tone ? "banner " + tone : "muted";
  }

  function setChunkCount(n) {
    if (chunkCountEl) chunkCountEl.textContent = String(n);
  }

  function failedCount() {
    return Object.keys(failed).length;
  }

  function paintFailed() {
    var n = failedCount();
    if (warnEl) {
      warnEl.hidden = !n;
      warnEl.textContent = n
        ? (n === 1 ? "1 part" : n + " parts") + " of the recording didn't upload yet. Check the connection, then try again."
        : "";
    }
    if (retryBtn) retryBtn.hidden = !n;
  }

  function stillRecording() {
    return !stopping && mediaRecorder && mediaRecorder.state !== "inactive";
  }

  function onBeforeUnload(event) {
    if (!stillRecording() && !uploading && !failedCount()) return;
    event.preventDefault();
    event.returnValue = "";
  }

  function releaseDevices() {
    try {
      if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
    } catch (e) {}
    if (mediaStream) mediaStream.getTracks().forEach(function (t) { t.stop(); });
    if (socket && socket.readyState <= 1) {
      try { socket.close(); } catch (e) {}
    }
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  window.addEventListener("beforeunload", onBeforeUnload);
  if (typeof ui.setLeaveGuard === "function") {
    ui.setLeaveGuard({
      title: "Stop recording and leave?",
      message: "The recording is still running. If you leave now, the rest of the meeting won't be recorded. Use “Stop and save” first.",
      ok: "Leave anyway",
      onLeave: function () {
        window.removeEventListener("beforeunload", onBeforeUnload);
        stopping = true;
        releaseDevices();
      },
    });
  }

  function connectCaptions() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + window.location.host + "/ws/meetings/" + encodeURIComponent(meetingId) + "/transcription";
    try {
      socket = new WebSocket(url);
    } catch (err) {
      startPollFallback();
      return;
    }
    socket.addEventListener("open", function () {
      setMic("Recording. Live captions are on.", "ok");
    });
    socket.addEventListener("message", function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (!data) return;
      if (data.type === "snapshot") {
        render(data.segments || []);
        return;
      }
      if (data.type === "segment" && data.segment) {
        appendSegment(data.segment);
        return;
      }
      if (data.type === "status" && data.status === "stopped" && !stopping) refreshPage();
    });
    socket.addEventListener("close", function () {
      if (!stopping && onPage()) startPollFallback();
    });
    socket.addEventListener("error", function () {
      if (!stopping && onPage()) startPollFallback();
    });
  }

  function startPollFallback() {
    if (pollTimer) return;
    setMic("Recording. Captions update every few seconds.", "ok");
    function tick() {
      if (!onPage()) {
        window.clearInterval(pollTimer);
        pollTimer = null;
        return;
      }
      fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/transcription", {
        credentials: "same-origin",
        headers: csrfHeaders(),
      })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (payload) {
          if (!payload || !payload.live) return;
          render(payload.segments || []);
        })
        .catch(function () {});
    }
    tick();
    pollTimer = window.setInterval(tick, 3000);
  }

  function sendChunk(blob, sequence) {
    var body = new FormData();
    body.append("seq", String(sequence));
    body.append("chunk", blob, "chunk-" + sequence + ".webm");
    return fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/transcription/chunks", {
      method: "POST",
      credentials: "same-origin",
      headers: csrfHeaders(),
      body: body,
    }).then(function (res) {
      if (res.status === 401) {
        var err = new Error("signed out");
        err.signedOut = true;
        throw err;
      }
      if (!res.ok) throw new Error("upload failed");
      return res.json();
    });
  }

  /** Upload one part, retrying with a short wait; a part that still fails is kept for "Try again". */
  function uploadBlob(blob, sequence) {
    if (!blob || !blob.size) return Promise.resolve();
    uploading += 1;
    var attempt = 0;
    function tryOnce() {
      attempt += 1;
      return sendChunk(blob, sequence).catch(function (err) {
        if (err && err.signedOut) throw err;
        if (attempt >= 3) throw err;
        return new Promise(function (resolve) { window.setTimeout(resolve, attempt * 1500); }).then(tryOnce);
      });
    }
    return tryOnce()
      .then(function (data) {
        delete failed[sequence];
        if (data && typeof data.chunk_count === "number") setChunkCount(data.chunk_count);
      })
      .catch(function (err) {
        failed[sequence] = blob;
        if (err && err.signedOut) {
          setMic("You have been signed out, so the recording can't be saved. Sign in again in another tab, then press Try again.", "error");
        }
      })
      .finally(function () {
        uploading -= 1;
        paintFailed();
      });
  }

  function retryFailed() {
    var pending = Object.keys(failed).map(function (key) {
      var blob = failed[key];
      delete failed[key];
      return uploadBlob(blob, parseInt(key, 10));
    });
    paintFailed();
    return Promise.all(pending);
  }

  function waitForUploads() {
    return new Promise(function (resolve) {
      var tries = 0;
      function tick() {
        if (uploading <= 0 || tries > 300) {
          resolve();
          return;
        }
        tries += 1;
        window.setTimeout(tick, 100);
      }
      tick();
    });
  }

  function pickMime() {
    var types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    for (var i = 0; i < types.length; i += 1) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(types[i])) {
        return types[i];
      }
    }
    return "";
  }

  function startCapture() {
    if (micRetryBtn) micRetryBtn.hidden = true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      setMic("This browser can't record sound. Use Chrome, Edge or Safari, or upload a recording afterwards.", "error");
      return;
    }
    setMic("Asking to use the microphone… Choose “Allow” if your browser asks.");
    navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      .then(function (stream) {
        if (!onPage()) {
          stream.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        mediaStream = stream;
        var mime = pickMime();
        mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        mediaRecorder.addEventListener("dataavailable", function (event) {
          if (!event.data || !event.data.size) return;
          var thisSeq = seq;
          seq += 1;
          uploadBlob(event.data, thisSeq);
        });
        mediaRecorder.start(5000);
        setMic("Recording. Waiting for live captions…", "ok");
        connectCaptions();
      })
      .catch(function (err) {
        console.warn(err);
        var denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
        setMic(
          denied
            ? "The microphone is blocked. Allow it in the browser's site settings (the icon next to the address), then press Try again."
            : "No microphone was found. Connect one, then press Try again.",
          "error"
        );
        if (micRetryBtn) micRetryBtn.hidden = false;
      });
  }

  function submitStop() {
    window.removeEventListener("beforeunload", onBeforeUnload);
    if (typeof ui.setLeaveGuard === "function") ui.setLeaveGuard(null);
    if (mediaStream) mediaStream.getTracks().forEach(function (t) { t.stop(); });
    var form = document.createElement("form");
    form.method = "post";
    form.action = (stopBtn && stopBtn.getAttribute("data-stop-url")) || "/meetings/" + meetingId + "/transcription/stop";
    document.body.appendChild(form);
    if (window.BCPCsrf && typeof window.BCPCsrf.injectForms === "function") window.BCPCsrf.injectForms();
    form.submit();
  }

  function finish() {
    if (stopBtn) stopBtn.textContent = "Saving the last part…";
    waitForUploads()
      .then(function () {
        if (!failedCount()) return true;
        return retryFailed().then(function () { return !failedCount(); });
      })
      .then(function (allSaved) {
        if (allSaved) return true;
        var confirm = ui.confirm || function () { return Promise.resolve(window.confirm("Some parts didn't upload. Finish anyway?")); };
        return confirm({
          title: "Some of the recording didn't upload",
          text: failedCount() + " short part(s) couldn't be sent. You can keep this page open and try again, or finish without them.",
          ok: "Finish without them",
          danger: true,
        });
      })
      .then(function (go) {
        if (go) {
          submitStop();
          return;
        }
        if (stopBtn) {
          stopBtn.disabled = false;
          stopBtn.textContent = "Stop and save";
        }
        setMic("Recording stopped. Press Try again to send the missing parts, then Stop and save.", "error");
      });
  }

  function stopAndSubmit() {
    if (stopping) return;
    stopping = true;
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    if (socket && socket.readyState <= 1) {
      try { socket.close(); } catch (e) {}
    }
    if (stopBtn) {
      stopBtn.disabled = true;
      stopBtn.textContent = "Saving the recording…";
    }
    setMic("Saving the recording…");
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      mediaRecorder.addEventListener("stop", finish, { once: true });
      try { mediaRecorder.requestData(); } catch (e) {}
      mediaRecorder.stop();
    } else {
      finish();
    }
  }

  if (stopBtn) {
    stopBtn.addEventListener("click", function () {
      if (stopping) {
        stopBtn.disabled = true;
        finish();
        return;
      }
      var confirm = ui.confirm;
      if (!confirm) {
        stopAndSubmit();
        return;
      }
      confirm({
        title: "Stop recording?",
        text: "The recording will be saved and the AI will write the transcript and a first draft of the minutes.",
        ok: "Stop and save",
      }).then(function (yes) {
        if (yes) stopAndSubmit();
      });
    });
  }
  if (retryBtn) retryBtn.addEventListener("click", retryFailed);
  if (micRetryBtn) micRetryBtn.addEventListener("click", startCapture);
  startCapture();
})();
