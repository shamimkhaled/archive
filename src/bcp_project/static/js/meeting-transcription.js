(function () {
  var root = document.getElementById("transcription");
  var minutesRoot = document.getElementById("minutes");
  var meetingId = (root && root.getAttribute("data-meeting-id")) ||
    (minutesRoot && minutesRoot.getAttribute("data-meeting-id"));

  function csrfHeaders() {
    return window.BCPCsrf && typeof window.BCPCsrf.headers === "function"
      ? window.BCPCsrf.headers()
      : {};
  }

  if (
    minutesRoot &&
    (minutesRoot.getAttribute("data-minutes-status") === "pending" ||
      minutesRoot.getAttribute("data-minutes-status") === "generating")
  ) {
    if (meetingId) {
      window.setInterval(function () {
        fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/minutes", {
          credentials: "same-origin",
          headers: csrfHeaders(),
        })
          .then(function (res) { return res.ok ? res.json() : null; })
          .then(function (payload) {
            if (payload && payload.ready) window.location.reload();
          })
          .catch(function () {});
      }, 4000);
    }
  }

  if (!root) return;

  var status = root.getAttribute("data-transcription-status") || "off";
  meetingId = root.getAttribute("data-meeting-id") || meetingId;
  if (!meetingId) return;

  var seenIds = {};

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

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
    li.innerHTML =
      "<span class=\"transcription-speaker\">" + escapeHtml(line.speaker) +
      "</span><span class=\"transcription-text\">" + escapeHtml(line.text) + "</span>";
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
    var headers = csrfHeaders();
    fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/transcription", {
      credentials: "same-origin",
      headers: headers,
    })
      .then(function (res) {
        if (res.status === 401) {
          window.location.href = "/login";
          return null;
        }
        if (!res.ok) return null;
        return res.json();
      })
      .then(function (payload) {
        if (!payload) return;
        if (payload.processing) {
          render(payload.segments || []);
          return;
        }
        if (payload.status === "stopped" || (!payload.live && !payload.processing)) {
          window.location.reload();
        }
      })
      .catch(function () {});
  }

  if (status === "processing") {
    pollUntilReady();
    window.setInterval(pollUntilReady, 4000);
    return;
  }

  if (status !== "live") return;

  var stopBtn = document.getElementById("txStopBtn");
  var micStatus = document.getElementById("txMicStatus");
  var chunkCountEl = document.getElementById("txChunkCount");
  var mediaRecorder = null;
  var mediaStream = null;
  var seq = 0;
  var uploading = 0;
  var stopping = false;
  var socket = null;
  var pollTimer = null;

  function setMic(text) {
    if (micStatus) micStatus.textContent = text;
  }

  function setChunkCount(n) {
    if (chunkCountEl) chunkCountEl.textContent = String(n);
  }

  function connectCaptions() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + window.location.host + "/ws/meetings/" + encodeURIComponent(meetingId) + "/transcription";
    try {
      socket = new WebSocket(url);
    } catch (err) {
      setMic("Live captions via polling (WebSocket unavailable).");
      startPollFallback();
      return;
    }
    socket.addEventListener("open", function () {
      setMic("Microphone on — live captions connected.");
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
      if (data.type === "status" && data.status === "stopped") {
        window.location.reload();
      }
    });
    socket.addEventListener("close", function () {
      if (!stopping) startPollFallback();
    });
    socket.addEventListener("error", function () {
      if (!stopping) startPollFallback();
    });
  }

  function startPollFallback() {
    if (pollTimer) return;
    setMic("Microphone on — captions via polling.");
    function tick() {
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
    pollTimer = window.setInterval(tick, 2500);
  }

  function uploadBlob(blob, sequence) {
    if (!blob || !blob.size) return Promise.resolve();
    uploading += 1;
    var body = new FormData();
    body.append("seq", String(sequence));
    body.append("chunk", blob, "chunk-" + sequence + ".webm");
    var headers = csrfHeaders();
    return fetch("/api/meetings/" + encodeURIComponent(meetingId) + "/transcription/chunks", {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: body,
    })
      .then(function (res) {
        if (!res.ok) throw new Error("chunk upload failed");
        return res.json();
      })
      .then(function (data) {
        if (data && typeof data.chunk_count === "number") setChunkCount(data.chunk_count);
      })
      .finally(function () {
        uploading -= 1;
      });
  }

  function waitForUploads() {
    return new Promise(function (resolve) {
      var tries = 0;
      function tick() {
        if (uploading <= 0 || tries > 80) {
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
    var types = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
    ];
    for (var i = 0; i < types.length; i += 1) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(types[i])) {
        return types[i];
      }
    }
    return "";
  }

  function startCapture() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setMic("Microphone API not available in this browser.");
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      .then(function (stream) {
        mediaStream = stream;
        var mime = pickMime();
        var options = mime ? { mimeType: mime } : undefined;
        mediaRecorder = new MediaRecorder(stream, options);
        mediaRecorder.addEventListener("dataavailable", function (event) {
          if (!event.data || !event.data.size) return;
          var thisSeq = seq;
          seq += 1;
          uploadBlob(event.data, thisSeq).catch(function () {
            setMic("Chunk upload failed — check your connection.");
          });
        });
        mediaRecorder.start(5000);
        setMic("Microphone on — recording; waiting for live captions…");
        connectCaptions();
      })
      .catch(function (err) {
        setMic("Microphone blocked or unavailable. Allow mic access for this site, then reload.");
        console.warn(err);
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
      stopBtn.textContent = "Uploading final audio…";
    }
    setMic("Finishing upload…");

    function finish() {
      waitForUploads().then(function () {
        if (mediaStream) {
          mediaStream.getTracks().forEach(function (t) { t.stop(); });
        }
        var form = document.createElement("form");
        form.method = "post";
        form.action = (stopBtn && stopBtn.getAttribute("data-stop-url")) ||
          ("/meetings/" + meetingId + "/transcription/stop");
        document.body.appendChild(form);
        if (window.BCPCsrf && typeof window.BCPCsrf.injectForms === "function") {
          window.BCPCsrf.injectForms();
        }
        form.submit();
      });
    }

    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      mediaRecorder.addEventListener("stop", finish, { once: true });
      try {
        mediaRecorder.requestData();
      } catch (e) {}
      mediaRecorder.stop();
    } else {
      finish();
    }
  }

  if (stopBtn) stopBtn.addEventListener("click", stopAndSubmit);
  startCapture();
})();
