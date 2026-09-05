/* =============================================================================
 * EquityPilot — Blogger chat widget
 *
 * One <script> tag, no build step, no dependencies, no iframe.
 *
 *   <script src="https://equitypilot.in/blogger-chat-widget.js"
 *           data-api="https://equitypilot.in/api/v1"
 *           data-ticker="SHRIRAMFIN"
 *           async></script>
 *
 * Why not an iframe of the platform's own chat page: the frontend sends
 * X-Frame-Options: DENY on every route, so an embedded frame renders as a blank
 * box. It is also the wrong shape — that page is for a signed-in analyst with a
 * company workspace, and a blog reader wants one question answered.
 *
 * What this is: a small panel that talks to the public endpoints
 * (GET /blogger/status, POST /blogger/chat) and nothing else. It holds no
 * credential and needs none — the endpoints are rate limited and restricted to
 * the companies the operator published, and every answer it shows comes from
 * the platform's own retrieval with its citations attached.
 *
 * Safety properties worth knowing before editing this file:
 *
 *  - Model output is inserted with textContent, never innerHTML. An answer is
 *    untrusted text: rendering it as markup would let a passage quoted from a
 *    document execute in the blog's origin.
 *  - A citation is rendered as a link only when its URL is https. Anything else
 *    is shown as plain text.
 *  - No request carries credentials, and nothing is written to localStorage.
 *    The conversation id lives in sessionStorage, so it survives a page turn
 *    and not a browser profile.
 *  - If the platform says the feature is off, the widget renders nothing at
 *    all. A dead chat box on a public blog is worse than no chat box.
 * ========================================================================== */
