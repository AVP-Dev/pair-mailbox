/* Клиент дашборда. Рисует снимок, вложенный в страницу, и НИЧЕГО не запрашивает.
 *
 * Почему здесь нет fetch: страница открыта с `file://`, где браузер запрещает
 * чтение локальных файлов. Единственный источник данных — JSON в соседнем
 * `<script id="snapshot" type="application/json">`. Свежесть обеспечивает
 * `<meta http-equiv="refresh">`, который ставит генератор: одно место отвечает за
 * данные, одно — за их обновление, и обе части заменяемы по отдельности.
 *
 * Весь текст из снимка вставляется через textContent. innerHTML здесь был бы не
 * «неаккуратностью», а дырой: в снимке лежит то, что писали агенты и человек
 * (текст задач, письма, причины отказов), и `</script>` или `<img onerror>` в
 * письме выполнились бы на машине человека. API, разбирающий HTML, не используется
 * ни разу, и это проверяется тестом, а не договорённостью. */
(function () {
  "use strict";

  var THEME_KEY = "pairboard.theme";
  var WATCH_KEY = "pairboard.watch";

  /* Словарь видов событий приходит отдельным ассетом (assets/labels.js): это данные,
   * а не поведение. Пустой словарь — повод показать машинные имена, а не ронять страницу. */
  var LABELS = window.PAIRBOARD_LABELS || {};

  /* Виды, после которых человек идёт смотреть. Подсвечивается строка, а не вся
   * лента: иначе красный перестаёт значить что-то ровно тогда, когда сломалось. */
  var ALARM = /(pane_dead|pane_missing|collision|restart_failed|restart_impossible|refused|release)/;

  /* Порог «молчит» тот же, что у pairpilot: молчащая, но живая роль и умершая
   * роль — разные вещи, и подсвечивать надо только вторую. */
  var SILENCE_WARN = 300;
  var SILENCE_BAD = 1800;

  function readSnapshot() {
    /* Отсутствующий узел и нечитаемый JSON дают одно и то же — null, который
     * render() показывает баннером: JSON.parse(null) вернул бы null и сам. */
    try {
      return JSON.parse(document.getElementById("snapshot").textContent);
    } catch (error) {
      return null;
    }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    /* textContent, а не innerHTML: содержимое снимка написано людьми и агентами. */
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

  /* Вердикт по трём фактам сразу. Порядок = убывание уверенности: «плохо»
   * выигрывает у «не знаю», потому что доказанная поломка должна быть видна
   * даже там, где часть фактов недоступна. Рисовать «не знаю» как «нет» нельзя:
   * человек пойдёт перезапускать то, что не сломано. */
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

  /* localStorage недоступен в приватном режиме и на части `file://`: выбор темы
   * или паузы не переживёт перезагрузку — потеря удобства, а не поломка. */
  function remember(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (error) {
      /* см. выше */
    }
  }

  function recall(key) {
    try {
      return localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var button = document.getElementById("theme-toggle");
    var dark = theme === "dark";
    if (button) {
      button.textContent = dark ? "светлая" : "тёмная";
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
    var box = panel("раунд " + (info.number === undefined ? "?" : info.number));
    var track = el("div", "phases");
    (info.phases || []).forEach(function (name, index) {
      var step = el("div", "phase", name);
      if (info.phase_index && index + 1 < info.phase_index) step.classList.add("done");
      if (name === info.phase) step.classList.add("now");
      track.appendChild(step);
    });
    box.appendChild(track);
    box.appendChild(kv([
      ["фаза", info.phase || "?", info.closed ? "ok" : "warn"],
      ["режим", info.mode === "audit" ? "AUDIT (правок нет)" : (info.mode || "fix")],
      ["автономность", info.autonomy || "?"],
      ["пишет", (info.writer || "—") + (info.writer_reason ? " — " + info.writer_reason : "")],
      ["состояние записано", info.state_age || "—", info.state_age_seconds > 900 ? "warn" : ""],
      ["событий", (snap.counts ? snap.counts.events_shown + " из " + snap.counts.events_total : "?") +
        (snap.watch ? " · обновляется" : " · не обновляется")]
    ]));
    if (info.task) box.appendChild(el("p", "muted", info.task));
    return box;
  }

  function renderRoles(snap) {
    var box = panel("агенты");
    var roles = snap.roles || [];
    if (!roles.length) {
      box.appendChild(el("p", "muted", "роли не зарегистрированы"));
      return box;
    }
    var closed = !!(snap.round && snap.round.closed);
    roles.forEach(function (role) {
      var line = el("div", "role");
      line.appendChild(el("span", "dot " + roleStatus(role, closed)));
      line.appendChild(el("span", "role-name", role.role));
      if (role.is_writer) line.appendChild(el("span", "tag writer", "writer"));
      line.appendChild(el("span", "tag", "critic"));
      line.appendChild(el("span", "muted", "панель " + (role.pane || "—") +
        " · pid " + (role.pid == null ? "—" : role.pid) +
        (role.pid == null ? "" : (role.pid_alive ? " жив" : " мёртв")) +
        " · молчит " + (role.silence || "—")));
      if (role.pane_note) line.appendChild(el("span", "warn", role.pane_note));
      if (role.restored_by) line.appendChild(el("span", "muted", role.restored_by));
      box.appendChild(line);
    });
    return box;
  }

  function renderPilot(snap) {
    var pilot = snap.pilot || {};
    var box = panel("сверхвизор");
    box.appendChild(kv([
      ["процесс", pilot.running ? "жив, pid " + pilot.pid : "не найден",
        pilot.running ? "ok" : "warn"],
      ["лог", (pilot.log_size || 0) + " б, изменён " + pilot.log_age + " назад"],
      ["отчёт", pilot.report_exists ? "есть, обновлён " + pilot.report_age + " назад"
        : "нет", pilot.report_exists ? "" : "warn"],
      ["отказов в показанных событиях", String(pilot.refusals === undefined ? 0 : pilot.refusals),
        pilot.refusals ? "warn" : ""],
      ["последнее действие", pilot.last || "—"]
    ]));
    return box;
  }

  function renderTasks(snap) {
    var box = panel("задачи");
    var tasks = snap.tasks || [];
    if (!tasks.length) {
      box.appendChild(el("p", "muted", "задач нет"));
      return box;
    }
    tasks.forEach(function (task) {
      var line = el("div", "task " + task.status);
      line.appendChild(el("div", "text", "#" + task.id + " " + task.text));
      var meta = el("div", "muted", task.status + " · раунд " + (task.round === null ? "?" : task.round) +
        (task.added_by ? " · " + task.added_by : "") +
        (task.closed_by ? " · закрыл " + task.closed_by : ""));
      line.appendChild(meta);
      if (task.reason) line.appendChild(el("div", "muted", "причина: " + task.reason));
      box.appendChild(line);
    });
    return box;
  }

  function renderEvents(snap) {
    var box = panel("события");
    var list = el("div", "events");
    var events = snap.events || [];
    if (!events.length) list.appendChild(el("p", "muted", "событий нет"));
    events.forEach(function (event) {
      var line = el("div", "event");
      if (ALARM.test(event.kind)) line.classList.add("alarm");
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
      app.appendChild(el("p", "banner", "Снимок состояния не прочитан: страница повреждена. " +
        "Пересоберите: bin/pairboard"));
      return;
    }
    if (snap.read_error) {
      app.appendChild(el("p", "banner", "state.json не прочитан: " + snap.read_error));
    }
    document.getElementById("project").textContent =
      (snap.project || "пара") + " · writer " + ((snap.round && snap.round.writer) || "—");
    var stamp = clock(snap.generated_at);
    document.getElementById("stamp").textContent = stamp === "—" ? "время сборки неизвестно"
      : "собрано " + stamp + " UTC" + (snap.watch ? " · авто" : " · статично");

    [renderRound, renderRoles, renderPilot, renderTasks, renderEvents].forEach(function (draw) {
      app.appendChild(draw(snap));
    });
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
      button.textContent = paused ? "обновлять" : "пауза";
      button.setAttribute("aria-pressed", String(paused));
    }
    var existing = document.querySelector('meta[http-equiv="refresh"]');
    if (existing) existing.remove();
    /* Тег refresh снимается всегда и создаётся заново: включить обновление иначе
     * нечем, сервер его уже отдал. Пауза действует сразу, а не после перезагрузки. */
    if (paused) return;
    var seconds = parseInt(document.body.getAttribute("data-watch-seconds"), 10);
    /* Нулевой период не превращается в тег: `content="0"` — это немедленная
     * перезагрузка по кругу, и страница без автообновления превратилась бы в
     * мигающий кадр вместо фотографии. Разовый запуск — это снимок, и вести себя
     * он должен как снимок. */
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
    /* Пауза автообновления — отдельная кнопка, а не второй эффект у кнопки темы:
     * каждая меняет одно своё свойство, и по нажатию видно что именно. */
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