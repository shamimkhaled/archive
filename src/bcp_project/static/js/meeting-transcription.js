(function () {
  var root = document.getElementById("transcription");
  if (!root) return;
  if (root.getAttribute("data-transcription-status") !== "live") return;

  var log = document.getElementById("transcriptionLog");
  var meetingId = root.getAttribute("data-meeting-id");
  if (!log || !meetingId) return;

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function render(segments) {
    log.innerHTML = (segments || []).map(function (line) {
      return (
        "<li><span class=\"transcription-speaker\">" + escapeHtml(line.speaker) +
        "</span><span class=\"transcription-text\">" + escapeHtml(line.text) + "</span></li>"
      );
    }).join("");
    log.scrollTop = log.scrollHeight;
  }

  function poll() {
    var headers = window.BCPCsrf && typeof window.BCPCsrf.headers === "function"
      ? window.BCPCsrf.headers()
      : {};
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
        if (!payload.live) {
          window.location.reload();
          return;
        }
        render(payload.segments || []);
      })
      .catch(function () {});
  }

  poll();
  window.setInterval(poll, 4000);
})();
