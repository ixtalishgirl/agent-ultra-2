(function () {
  "use strict";

  var chatArea = document.getElementById("chat-area");
  var chatForm = document.getElementById("chat-form");
  var chatInput = document.getElementById("chat-input");
  var screen = document.getElementById("browser-screen");
  var browserStatus = document.getElementById("browser-status");
  var browserForm = document.getElementById("browser-form");
  var browserCopy = document.getElementById("browser-copy");
  var browserUrl = document.getElementById("browser-url");
  var browserResult = document.getElementById("browser-result");
  var browserActionForm = document.getElementById("browser-action-form");
  var browserAction = document.getElementById("browser-action");
  var browserSelector = document.getElementById("browser-selector");
  var browserAmount = document.getElementById("browser-amount");
  var copiesForm = document.getElementById("copies-form");
  var copiesTask = document.getElementById("copies-task");
  var copiesCount = document.getElementById("copies-count");
  var copiesConcurrent = document.getElementById("copies-concurrent");
  var copiesResult = document.getElementById("copies-result");
  var messages = [];
  var screenUrl = null;

  function currentCopy() {
    return browserCopy.value.trim() || "primary";
  }

  function addLine(cls, text) {
    var div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    chatArea.appendChild(div);
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  chatForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    messages.push({ role: "user", content: text });
    addLine("user", "You: " + text);
    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: messages }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        var reply = data.reply || "";
        messages.push({ role: "assistant", content: reply });
        addLine("reply", "Agent: " + reply);
        if (data.tool_rounds) {
          addLine("reply", "Browser rounds: " + data.tool_rounds);
        }
      })
      .catch(function (err) {
        addLine("reply", "Request failed: " + err);
      });
  });

  browserForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var url = browserUrl.value.trim();
    if (!url) return;
    fetch("/api/browser/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "goto",
        args: { url: url },
        copy_id: currentCopy(),
      }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        browserResult.textContent = JSON.stringify(data, null, 2);
      })
      .catch(function (err) {
        browserResult.textContent = "Request failed: " + err;
      });
  });

  browserActionForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var args = {};
    var selector = browserSelector.value.trim();
    if (selector) args.selector = selector;
    var amount = browserAmount.value.trim();
    if (amount) args.amount = parseFloat(amount);
    fetch("/api/browser/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: browserAction.value,
        args: args,
        copy_id: currentCopy(),
      }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        browserResult.textContent = JSON.stringify(data, null, 2);
      })
      .catch(function (err) {
        browserResult.textContent = "Request failed: " + err;
      });
  });

  copiesForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var button = copiesForm.querySelector("button[type=submit]");
    button.disabled = true;
    copiesResult.textContent = "Running arena...";
    fetch("/api/arena/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: copiesTask.value.trim() || "arena-task",
        copies: parseInt(copiesCount.value, 10) || 4,
        max_concurrent: parseInt(copiesConcurrent.value, 10) || 4,
      }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        var winner = data.winner || {};
        var winnerId = winner.copy_id || winner.worker_id || "none";
        var lines = [
          "copies: " + data.copies,
          "waves: " + data.waves,
          "reports: " + data.reports,
          "missing: " + JSON.stringify(data.missing),
          "winner: " + winnerId + " (" + (winner.approach || "") + ")",
          "winner result: " + JSON.stringify(winner.result),
          "assignments:",
        ];
        (data.assignments || []).forEach(function (a) {
          lines.push("  " + a.copy_id + " -> " + a.approach);
        });
        lines.push(data.status || "");
        copiesResult.textContent = lines.join("\n");
      })
      .catch(function (err) {
        copiesResult.textContent = "Request failed: " + err;
      })
      .finally(function () {
        button.disabled = false;
      });
  });

  function pollScreenshot() {
    fetch("/api/browser/screenshot?copy=" + encodeURIComponent(currentCopy()))
      .then(function (resp) {
        if (resp.status === 404) {
          if (screenUrl) {
            URL.revokeObjectURL(screenUrl);
            screenUrl = null;
          }
          screen.hidden = true;
          screen.removeAttribute("src");
          return null;
        }
        if (!resp.ok) throw new Error("screenshot HTTP " + resp.status);
        return resp.blob();
      })
      .then(function (blob) {
        if (!blob) return;
        if (screenUrl) URL.revokeObjectURL(screenUrl);
        screenUrl = URL.createObjectURL(blob);
        screen.src = screenUrl;
        screen.hidden = false;
      })
      .catch(function () {
        /* status poll below reports the state; image just stays */
      });
  }

  function pollStatus() {
    fetch("/api/browser/status")
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (data.active_alive) {
          browserStatus.textContent = "Live browser screen (copy "
            + data.active_copy_id + ", "
            + (data.active_title || data.active_url || "") + ").";
        } else if (data.active_copy_id) {
          browserStatus.textContent = "Browser session not alive "
            + "(copy " + data.active_copy_id + "). Open again.";
        } else {
          browserStatus.textContent = "No live browser session.";
        }
      })
      .catch(function () {
        browserStatus.textContent = "No live browser session.";
      });
  }

  function tick() {
    pollScreenshot();
    pollStatus();
  }

  tick();
  setInterval(tick, 2000);
})();
