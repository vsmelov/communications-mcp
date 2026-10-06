"use strict";
// Дашборд дежурств: опрос /api/state раз в 4 с (пауза в скрытой вкладке), карточки обновляются на месте —
// открытые хвосты и фокус не сбрасываются. Весь текст из данных вставляется через textContent (без innerHTML).
(() => {
  const POLL_MS = 4000;
  const LS_TEXT = "duties.showText";
  const $ = (id) => document.getElementById(id);
  const cards = new Map();
  let lastState = null;
  let timer = null;
  let inflight = false;
  let lastAlertsKey = "";

  // ---------- утилиты
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const kid of kids.flat(3)) {
      if (kid === null || kid === undefined || kid === false || kid === "") continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* приватный режим */ } };
  const showText = () => $("showText").checked;

  async function api(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(url, opt);
    } catch (e) {
      return { ok: false, error: "дашборд не отвечает: " + e.message };
    }
    let data;
    try { data = await r.json(); } catch (e) { data = { ok: false, error: "HTTP " + r.status }; }
    if (!r.ok && data && data.ok === undefined) data.ok = false;
    return data;
  }

  let toastTimer = null;
  function toast(text, isError) {
    const t = $("toast");
    t.textContent = text;
    t.className = "toast" + (isError ? " error" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 9000 : 5000);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      const ta = h("textarea", { "aria-hidden": "true", class: "offscreen" });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  }

  async function busy(btn, fn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    try { return await fn(); } finally {
      btn.removeAttribute("aria-busy");
      btn.disabled = false;
    }
  }

  const LEVEL_TEXT = { red: "тревога", yellow: "внимание", green: "норма", grey: "архив" };
  const dot = (lvl) => h("span", { class: "dot " + (lvl || ""), "aria-hidden": "true" });
  const startedBy = (by, title) => (by === "dashboard" ? "запущен с дашборда"
    : by === "local" ? "запущен из командной строки" : `запущен из сессии «${title}»`);

  // ---------- строки статуса
  function listenerRow(d) {
    const li = d.listener || {};
    if (li.state === "alive" || li.state === "duplicate") {
      const dup = li.state === "duplicate";
      return [
        dot(dup ? "red" : (li.outdated ? "yellow" : "green")),
        dup ? `ДУБЛЬ: ${li.count} процесса (PID ${(li.pids || []).join(", ")})` : `работает · PID ${li.pid}`,
        li.uptime ? ` · ${li.uptime}` : "",
        li.outdated ? h("span", { class: "detail" }, `старый код: запущен до правки слушателя (${li.script_changed_at || "?"})`) : "",
        li.started_by_title ? h("span", { class: "detail" }, startedBy(li.started_by, li.started_by_title)) : "",
      ];
    }
    return [dot(d.active ? "red" : ""), "не запущен"];
  }

  function heartbeatRow(d) {
    const hb = d.heartbeat || {};
    if (!hb.age) {
      return [dot(d.active && (d.listener || {}).state !== "dead" ? "red" : ""), "нет",
        hb.note ? h("span", { class: "detail" }, hb.note) : "",
        hb.last_line_age ? h("span", { class: "detail" }, `последняя строка лога ${hb.last_line_age} назад`) : ""];
    }
    return [dot(hb.level), `${hb.age} назад (${hb.at})`, hb.has_alive ? "" : h("span", { class: "detail" }, "строк ALIVE нет — старая версия слушателя")];
  }

  function connectionRow(d) {
    const c = d.connection || {};
    if (c.state === "online") return [dot("green"), "есть", c.via ? ` · via ${c.via}` : "", c.for ? ` · ${c.for}` : ""];
    if (c.state === "offline") {
      return [dot(d.active ? "red" : ""), "нет", c.for ? ` · ${c.since_approx ? "не менее " : ""}${c.for}` : "",
        c.detail ? h("span", { class: "detail mono" }, c.detail) : ""];
    }
    if (c.state === "starting") return [dot("yellow"), "запускается", c.detail ? h("span", { class: "detail" }, c.detail) : ""];
    return [dot(""), "неизвестно"];
  }

  function sessionLink(title, link) {
    if (!link) return title ? `«${title}»` : "";
    return h("a", { href: link, title: "Открыть сессию в приложении Claude" }, title ? `«${title}»` : "сессия");
  }

  function agentRow(d) {
    const a = d.agent || {};
    const parts = [];
    const alive = (d.listener || {}).state !== "dead";
    if (a.state === "connected") {
      const m = (a.monitors || [])[0] || {};
      parts.push(dot("green"), "подключён — ", sessionLink(m.title || m.session_id || m.cli_session_id, m.link),
        m.since ? ` · Monitor с ${m.since}` : "");
      if (m.link) parts.push(" ", h("a", { class: "btn btn-quiet", href: m.link }, "Открыть сессию"));
    } else if (a.state === "rearming") {
      const s = a.last_seen || {};
      parts.push(dot("yellow"), "перевзводит Monitor — ", sessionLink(s.title || s.session_id, s.link), s.ago ? ` (был ${s.ago} назад)` : "");
    } else if (a.state === "idle") {
      const s = a.last_seen || a.attached || {};
      parts.push(dot(d.active ? "red" : ""), "сессия ", sessionLink(s.title || s.session_id, s.link), " открыта, но Monitor не запущен");
      if (s.link) parts.push(" ", h("a", { class: "btn btn-quiet", href: s.link }, "Открыть сессию"));
    } else {
      parts.push(dot(d.active && alive ? "red" : ""), d.active && alive ? "нет — никто не проснётся" : "нет");
      const s = a.attached || a.last_seen;
      if (s && s.link) parts.push(" ", h("a", { class: "btn btn-quiet", href: s.link }, "Открыть последнюю сессию"));
    }
    if (a.attached) {
      parts.push(h("span", { class: "detail" }, `назначен: «${a.attached.title || a.attached.session_id || a.attached.cli_session_id}»`,
        a.attached.alive ? " · сессия запущена" : " · сессия не запущена",
        a.attached.archived ? " · в архиве приложения" : ""));
    }
    if (a.marker) {
      const mk = a.marker;
      parts.push(h("span", { class: "detail" }, mk.behind_lines
        ? `отстаёт на ${mk.behind_lines} строк лога${mk.behind_batches ? `, из них пачек: ${mk.behind_batches}` : ""}`
        : `дочитал лог (маркер ${mk.updated_at || "—"})`));
    }
    if (a.orphan_pipes) parts.push(h("span", { class: "detail" }, `сирот tail/grep от прошлых Monitor: ${a.orphan_pipes}`));
    return parts;
  }

  function trafficRow(d) {
    const t = d.traffic || {};
    const seen = (d.chats || []).filter((c) => c.last_seen_id)
      .map((c) => ((d.chats || []).length > 1 ? `«${c.title}» ` : "") + `last_seen_id ${c.last_seen_id}`);
    return [`сегодня ${t.today ?? 0} · всего ${t.total ?? 0}`, t.last_at ? ` · последнее ${t.last_at}` : "",
      t.owner_calls ? h("span", { class: "detail" }, `из них вызовов владельца /ai: ${t.owner_calls}`) : "",
      seen.length ? h("span", { class: "detail" }, "state: " + seen.join(", ")) : "",
      t.last_reply ? h("span", { class: "detail" }, `последний ответ через send_reply: ${t.last_reply.at}`) : ""];
  }

  function pendingRow(d) {
    const p = d.pending || {};
    if (!p.known) return [dot(""), "нет данных об ответах", h("span", { class: "detail" }, "появятся после первого ответа через send_reply.py")];
    if (!p.count) return [dot("green"), "0", p.last_reply_at ? ` · последний ответ ${p.last_reply_at}` : "", p.approx ? h("span", { class: "detail" }, "≈ по времени черновиков notes/reply*.txt") : ""];
    const lvl = p.oldest_min !== null && p.oldest_min > (lastState?.settings?.unanswered_alert_min ?? 30) ? "red" : "yellow";
    return [dot(lvl), `${p.count}`, p.oldest_min !== null ? ` · самое старое ${p.oldest_min} мин (с ${p.oldest_at})` : "",
      p.approx ? h("span", { class: "detail" }, "≈ по времени черновиков notes/reply*.txt") : ""];
  }

  function archiveRow(d) {
    const out = [];
    for (const c of d.chats || []) {
      if (c.archive) out.push(h("span", { class: "detail" }, `«${c.title}»: дамп обновлён ${c.archive.age} назад`));
      else out.push(h("span", { class: "detail" }, `«${c.title}»: нет в tg-archive`));
    }
    return out;
  }

  function statusList(d) {
    const rows = [
      ["Слушатель", listenerRow(d)], ["Пульс", heartbeatRow(d)], ["Связь", connectionRow(d)],
      ["Агент", agentRow(d)], ["Входящие", trafficRow(d)], ["Ждёт ответа", pendingRow(d)], ["Архив", archiveRow(d)],
    ];
    const dl = h("dl", { class: "kv" });
    for (const [k, v] of rows) dl.append(h("dt", null, k), h("dd", null, v));
    return dl;
  }

  // ---------- карточка
  function makeCard(d) {
    const c = { name: d.name };
    c.title = h("h3");
    c.badge = h("span", { class: "pill" });
    c.sub = h("div", { class: "card-sub" });
    c.note = h("p", { class: "card-sub", hidden: true });
    c.chips = h("div", { class: "chips" });
    c.status = h("div");
    c.alerts = h("ul", { class: "card-alerts" });
    c.btnStart = h("button", { type: "button", class: "btn", onclick: () => action(c, "start") }, "Старт");
    c.btnStop = h("button", { type: "button", class: "btn btn-danger", onclick: () => action(c, "stop") }, "Стоп");
    c.btnRestart = h("button", { type: "button", class: "btn", onclick: () => action(c, "restart") }, "Перезапуск");
    c.btnCopy = h("button", { type: "button", class: "btn", onclick: () => copyMonitor(c) }, "Скопировать команду Monitor");
    c.btnReadme = h("button", { type: "button", class: "btn btn-quiet", onclick: () => openReadme(c) }, "Открыть README");
    c.btnActive = h("button", { type: "button", class: "btn btn-quiet", onclick: () => toggleActive(c) }, "В архив");
    c.pre = h("pre", { tabindex: "0", "aria-label": "Хвост stdout слушателя" }, "…");
    c.tail = h("details", { class: "tail" },
      h("summary", null, "Хвост stdout (последние 60 строк)"),
      h("div", { class: "actions tail-actions" },
        h("button", { type: "button", class: "btn btn-quiet", onclick: () => loadTail(c) }, "Обновить хвост")),
      c.pre);
    c.tail.addEventListener("toggle", () => { if (c.tail.open) loadTail(c); });
    c.root = h("article", { class: "card", id: "duty-" + d.name, "aria-labelledby": "t-" + d.name },
      h("div", { class: "card-head" }, c.title, c.badge),
      c.sub, c.note, c.chips, c.status, c.alerts,
      h("div", { class: "actions" }, c.btnStart, c.btnStop, c.btnRestart, c.btnCopy, c.btnReadme, c.btnActive),
      c.tail);
    c.title.id = "t-" + d.name;
    cards.set(d.name, c);
    return c;
  }

  function updateCard(c, d) {
    c.data = d;
    c.root.dataset.level = d.level || "grey";
    c.title.textContent = d.title || d.name;
    c.badge.textContent = LEVEL_TEXT[d.level] || d.level;
    c.badge.className = "pill " + (d.level || "");
    c.sub.replaceChildren(h("span", { class: "mono" }, d.name), " · ", d.workspace || "воркспейс не задан");
    const note = [d.notes, d.status_note].filter(Boolean).join(" · ");
    c.note.textContent = note;
    c.note.hidden = !note;
    c.chips.replaceChildren(...(d.chats || []).map((ch) => {
      const id = String(ch.id ?? ch.spec ?? "?");
      return h("span", { class: "chip" }, ch.title && ch.title !== id ? `${ch.title} · ${id}` : id);
    }));
    c.status.replaceChildren(statusList(d));
    c.alerts.replaceChildren(...(d.alerts || []).map((a) => h("li", { class: a.level }, a.text)));
    const st = (d.listener || {}).state;
    c.btnStart.disabled = st === "alive" || st === "duplicate";
    c.btnStop.disabled = !(st === "alive" || st === "duplicate");
    c.btnReadme.disabled = !d.readme;
    c.btnActive.textContent = d.active ? "В архив" : "Вернуть из архива";
    if (c.tail.open && !c.tailLoading) loadTail(c, true);
  }

  async function toggleActive(c) {
    const d = c.data || {};
    const name = d.title || c.name;
    if (d.active && !confirm(`Перенести «${name}» в архив? Тревоги по нему считаться не будут; слушатель не останавливается.`)) return;
    await busy(c.btnActive, async () => {
      const r = await api("POST", `/api/duties/${encodeURIComponent(c.name)}/${d.active ? "deactivate" : "activate"}`, {});
      toast(r && r.ok ? (r.active ? `«${name}» снова активно` : `«${name}» в архиве`) : (r?.error || "не получилось"), !(r && r.ok));
      await refresh(true);
    });
  }

  async function action(c, what) {
    const d = c.data || {};
    const name = d.title || c.name;
    if (what === "stop" && !confirm(`Остановить слушатель «${name}»? Пока он остановлен, сообщения не собираются (при старте догонятся по state).`)) return;
    if (what === "restart" && !confirm(`Перезапустить слушатель «${name}»?`)) return;
    const btn = { start: c.btnStart, stop: c.btnStop, restart: c.btnRestart }[what];
    await busy(btn, async () => {
      const r = await api("POST", `/api/duties/${encodeURIComponent(c.name)}/${what}`, {});
      if (r && r.ok) {
        const msg = { start: `запущен (PID ${r.pid ?? "?"})`, stop: "остановлен", restart: `перезапущен (PID ${r.start?.pid ?? "?"})` }[what];
        toast(`«${name}»: ${msg}`);
      } else {
        toast(`«${name}»: ${r?.error || r?.message || "не получилось"}`, true);
      }
      await refresh(true);
    });
  }

  async function copyMonitor(c) {
    await busy(c.btnCopy, async () => {
      const r = await api("GET", `/api/duties/${encodeURIComponent(c.name)}/monitor-command`);
      if (!r || !r.command) return toast(r?.error || "команду получить не удалось", true);
      const ok = await copyText(r.command);
      toast(ok ? "Команда Monitor скопирована: вставь в тул Monitor (timeout_ms 1800000), затем duty_attach." : "Скопировать не вышло — команда в консоли браузера", !ok);
      if (!ok) console.log(r.command);
    });
  }

  async function openReadme(c) {
    await busy(c.btnReadme, async () => {
      const r = await api("POST", `/api/duties/${encodeURIComponent(c.name)}/open-readme`, {});
      toast(r && r.ok ? "README открыт" : (r?.error || "не открылся"), !(r && r.ok));
    });
  }

  async function loadTail(c, quiet) {
    c.tailLoading = true;
    try {
      const r = await api("GET", `/api/duties/${encodeURIComponent(c.name)}/tail?lines=60&text=${showText() ? 1 : 0}`);
      if (r && r.lines) {
        const atBottom = c.pre.scrollTop + c.pre.clientHeight >= c.pre.scrollHeight - 8;
        c.pre.textContent = r.lines.length ? r.lines.join("\n") : "(пусто)";
        if (atBottom || !quiet) c.pre.scrollTop = c.pre.scrollHeight;
      } else if (!quiet) {
        c.pre.textContent = r?.error || "не удалось прочитать";
      }
    } finally {
      c.tailLoading = false;
    }
  }

  // ---------- сводка, тревоги, найденные
  function tile(num, label, lvl) {
    return h("div", { class: "tile " + (lvl || "") }, h("div", { class: "num" }, String(num)), h("div", { class: "lbl" }, label));
  }

  function renderSummary(s) {
    const sm = s.summary || {};
    const act = sm.active || 0;
    $("summary").replaceChildren(
      tile(act, `активных дежурств (всего ${sm.total || 0})`),
      tile(`${sm.listeners_alive || 0}/${act}`, "слушатели работают", (sm.listeners_alive || 0) < act ? "red" : "green"),
      tile(`${sm.agents_connected || 0}/${act}`, "агенты на Monitor", (sm.agents_connected || 0) < act ? "red" : "green"),
      tile(sm.pending || 0, "ждут ответа", sm.pending ? "yellow" : ""),
      tile(sm.red || 0, "красных тревог", sm.red ? "red" : "green"),
      tile(sm.yellow || 0, "жёлтых", sm.yellow ? "yellow" : ""));
  }

  function renderAlerts(s) {
    const items = [];
    for (const a of s.alerts || []) items.push({ ...a, who: "Общее", name: null });
    for (const d of Object.values(s.duties || {})) {
      if (!d.active) continue;
      for (const a of d.alerts || []) items.push({ ...a, who: d.title || d.name, name: d.name });
    }
    items.sort((x, y) => (x.level === y.level ? 0 : x.level === "red" ? -1 : 1));
    const key = JSON.stringify(items.map((i) => [i.level, i.code, i.name, i.text]));
    if (key === lastAlertsKey) return;  // не дёргать aria-live без изменений
    lastAlertsKey = key;
    $("alerts").replaceChildren(...items.map((a) => h("li", { class: "alert " + a.level },
      h("span", { class: "lvl" }, a.level === "red" ? "тревога" : "внимание"),
      a.name ? h("a", { class: "who", href: "#duty-" + a.name }, a.who) : h("span", { class: "who" }, a.who),
      h("span", { class: "txt" }, a.text))));
  }

  function renderFound(s) {
    const list = s.unregistered || [];
    $("foundSection").hidden = !list.length;
    $("found").replaceChildren(...list.map((f) => h("li", null,
      h("div", { class: "info" },
        h("strong", null, `PID ${f.pid}`), ` · чаты ${(f.chats || []).join(", ")}`,
        h("span", { class: "detail mono" }, f.state || ""),
        f.started_by_title ? h("span", { class: "detail" }, startedBy(f.started_by, f.started_by_title)) : ""),
      h("button", { type: "button", class: "btn", onclick: () => adoptIntoForm(f) }, "Завести"))));
  }

  function renderHygiene(s) {
    const o = s.orphans || {};
    const parts = [];
    parts.push(`Сирот tail/grep от прошлых Monitor: ${o.pipes ?? 0}. Петель watch_health.sh: ${o.health_loops ?? 0}` +
      (o.health_orphans ? ` (без живого родителя: ${o.health_orphans})` : "") + ".");
    parts.push(" На Windows эти процессы переживают истечение Monitor и копятся при каждом перевзводе; безвредны, но " +
      "лишние. Команда Monitor из дашборда завершает свой tail сама (tail --pid). Чистка старых — решение владельца.");
    parts.push(` Снимок процессов: ${s.snapshot?.took_ms ?? "?"} мс (${s.snapshot?.method || "?"}).`);
    $("hygiene").textContent = parts.join("");
  }

  function renderArchiver(s) {
    const a = s.archiver || {};
    const el = $("archiveState");
    if (a.ok === true) { el.textContent = "отвечает"; el.className = "pill green"; }
    else if (a.ok === false) { el.textContent = "не отвечает (Docker?)"; el.className = "pill red"; }
    else { el.textContent = "проверяю…"; el.className = "pill"; }
    if (a.url) $("archiveLink").href = a.url;
    const ar = s.archive || {};
    $("archiveAge").textContent = ar.age ? `дампы обновлялись ${ar.age} назад` : "";
  }

  function render(s) {
    lastState = s;
    $("updated").textContent = `обновлено ${new Date().toLocaleTimeString("ru-RU")} · порт ${s.port ?? ""}`;
    renderSummary(s);
    renderAlerts(s);
    renderArchiver(s);
    renderFound(s);
    renderHygiene(s);
    const duties = Object.values(s.duties || {}).sort((a, b) => a.name.localeCompare(b.name));
    const activeBox = $("duties");
    const archBox = $("archivedList");
    const seen = new Set();
    let nActive = 0;
    let nArch = 0;
    for (const d of duties) {
      seen.add(d.name);
      const c = cards.get(d.name) || makeCard(d);
      updateCard(c, d);
      const box = d.active ? activeBox : archBox;
      if (c.root.parentElement !== box) box.append(c.root);
      if (d.active) nActive++; else nArch++;
    }
    for (const [name, c] of cards) {
      if (!seen.has(name)) { c.root.remove(); cards.delete(name); }
    }
    $("noDuties").hidden = nActive > 0;
    $("archivedCount").textContent = nArch ? `(${nArch})` : "(нет)";
  }

  async function refresh(force) {
    if (inflight && !force) return;
    inflight = true;
    try {
      const s = await api("GET", "/api/state");
      if (s && s.duties) render(s);
      else $("updated").textContent = "нет данных: " + (s?.error || "дашборд не отвечает");
    } finally {
      inflight = false;
    }
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!document.hidden) await refresh();
      schedule();
    }, POLL_MS);
  }

  // ---------- форма «Новое дежурство»
  function adoptIntoForm(f) {
    const form = $("addForm");
    form.reset();
    form.elements.name.value = (f.suggested_name || "").toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 40);
    form.elements.chat.value = (f.chats || []).join(", ");
    form.elements.workspace.value = f.workspace || "";
    form.elements.adopt.value = "1";
    form.elements.state.value = f.state || "";
    form.elements.log.value = f.log || "";
    form.elements.stdout.value = f.stdout || "";
    form.elements.quiet.value = f.quiet ?? 0;
    form.elements.trigger.value = f.trigger ?? "/ai";
    $("startWrap").hidden = true;
    const note = $("adoptNote");
    note.textContent = `Подхватываю работающий слушатель PID ${f.pid} — перезапуска не будет. stdout: ${f.stdout || "не найден — укажите вручную после заведения"}`;
    note.hidden = false;
    $("addSection").scrollIntoView({ behavior: "smooth", block: "start" });
    form.elements.name.focus();
  }

  function clearAdopt() {
    const form = $("addForm");
    for (const k of ["adopt", "state", "log", "stdout"]) form.elements[k].value = "";
    $("adoptNote").hidden = true;
    $("startWrap").hidden = false;
  }

  async function submitAdd(ev) {
    ev.preventDefault();
    const form = $("addForm");
    if (!form.reportValidity()) return;
    const el = form.elements;
    const body = {
      name: el.name.value.trim(), chat: el.chat.value.trim(), title: el.title.value.trim(),
      workspace: el.workspace.value.trim(), trigger: el.trigger.value, quiet: parseInt(el.quiet.value || "0", 10) || 0,
      sign: el.sign.checked, start: el.start.checked && !el.adopt.value, notes: el.notes.value,
      adopt: !!el.adopt.value, state: el.state.value, log: el.log.value, stdout: el.stdout.value,
    };
    const btn = form.querySelector('button[type="submit"]');
    await busy(btn, async () => {
      const r = await api("POST", "/api/duties", body);
      const box = $("addResult");
      if (!r || !r.ok) {
        toast(r?.error || "не получилось", true);
        return;
      }
      const cmd = r.monitor_command?.command || "";
      box.replaceChildren(
        h("p", null, h("strong", null, `Дежурство «${r.title || r.duty}» заведено.`),
          r.created?.readme ? " README создан из шаблона — впишите правила ответа." : ""),
        h("p", { class: "small" }, `Воркспейс: ${r.workspace}`),
        r.start ? h("p", { class: "small" }, r.start.ok ? `Слушатель запущен (PID ${r.start.pid}).` : `Слушатель: ${r.start.error || "не запустился"}`) : "",
        h("p", { class: "small" }, r.reminder || ""),
        cmd ? h("pre", null, cmd) : "",
        cmd ? h("button", { type: "button", class: "btn", onclick: async () => toast(await copyText(cmd) ? "Скопировано" : "Не скопировалось", false) }, "Скопировать команду Monitor") : "");
      box.hidden = false;
      form.reset();
      clearAdopt();
      toast(`Дежурство «${r.duty}» заведено`);
      await refresh(true);
    });
  }

  // ---------- старт
  function init() {
    $("showText").checked = lsGet(LS_TEXT, "0") === "1";
    $("showText").addEventListener("change", () => {
      lsSet(LS_TEXT, showText() ? "1" : "0");
      for (const c of cards.values()) if (c.tail.open) loadTail(c);
    });
    $("refreshBtn").addEventListener("click", () => refresh(true));
    $("addForm").addEventListener("submit", submitAdd);
    $("resetBtn").addEventListener("click", () => setTimeout(clearAdopt, 0));
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
    refresh(true).then(schedule);
  }
  init();
})();
