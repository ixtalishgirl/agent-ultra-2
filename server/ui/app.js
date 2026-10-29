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
  var messages = [];
  var screenUrl = null;

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
        copy_id: browserCopy.value.trim() || "primary",
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

  function pollScreenshot() {
    fetch("/api/browser/screenshot")
      .then(function (resp) {
        if (resp.status === 404) {
          if (screenUrl) {
            URL.revokeObjectURL(screenUrl);
            screenUrl = null;
          }
          screen.hidden = true;
          screen.removeAttribute("src");
          browserStatus.textContent = "No live browser session.";
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
        browserStatus.textContent = "Live browser screen.";
      })
      .catch(function () {
        browserStatus.textContent = "No live browser session.";
      });
  }

  pollScreenshot();
  setInterval(pollScreenshot, 2000);
})();