(function () {
  "use strict";

  // --------------------------------------------------------------------------
  // Configuration, from the script tag's own attributes
  // --------------------------------------------------------------------------
  var script = document.currentScript;
  var data = script ? script.dataset : {};

  var API = (data.api || "https://equitypilot.in/api/v1").replace(/\/+$/, "");
  var REQUESTED_TICKER = (data.ticker || "").trim().toUpperCase();
  var HEADING = data.heading || "Ask about this stock";
  var HINT =
    data.hint ||
    "Ask in English, Hindi or Hinglish — \"Shriram Finance ka AUM kitna hai?\"";
  var ACCENT = data.accent || "#0f5c4a";

  // The platform's own limit is 12 requests a minute per address. Asking a
  // reader to wait is kinder than letting the server refuse them mid-sentence.
  var MIN_SECONDS_BETWEEN_QUESTIONS = 3;
  var MAX_RENDERED_CITATIONS = 6;
  var MAX_SNIPPET_CHARS = 280;
  var SESSION_KEY = "equitypilot.blogger.session";

  // --------------------------------------------------------------------------
  // Small helpers
  // --------------------------------------------------------------------------
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function sessionId() {
    // Per browser tab, regenerated if storage is unavailable (private mode on
    // some browsers throws). A lost id costs the reader their thread, not the
    // answer.
    try {
      var existing = window.sessionStorage.getItem(SESSION_KEY);
      if (existing) return existing;
      var fresh =
        "b" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      window.sessionStorage.setItem(SESSION_KEY, fresh);
      return fresh;
    } catch (error) {
      return "b" + Date.now().toString(36);
    }
  }

  function isSafeUrl(value) {
    return typeof value === "string" && value.indexOf("https://") === 0;
  }

  function describeFailure(response) {
    // Only the platform's own `detail` string is shown, and only when it is a
    // string: that is a message written for a reader. Anything else — a proxy's
    // HTML error page, an empty body — becomes generic wording, because the
    // widget has no way to know what it is looking at.
    if (!response) return "The chatbot could not be reached. Check your connection and try again.";
    if (response.status === 429) {
      return "That is a lot of questions at once. Please wait a moment and ask again.";
    }
    if (response.status === 503) {
      return "The chatbot is not available on this site right now.";
    }
    if (response.status === 403) {
      return "This company is not available through the blog chatbot.";
    }
    var detail = response.detail;
    if (typeof detail === "string" && detail.length && detail.length < 240) {
      return detail;
    }
    return "Something went wrong while answering. Please try again.";
  }

  // --------------------------------------------------------------------------
  // Styles, scoped to the widget's own class prefix
  // --------------------------------------------------------------------------
  function injectStyles() {
    if (document.getElementById("equitypilot-blogger-style")) return;
    var css = [
      ".epb-launcher{position:fixed;right:18px;bottom:18px;z-index:9998;",
      "border:none;border-radius:999px;padding:12px 18px;cursor:pointer;",
      "background:" + ACCENT + ";color:#fff;font:600 14px/1.2 system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans Devanagari',sans-serif;",
      "box-shadow:0 6px 20px rgba(0,0,0,.22)}",
      ".epb-panel{position:fixed;right:18px;bottom:18px;z-index:9999;width:min(380px,calc(100vw - 32px));",
      "max-height:min(620px,calc(100vh - 36px));display:none;flex-direction:column;overflow:hidden;",
      "background:#fff;color:#14201c;border:1px solid rgba(0,0,0,.12);border-radius:14px;",
      "box-shadow:0 18px 48px rgba(0,0,0,.28);",
      "font:14px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans Devanagari','Noto Sans',sans-serif}",
      ".epb-panel.epb-open{display:flex}",
      ".epb-head{display:flex;align-items:center;gap:8px;padding:12px 14px;background:" + ACCENT + ";color:#fff}",
      ".epb-title{font-weight:600;font-size:14px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".epb-icon{border:none;background:rgba(255,255,255,.16);color:#fff;border-radius:8px;",
      "width:28px;height:28px;cursor:pointer;font-size:15px;line-height:1}",
      ".epb-ticker{padding:8px 14px;border-bottom:1px solid rgba(0,0,0,.08);display:flex;gap:8px;align-items:center}",
      ".epb-ticker select{flex:1;padding:6px 8px;border:1px solid rgba(0,0,0,.2);border-radius:8px;background:#fff;",
      "font:inherit;color:inherit}",
      ".epb-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;background:#fbfcfb}",
      ".epb-hint{color:#5b6b64;font-size:13px;margin:0}",
      ".epb-msg{max-width:88%;padding:9px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word}",
      ".epb-user{align-self:flex-end;background:" + ACCENT + ";color:#fff;border-bottom-right-radius:3px}",
      ".epb-ai{align-self:flex-start;background:#fff;border:1px solid rgba(0,0,0,.12);border-bottom-left-radius:3px}",
      ".epb-error{align-self:flex-start;background:#fff4f2;border:1px solid #f0c3b9;color:#8c2f1c}",
      ".epb-pending{align-self:flex-start;color:#5b6b64;font-size:13px}",
      ".epb-pending span{animation:epb-blink 1.2s infinite}",
      ".epb-pending span:nth-child(2){animation-delay:.2s}.epb-pending span:nth-child(3){animation-delay:.4s}",
      "@keyframes epb-blink{0%,60%,100%{opacity:.25}30%{opacity:1}}",
      ".epb-meta{align-self:flex-start;max-width:88%;font-size:12px;color:#5b6b64}",
      ".epb-warn{color:#8a5b00}",
      ".epb-sources{align-self:flex-start;max-width:88%;display:flex;flex-direction:column;gap:4px;",
      "font-size:12px;border-top:1px dashed rgba(0,0,0,.16);padding-top:6px}",
      ".epb-sources b{color:#3d4c46;font-weight:600}",
      ".epb-sources a{color:" + ACCENT + ";text-decoration:underline;word-break:break-all}",
      ".epb-snippet{color:#5b6b64;font-style:italic}",
      ".epb-disclosure{align-self:flex-start;max-width:88%;font-size:11px;color:#7b8880}",
      ".epb-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid rgba(0,0,0,.1);background:#fff}",
      ".epb-form textarea{flex:1;resize:none;min-height:38px;max-height:110px;padding:9px 10px;",
      "border:1px solid rgba(0,0,0,.2);border-radius:9px;font:inherit;color:inherit;background:#fff}",
      ".epb-form textarea:focus{outline:2px solid " + ACCENT + ";outline-offset:-1px}",
      ".epb-send{border:none;border-radius:9px;padding:0 14px;background:" + ACCENT + ";color:#fff;",
      "font:600 13px/1 inherit;cursor:pointer}",
      ".epb-send[disabled]{opacity:.5;cursor:default}",
      ".epb-foot{padding:6px 12px 10px;font-size:11px;color:#7b8880;background:#fff;text-align:center}",
      ".epb-foot a{color:inherit}",
      "@media (prefers-color-scheme:dark){",
      ".epb-panel{background:#16211d;color:#e8efeb;border-color:rgba(255,255,255,.14)}",
      ".epb-log{background:#101916}.epb-ai{background:#1d2a25;border-color:rgba(255,255,255,.14)}",
      ".epb-form,.epb-foot{background:#16211d}.epb-form textarea{background:#101916;color:#e8efeb;",
      "border-color:rgba(255,255,255,.2)}.epb-hint,.epb-meta,.epb-snippet{color:#a9b8b1}",
      ".epb-ticker select{background:#101916;color:#e8efeb;border-color:rgba(255,255,255,.2)}}",
      "@media (max-width:480px){.epb-panel,.epb-launcher{right:10px;bottom:10px}}",
    ].join("");
    var style = document.createElement("style");
    style.id = "equitypilot-blogger-style";
    style.appendChild(document.createTextNode(css));
    document.head.appendChild(style);
  }

  // --------------------------------------------------------------------------
  // Transport
  // --------------------------------------------------------------------------
  function request(path, options) {
    return fetch(API + path, options).then(function (response) {
      return response
        .json()
        .catch(function () {
          return null;
        })
        .then(function (payload) {
          return { ok: response.ok, status: response.status, detail: payload && payload.detail, body: payload };
        });
    });
  }

  function loadStatus() {
    // credentials omitted deliberately: nothing here is authenticated, and a
    // credentialed cross-origin request would need a stricter CORS policy for
    // no benefit.
    return request("/blogger/status", { method: "GET", credentials: "omit" });
  }

  function ask(ticker, question, session) {
    return request("/blogger/chat", {
      method: "POST",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: ticker,
        question: question,
        session_id: session,
        // "auto" is the point: a reader types Hinglish and should be answered
        // in Hinglish without being asked to choose.
        language: "auto",
      }),
    });
  }

  // --------------------------------------------------------------------------
  // Rendering
  // --------------------------------------------------------------------------
  var state = {
    ticker: REQUESTED_TICKER,
    tickers: [],
    session: sessionId(),
    busy: false,
    lastAskedAt: 0,
  };

  var nodes = {};

  function buildPanel() {
    var launcher = el("button", "epb-launcher", "Ask EquityPilot");
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open the EquityPilot chatbot");

    var panel = el("section", "epb-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "EquityPilot chatbot");

    var head = el("div", "epb-head");
    head.appendChild(el("span", "epb-title", HEADING));
    var close = el("button", "epb-icon", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close the chatbot");
    head.appendChild(close);
    panel.appendChild(head);

    // The company picker is only built when there is a choice to make.
    var tickerBar = el("div", "epb-ticker");
    tickerBar.style.display = "none";
    var select = document.createElement("select");
    select.setAttribute("aria-label", "Choose a company");
    tickerBar.appendChild(select);
    panel.appendChild(tickerBar);

    var log = el("div", "epb-log");
    log.setAttribute("aria-live", "polite");
    log.appendChild(el("p", "epb-hint", HINT));
    panel.appendChild(log);

    var form = el("form", "epb-form");
    var input = document.createElement("textarea");
    input.rows = 1;
    input.maxLength = 2000;
    input.placeholder = "Type a question…";
    input.setAttribute("aria-label", "Your question");
    var send = el("button", "epb-send", "Ask");
    send.type = "submit";
    form.appendChild(input);
    form.appendChild(send);
    panel.appendChild(form);

    var foot = el("div", "epb-foot");
    panel.appendChild(foot);

    document.body.appendChild(launcher);
    document.body.appendChild(panel);

    nodes = {
      launcher: launcher, panel: panel, close: close, log: log, form: form,
      input: input, send: send, foot: foot, tickerBar: tickerBar, select: select,
    };

    launcher.addEventListener("click", function () {
      open(true);
    });
    close.addEventListener("click", function () {
      open(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && nodes.panel.classList.contains("epb-open")) {
        open(false);
      }
    });
    select.addEventListener("change", function () {
      state.ticker = select.value;
      // A different company is a different conversation: keeping the thread
      // would carry one company's context into another's answer.
      state.session = sessionId() + "-" + state.ticker;
      resetLog();
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submit();
    });
    // Enter sends, Shift+Enter makes a new line — the convention a reader
    // already expects from every other chat box.
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submit();
      }
    });
  }

  function open(shouldOpen) {
    nodes.panel.classList.toggle("epb-open", shouldOpen);
    nodes.launcher.style.display = shouldOpen ? "none" : "";
    if (shouldOpen) nodes.input.focus();
  }

  function resetLog() {
    nodes.log.textContent = "";
    nodes.log.appendChild(el("p", "epb-hint", HINT));
  }

  function scrollDown() {
    nodes.log.scrollTop = nodes.log.scrollHeight;
  }

  function addMessage(className, text) {
    var node = el("div", "epb-msg " + className, text);
    nodes.log.appendChild(node);
    scrollDown();
    return node;
  }

  function addPending() {
    var node = el("div", "epb-pending");
    node.appendChild(document.createTextNode("Reading the documents "));
    for (var i = 0; i < 3; i += 1) node.appendChild(el("span", null, "•"));
    nodes.log.appendChild(node);
    scrollDown();
    return node;
  }

  function addSources(citations) {
    if (!citations || !citations.length) return;
    var box = el("div", "epb-sources");
    box.appendChild(el("b", null, "Sources"));
    citations.slice(0, MAX_RENDERED_CITATIONS).forEach(function (citation) {
      var row = el("div");
      var title =
        citation.document_title || citation.label || "Document";
      if (isSafeUrl(citation.url)) {
        var link = document.createElement("a");
        link.href = citation.url;
        link.target = "_blank";
        // The blog is a different origin: without these the destination gets a
        // reference to this window.
        link.rel = "noopener noreferrer nofollow";
        link.textContent = title;
        row.appendChild(link);
      } else {
        row.appendChild(document.createTextNode(title));
      }
      if (citation.page) {
        row.appendChild(document.createTextNode(" · p." + citation.page));
      }
      if (citation.snippet) {
        var snippet = String(citation.snippet).slice(0, MAX_SNIPPET_CHARS);
        row.appendChild(el("div", "epb-snippet", "“" + snippet + "…”"));
      }
      box.appendChild(row);
    });
    nodes.log.appendChild(box);
    scrollDown();
  }

  function addMeta(body) {
    var notes = [];
    // `grounded` is true only when the answer rests on retrieved evidence: the
    // citation audit passed *and* there was something to cite. An answer that
    // declined for want of evidence is false, so this note appears beside it —
    // the text says the same thing, but the widget should not be the reason a
    // reader misses it.
    if (body.grounded === false) {
      notes.push("Not grounded in an indexed document — verify before acting.");
    }
    (body.warnings || []).forEach(function (warning) {
      notes.push(String(warning));
    });
    if (body.language && body.language.native_label) {
      notes.push("Answered in " + body.language.native_label + ".");
    }
    if (!notes.length) return;
    var node = el("div", "epb-meta epb-warn", notes.join(" "));
    nodes.log.appendChild(node);
    scrollDown();
  }

  function addDisclosure(text) {
    if (!text) return;
    nodes.log.appendChild(el("div", "epb-disclosure", text));
    scrollDown();
  }

  // --------------------------------------------------------------------------
  // Conversation
  // --------------------------------------------------------------------------
  function submit() {
    if (state.busy) return;
    var question = nodes.input.value.trim();
    if (!question) return;
    if (!state.ticker) {
      addMessage("epb-error", "Choose a company first.");
      return;
    }

    // A soft client-side throttle. The server enforces the real one; this keeps
    // a reader from spending their whole minute budget with one held key.
    var wait = MIN_SECONDS_BETWEEN_QUESTIONS * 1000 - (Date.now() - state.lastAskedAt);
    if (wait > 0) {
      addMessage("epb-error", "One moment — you can ask again in a second or two.");
      return;
    }

    nodes.input.value = "";
    addMessage("epb-user", question);
    var pending = addPending();
    state.busy = true;
    state.lastAskedAt = Date.now();
    nodes.send.disabled = true;

    ask(state.ticker, question, state.session).then(
      function (response) {
        pending.remove();
        state.busy = false;
        nodes.send.disabled = false;
        if (!response.ok || !response.body) {
          addMessage("epb-error", describeFailure(response));
          return;
        }
        var body = response.body;
        // textContent, not innerHTML: the answer is model output quoting
        // documents, and this widget runs in the blog's origin.
        addMessage("epb-ai", body.answer || "");
        addMeta(body);
        addSources(body.citations);
        addDisclosure(body.disclosure);
        nodes.input.focus();
      },
      function () {
        pending.remove();
        state.busy = false;
        nodes.send.disabled = false;
        addMessage("epb-error", describeFailure(null));
      }
    );
  }

  // --------------------------------------------------------------------------
  // Start
  // --------------------------------------------------------------------------
  function start() {
    loadStatus().then(function (response) {
      var status = response.ok ? response.body : null;
      if (!status || status.enabled !== true || !status.tickers || !status.tickers.length) {
        // Not an error worth showing a reader: the operator has not published
        // anything here, so the blog simply has no chatbot. Logged for whoever
        // is wondering why the widget does not appear.
        if (window.console && console.info) {
          console.info("EquityPilot widget: the chatbot is not available on this site.");
        }
        return;
      }

      state.tickers = status.tickers;
      var published = status.tickers.map(function (row) {
        return row.ticker;
      });
      if (REQUESTED_TICKER && published.indexOf(REQUESTED_TICKER) === -1) {
        // Asked for a company the operator has not published. Falling back to
        // another company would answer a question about the wrong business.
        if (window.console && console.warn) {
          console.warn(
            "EquityPilot widget: " + REQUESTED_TICKER + " is not published; showing the available companies."
          );
        }
        state.ticker = published.length === 1 ? published[0] : "";
      } else if (!state.ticker) {
        state.ticker = published.length === 1 ? published[0] : "";
      }

      injectStyles();
      buildPanel();

      if (published.length > 1) {
        nodes.tickerBar.style.display = "";
        nodes.select.textContent = "";
        if (!state.ticker) {
          var placeholder = document.createElement("option");
          placeholder.value = "";
          placeholder.textContent = "Choose a company…";
          nodes.select.appendChild(placeholder);
        }
        status.tickers.forEach(function (row) {
          var option = document.createElement("option");
          option.value = row.ticker;
          option.textContent = row.name + " (" + row.ticker + ")";
          if (row.ticker === state.ticker) option.selected = true;
          nodes.select.appendChild(option);
        });
      }

      var totalPosts = status.tickers.reduce(function (sum, row) {
        return sum + (row.posts || 0);
      }, 0);
      var footText = "Grounded in " + totalPosts + " indexed post" + (totalPosts === 1 ? "" : "s");
      if (status.last_synced_at) {
        footText += " · updated " + String(status.last_synced_at).slice(0, 10);
      }
      nodes.foot.textContent = footText + " · Not investment advice";
    }, function () {
      // Network or CORS failure: stay silent. Rendering a broken box on a
      // public blog is worse than rendering nothing.
      if (window.console && console.info) {
        console.info("EquityPilot widget: could not reach the chatbot API.");
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
