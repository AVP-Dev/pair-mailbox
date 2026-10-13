/* Dashboard client. Draws the inlined snapshot and requests NOTHING.
 * All snapshot text inlines via textContent.
 * Invariant: Zero HTML Injection.
 */
(function () {
  "use strict";

  var THEME_KEY = "pairboard.theme";
  var WATCH_KEY = "pairboard.watch";

  var LABELS = window.PAIRBOARD_LABELS || {};
  var ALARM = /(pane_dead|pane_missing|collision|restart_failed|restart_impossible|refused|release)/;
  var SILENCE_WARN = 300;
  var SILENCE_BAD = 1800;

  function readSnapshot() {
    try {
      return JSON.parse(document.getElementById("snapshot").textContent);
    } catch (error) {
      return null;
    }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function panel(title) {
    var box = el("section", "panel");
    box.appendChild(el("h2", null, title));
    return box;
  }

  function kv(pairs) {
    var list = el("dl", "kv");
    pairs.forEach(function (pair) {
      list.appendChild(el("dt", null, pair[0]));
      list.appendChild(el("dd", pair[2] || null, pair[1]));
    });
    return list;
  }

  function roleStatus(role, closed) {
    if (role.pane_alive === false || role.pid_alive === false) return "bad";
    if (closed) return "unknown";
    if (role.pane_alive === null || role.silence_seconds === null ||
        role.silence_seconds === undefined) return "unknown";
    if (role.silence_seconds > SILENCE_BAD) return "bad";
    if (role.silence_seconds > SILENCE_WARN) return "warn";
    return "ok";
  }

  function clock(stamp) {
    if (!stamp) return "—";
    var moment = new Date(stamp);
    if (isNaN(moment.getTime())) return "—";
    return moment.toISOString().slice(11, 19);
  }

  function remember(key, value) {
    try { localStorage.setItem(key, value); } catch (e) {}
  }

  function recall(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var button = document.getElementById("theme-toggle");
    var dark = theme === "dark";
    if (button) {
      button.textContent = dark ? "light" : "dark";
      button.setAttribute("aria-pressed", String(dark));
    }
    remember(THEME_KEY, theme);
  }

  function restoreTheme() {
    var saved = recall(THEME_KEY);
    applyTheme(saved === "light" || saved === "dark" ? saved : "dark");
  }

  function renderRound(snap) {
    var info = snap.round || {};
    var box = panel("round " + (info.number === undefined ? "?" : info.number));
    if (info.task) {
      var goal = el("div", "hero-goal");
      goal.appendChild(el("span", "hero-badge", "GOAL"));
      goal.appendChild(el("span", "hero-text", info.task));
      box.appendChild(goal);
    }
    var track = el("div", "phases");
    (info.phases || []).forEach(function (name, index) {
      var step = el("div", "phase", name);
      if (info.phase_index && index + 1 < info.phase_index) step.classList.add("done");
      if (name === info.phase) step.classList.add("now");
      track.appendChild(step);
    });
    box.appendChild(track);
    box.appendChild(kv([
      ["phase", info.phase || "?", info.closed ? "ok" : "warn"],
      ["mode", info.mode === "audit" ? "AUDIT (no edits)" : (info.mode || "fix")],
      ["autonomy", info.autonomy || "?"],
      ["writes", (info.writer || "\u2014") + (info.writer_reason ? " — " + info.writer_reason : "")],
      ["state recorded", info.state_age || "\u2014", info.state_age_seconds > 900 ? "warn" : ""],
      ["events", (snap.counts ? snap.counts.events_shown + " of " + snap.counts.events_total : "?") +
        (snap.watch ? " \u00b7 live" : " \u00b7 static")]
    ]));
    if (info.task) box.appendChild(el("p", "muted", info.task));
    return box;
  }

  function renderRoles(snap) {
    var box = panel("agents");
    var roles = snap.roles || [];
    if (!roles.length) {
      box.appendChild(el("p", "muted", "no roles registered"));
      return box;
    }
    var closed = !!(snap.round && snap.round.closed);
    roles.forEach(function (role) {
      var line = el("div", "role");
      line.appendChild(el("span", "dot " + roleStatus(role, closed)));
      line.appendChild(el("span", "role-name", role.role));
      if (role.is_writer) {
        line.appendChild(el("span", "tag writer", "writer"));
      } else {
        line.appendChild(el("span", "tag critic", "critic"));
      }
      var paneText = "pane " + (role.pane || "\u2014");
      if (role.pane_human && role.pane_human !== role.pane) {
        paneText += " [" + role.pane_human + "]";
      }
      line.appendChild(el("span", "muted", paneText +
        " · pid " + (role.pid == null ? "—" : role.pid) +
        (role.pid == null ? "" : (role.pid_alive ? " alive" : " dead")) +
        " \u00b7 silent " + (role.silence || "\u2014")));
      if (role.pane_note) line.appendChild(el("span", "warn", role.pane_note));
      if (role.restored_by) line.appendChild(el("span", "muted", role.restored_by));
      box.appendChild(line);
    });
    return box;
  }

  function renderPilot(snap) {
    var pilot = snap.pilot || {};
    var box = panel("supervisor");
    box.appendChild(kv([
      ["process", pilot.running ? "alive, pid " + pilot.pid : "not found",
        pilot.running ? "ok" : "warn"],
      ["log", (pilot.log_size || 0) + " B, changed " + pilot.log_age + " ago"],
      ["report", pilot.report_exists ? "yes, updated " + pilot.report_age + " ago" : "no",
        pilot.report_exists ? "" : "warn"],
      ["refusals in shown events", String(pilot.refusals === undefined ? 0 : pilot.refusals),
        pilot.refusals ? "warn" : ""],
      ["last action", pilot.last || "\u2014"]
    ]));
    return box;
  }

  function taskNode(task) {
    var line = el("div", "task " + task.status);
    line.appendChild(el("div", "text", "#" + task.id + " " + task.text));
    line.appendChild(el("div", "muted", task.status + " \u00b7 round " + (task.round === null ? "?" : task.round) +
      (task.added_by ? " \u00b7 " + task.added_by : "") + (task.closed_by ? " \u00b7 closed by " + task.closed_by : "")));
    if (task.reason) line.appendChild(el("div", "muted", "cause: " + task.reason));
    return line;
  }

  function renderTasks(snap) {
    var box = panel("tasks");
    var tasks = snap.tasks || [];
    if (!tasks.length) return box.appendChild(el("p", "muted", "no tasks")), box;
    var open = [], done = [];
    tasks.forEach(function (t) { (t.status === "open" ? open : done).push(t); });
    if (!open.length) box.appendChild(el("p", "tasks-clean", "All current tasks done"));
    else open.forEach(function (t) { box.appendChild(taskNode(t)); });
    if (done.length) {
      var det = el("details", "task-archive");
      det.appendChild(el("summary", "muted", "Task archive: " + done.length));
      done.forEach(function (t) { det.appendChild(taskNode(t)); });
      box.appendChild(det);
    }
    return box;
  }

  function renderDiscussion(snap) {
    var box = panel("latest discussion");
    var letters = snap.discussion || [];
    if (!letters.length) {
      box.appendChild(el("p", "muted", "no letters with text — only pings or silence"));
      return box;
    }
    letters.forEach(function (letter) {
      var line = el("div", "event chat-msg");
      line.appendChild(el("span", "muted", clock(letter.ts)));
      line.appendChild(el("span", "kind", (letter.from || "?") + " → " + (letter.to || "?")));
      line.appendChild(el("div", "sum", (letter.subject ? letter.subject + " — " : "") + (letter.text || "")));
      box.appendChild(line);
    });
    return box;
  }

  function renderWaiting(snap) {
    var box = panel("who waits on whom");
    var waiting = snap.waiting || [];
    if (!waiting.length) {
      box.appendChild(el("p", "tasks-clean", "nobody waits on anyone"));
      return box;
    }
    waiting.forEach(function (ob) {
      var line = el("div", "event");
      line.appendChild(el("span", "muted", "#" + ob.seq));
      line.appendChild(el("span", "sum", (ob.from || "?") + " waits " + (ob.to || "?") + " \u2014 " + (ob.subject || "")));
      box.appendChild(line);
    });
    return box;
  }

  function renderEvents(snap) {
    var box = panel("events");
    var list = el("div", "events");
    var events = snap.events || [];
    if (!events.length) list.appendChild(el("p", "muted", "no events"));
    events.forEach(function (event) {
      var line = el("div", "event");
      if (ALARM.test(event.kind)) line.classList.add("alarm");
      if (event.kind === "send") line.classList.add("chat-msg");
      line.appendChild(el("span", "muted", clock(event.ts)));
      line.appendChild(el("span", "kind", LABELS[event.kind] || event.kind));
      line.appendChild(el("span", "sum", (event.role ? event.role + " " : "") +
        (event.to ? "→ " + event.to + " " : "") + (event.summary || "")));
      list.appendChild(line);
    });
    box.appendChild(list);
    return box;
  }


  function render(snap) {
    var app = document.getElementById("app");
    while (app.firstChild) app.removeChild(app.firstChild);
    if (!snap) {
      app.appendChild(el("p", "banner", "State snapshot unread: the page is broken. " +
        "Rebuild: bin/pairboard"));
      return;
    }
    if (snap.read_error) {
      app.appendChild(el("p", "banner", "state.json unread: " + snap.read_error));
    }
    document.getElementById("project").textContent =
      (snap.project || "pair") + " \u00b7 writer " + ((snap.round && snap.round.writer) || "\u2014");
    var stamp = clock(snap.generated_at);
    document.getElementById("stamp").textContent = stamp === "\u2014" ? "build time unknown"
      : "built " + stamp + " UTC" + (snap.watch ? " \u00b7 auto" : " \u00b7 static");

    var topGrid = el("div", "grid kpi-grid");
    topGrid.appendChild(renderRound(snap));
    topGrid.appendChild(renderPilot(snap));
    app.appendChild(topGrid);

    var mainGrid = el("div", "dashboard-grid");
    var colLeft = el("div", "col-left");
    colLeft.appendChild(renderRoles(snap));
    colLeft.appendChild(renderTasks(snap));
    mainGrid.appendChild(colLeft);

    var colRight = el("div", "col-right");
    colRight.appendChild(renderWaiting(snap));
    colRight.appendChild(renderDiscussion(snap));
    colRight.appendChild(renderEvents(snap));
    mainGrid.appendChild(colRight);

    app.appendChild(mainGrid);
  }

  function watchEnabled() {
    return recall(WATCH_KEY) === "off";
  }

  function setWatch(paused) {
    remember(WATCH_KEY, paused ? "off" : "on");
  }

  function applyWatchState() {
    var paused = watchEnabled();
    var button = document.getElementById("watch-toggle");
    if (button) {
      button.textContent = paused ? "resume" : "pause";
      button.setAttribute("aria-pressed", String(paused));
    }
    var existing = document.querySelector('meta[http-equiv="refresh"]');
    if (existing) existing.remove();
    if (paused) return;
    var seconds = parseInt(document.body.getAttribute("data-watch-seconds"), 10);
    if (!(seconds > 0)) return;
    var fresh = document.createElement("meta");
    fresh.setAttribute("http-equiv", "refresh");
    fresh.setAttribute("content", String(seconds));
    document.head.appendChild(fresh);
  }

  function wireToggles() {
    var themeButton = document.getElementById("theme-toggle");
    if (themeButton) {
      themeButton.addEventListener("click", function () {
        var current = document.documentElement.getAttribute("data-theme");
        applyTheme(current === "dark" ? "light" : "dark");
      });
    }
    var watchButton = document.getElementById("watch-toggle");
    if (watchButton) {
      watchButton.addEventListener("click", function () {
        setWatch(!watchEnabled());
        applyWatchState();
      });
    }
  }

  restoreTheme();
  wireToggles();
  applyWatchState();
  render(readSnapshot());
})();
