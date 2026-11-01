/* RVC 控制台 · 页面逻辑（原生 JS，无构建依赖） */
(function () {
  "use strict";

  let bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;

  const TAB_META = {
    overview: { title: "概览", desc: "插件与 Gateway 状态、最近任务一览" },
    tasks: { title: "任务", desc: "RVC 翻唱与 TTS 语音的当前/历史任务，可试听回放" },
    rvc: { title: "RVC 控制台", desc: "用现有音色做转换，或只分离人声 / 伴奏" },
    tts: { title: "TTS 控制台", desc: "输入文字试听音色：原始 TTS 与 RVC 转换后对比" },
    models: { title: "模型管理", desc: "音色列表、别名、性别与默认音色" },
    audio: { title: "音频效果", desc: "歌曲翻唱与 TTS 各自独立的 Reverb 混响参数" },
    settings: { title: "插件设置", desc: "按分组修改插件参数，保存后部分项需重载插件" },
    diagnostics: { title: "诊断", desc: "连通性、配置检查与最近的错误日志" },
  };

  const state = {
    tab: "overview",
    taskPage: 1,
    taskPageSize: 30,
    rvcMode: "convert",
    overview: null,
    models: null,
    fields: [],
    values: {},
    dirty: {},
    needsReload: [],
    jobId: null,
    timer: null,
    urls: [],
    uploadId: null,
    sepUploadId: null,
    uploading: false,
    sepRunning: false,
    taskItems: [],
    taskStatus: "all",
    taskMode: "current",
    taskBiz: "rvc",
    ttsItems: [],
    ttsSnap: null,
    gatewayModelStems: [],
    gatewayModelFiles: [],
    activeTimer: null,
    elapsedTimer: null,
    activeElapsed: 0,
    activeAt: 0,
    audioEls: [],
    clipUploadId: null,
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "-";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function fmtDuration(sec) {
    const n = Number(sec);
    if (!Number.isFinite(n) || n <= 0) return "-";
    return n >= 60 ? `${Math.floor(n / 60)}m${Math.round(n % 60)}s` : `${n.toFixed(1)}s`;
  }

  function toast(text, ms) {
    const el = $("#toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, ms || 2800);
  }

  function banner(text) {
    const el = $("#banner");
    if (!text) { el.hidden = true; el.textContent = ""; return; }
    el.textContent = text;
    el.hidden = false;
  }

  /* ---------------- bridge / API ---------------- */
  async function ensureBridge() {
    if (bridge) {
      try { await bridge.ready(); } catch (e) { /* ignore */ }
      return bridge;
    }
    for (let i = 0; i < 50; i += 1) {
      bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
      if (bridge) {
        try { await bridge.ready(); } catch (e) { /* ignore */ }
        return bridge;
      }
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new Error("未检测到 AstrBot 页面桥接（bridge-sdk.js 未加载）。请重载插件后从左侧「RVC 控制台」进入本页。");
  }

  function unwrap(res) {
    if (res && typeof res === "object") {
      if (res.status === "error") throw new Error(res.message || "请求失败");
      if (res.status === "ok" && Object.prototype.hasOwnProperty.call(res, "data")) return res.data;
    }
    return res || {};
  }

  async function api(path, options) {
    const opt = options || {};
    const b = await ensureBridge();
    const endpoint = "page/" + String(path).replace(/^\/+/, "");
    const raw = (opt.method || "GET").toUpperCase() === "GET"
      ? await b.apiGet(endpoint, opt.params || {})
      : await b.apiPost(endpoint, opt.body || {});
    return unwrap(raw);
  }

  /* ---------------- 状态 ---------------- */
  function setStatus(id, stateName, label) {
    const el = $(id);
    if (!el) return;
    el.dataset.state = stateName;
    el.innerHTML = `<i></i>${label}`;
  }

  function statusTag(status) {
    const map = {
      succeeded: ["ok", "成功"], failed: ["err", "失败"],
      running: ["run", "进行中"], pending: ["run", "排队"], unknown: ["", "未知"],
    };
    const [cls, text] = map[status] || ["", status || "未知"];
    return `<span class="tag ${cls}">${esc(text)}</span>`;
  }

  function genderLabel(g) {
    return { male: "男声", female: "女声", unknown: "未知" }[g] || "未知";
  }

  /* ---------------- 导航 ---------------- */
  function switchTab(name) {
    state.tab = name;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    const meta = TAB_META[name] || {};
    const titleEl = $("#pageTitle");
    const descEl = $("#pageDesc");
    if (titleEl) titleEl.textContent = meta.title || "RVC 控制台";
    if (descEl) descEl.textContent = meta.desc || "";
    document.title = (meta.title ? meta.title + " · " : "") + "RVC 控制台";
    const scrollEl = $("#content");
    if (scrollEl) scrollEl.scrollTop = 0;
    const actions = $(".top-actions");
    if (actions) actions.style.display = name === "overview" ? "" : "none";
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    if (name === "overview") loadOverview();
    if (name === "tasks") {
      switchTaskBiz(state.taskBiz);
    } else {
      stopActivePolling();
    }
    if (name === "models") loadModels();
    if (name === "rvc") prepareRvc();
    if (name === "tts") prepareTts();
    if (name === "audio") loadAudioTab();
    if (name === "settings") loadConfig();
    if (name === "diagnostics") loadDiagnostics();
  }

  function switchRvcMode(mode) {
    state.rvcMode = mode;
    $$("#rvcSeg .seg-item").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("#paneConvert").hidden = mode !== "convert";
    $("#paneSeparate").hidden = mode !== "separate";
    $("#paneClip").hidden = mode !== "clip";
    if (mode === "separate") prepareSeparate();
    if (mode === "convert") prepareRvc();
  }

  async function runClip() {
    const btn = $("#clipRun");
    btn.disabled = true;
    $("#clipStatus").textContent = "准备中...";
    try {
      const uploadId = await uploadAudio("#clipFile", "#clipUploadNote", "clipUploadId");
      const body = {
        upload_id: uploadId,
        seconds: Number($("#clipSeconds").value) || 25,
        output_format: $("#clipFormat").value,
      };
      const d = await api("rvc/clip", { method: "POST", body });
      $("#clipOutputs").innerHTML = "";
      $("#clipStatus").textContent = "已提交，正在切段";
      pollJob(d.job_id, "#clipStatus", (job) => {
        btn.disabled = false;
        if (job.status === "succeeded") {
          const range = job.clip_range || [];
          $("#clipInfo").textContent = range.length === 2
            ? `已切出 ${Number(range[1]).toFixed(2)} 秒（起点 ${Number(range[0]).toFixed(2)} 秒）`
            : "切段完成";
          showOutputs(job.id, "#clipOutputs", null);
        }
      });
    } catch (err) {
      $("#clipStatus").textContent = "失败：" + err.message;
      btn.disabled = false;
    }
  }

  /* ---------------- 概览 ---------------- */
  async function loadOverview() {
    try {
      const d = await api("overview");
      state.overview = d;
      banner("");
      const gw = d.gateway || {};
      setStatus("#stPlugin", "ok", "插件 <b>已加载</b>");
      setStatus("#stGateway", gw.ok ? "ok" : "err", `Gateway <b>${gw.ok ? "正常" : "异常"}</b>`);
      $("#ovPlugin").textContent = d.plugin && d.plugin.loaded ? "运行中" : "未知";
      $("#ovPluginHint").textContent = `版本 ${(d.plugin && d.plugin.version) || "未知"}`;
      $("#ovGateway").textContent = gw.ok ? "正常" : "异常";
      $("#ovGatewayHint").textContent = gw.ok
        ? `${gw.latency_ms}ms · ${gw.base}`
        : (gw.error || "不可达");
      $("#ovModels").textContent = `${(d.models && d.models.gateway_count) ?? "-"} 个`;
      $("#ovModelsHint").textContent = d.models && d.models.gateway_ok
        ? `已配置 ${d.models.configured_count} 个 · 今日 ${(d.today && d.today.count) ?? 0} 次`
        : ((d.models && d.models.error) || "Gateway 不可用");
      $("#ovDefault").textContent = (d.default_model && d.default_model.value) || "未设置";
      $("#ovDefaultHint").textContent = [
        d.default_model && d.default_model.alias ? `别名 ${d.default_model.alias}` : "",
        `性别 ${genderLabel(d.default_model && d.default_model.gender)}`,
      ].filter(Boolean).join(" · ");
      renderOverviewTasks(d);
      const rows = [
        ["Gateway 目录", (d.models && d.models.dir) || "-"],
        ["任务目录", d.paths && d.paths.tasks_available ? "可读" : "不可读"],
        ["成品目录", d.paths && d.paths.outputs_available ? "可读" : "不可读"],
        ["服务器时间", d.server_time || "-"],
      ];
      $("#ovMeta").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    } catch (err) {
      setStatus("#stGateway", "err", "Gateway <b>无法读取</b>");
      banner(err.message);
      $("#ovTasks").innerHTML = `<p class="empty">读取失败：${esc(err.message)}</p>`;
    }
  }

  function renderOverviewTasks(d) {
    const rows = (d.recent_tasks || []).slice(0, 6);
    if (!rows.length) { $("#ovTasks").innerHTML = `<p class="empty">暂无任务记录</p>`; return; }
    $("#ovTasks").innerHTML = rows.map((t) => `
      <div class="row">
        <div class="r-main">
          <div class="r-title">${esc(t.song || t.id)}</div>
          <div class="r-sub">${esc([t.artist, t.model].filter(Boolean).join(" · ") || "-")} · ${fmtTime(t.created_at)} · ${fmtDuration(t.duration)}</div>
        </div>
        ${statusTag(t.status)}
      </div>`).join("");
  }

  /* ---------------- 任务 ---------------- */
  function switchTaskBiz(biz) {
    state.taskBiz = biz === "tts" ? "tts" : "rvc";
    $$("#taskBizSeg .seg-item").forEach((b) => b.classList.toggle("active", b.dataset.biz === state.taskBiz));
    const thead = $("#taskTable thead");
    if (thead) {
      thead.innerHTML = state.taskBiz === "tts"
        ? "<tr><th>时间</th><th>用户</th><th>说的内容</th><th>音色</th><th>时长</th><th>状态</th><th></th></tr>"
        : "<tr><th>时间</th><th>歌曲 / 歌手</th><th>用户</th><th>文件 / 输入</th><th>音色</th><th>时长</th><th>状态</th><th></th></tr>";
    }
    switchTaskMode(state.taskMode);
  }

  async function loadTasks() {
    if (state.taskBiz === "tts") {
      try {
        const d = await api("tts/tasks", { params: { limit: 200 } });
        state.ttsItems = d.items || [];
        state.ttsSnap = d;
        $("#tasksHint").textContent = `共 ${d.total || 0} 条 · 队列上限 ${d.queue_limit ?? 3}`;
        state.taskPage = 1;
        renderTasks();
      } catch (err) {
        $("#tasksHint").textContent = "读取失败：" + err.message;
      }
      return;
    }
    try {
      const d = await api("tasks", { params: { limit: 200 } });
      state.taskItems = d.items || [];
      $("#tasksHint").textContent = `共 ${d.total || 0} 条`;
      state.taskPage = 1;
      renderTasks();
    } catch (err) {
      $("#tasksHint").textContent = "读取失败：" + err.message;
    }
  }

  function filteredTasks() {
    const kw = ($("#taskSearch").value || "").trim().toLowerCase();
    return state.taskItems.filter((t) => {
      if (state.taskStatus !== "all" && t.status !== state.taskStatus) return false;
      if (!kw) return true;
      return [t.song, t.artist, t.id, t.model, t.input_name, t.output_name]
        .some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function renderTasks() {
    if (state.taskBiz === "tts") return renderTtsHistory();
    const rows = filteredTasks();
    const size = state.taskPageSize;
    const pages = Math.max(1, Math.ceil(rows.length / size));
    if (state.taskPage > pages) state.taskPage = pages;
    const start = (state.taskPage - 1) * size;
    const pageRows = rows.slice(start, start + size);

    $("#taskTable tbody").innerHTML = pageRows.map((t) => `
      <tr>
        <td>${fmtTime(t.created_at)}</td>
        <td><div class="t-title">${esc(t.song || t.input_name || "-")}</div>
          <div class="t-sub">${esc(t.artist || "歌手未知")}</div></td>
        <td><div class="t-title">${esc(t.user_name || t.user || "-")}</div>
          <div class="t-sub">${esc(t.user || "")}</div></td>
        <td><div class="t-file">${esc(t.output_name || t.input_name || t.id)}</div></td>
        <td>${esc(t.model || "-")}</td>
        <td>${fmtDuration(t.duration)}</td>
        <td>${statusTag(t.status)}</td>
        <td class="actions"><button class="btn ghost sm" data-task="${esc(t.id)}" type="button">详情</button></td>
      </tr>`).join("");
    $("#taskEmpty").style.display = rows.length ? "none" : "block";

    const range = $("#taskRange");
    if (range) {
      range.textContent = rows.length
        ? `第 ${start + 1}–${start + pageRows.length} 条，共 ${rows.length} 条`
        : "共 0 条";
    }
    renderTaskPager(pages);
  }

  function renderTtsHistory() {
    const kw = ($("#taskSearch").value || "").trim().toLowerCase();
    const rows = state.ttsItems.filter((t) => !kw || [t.user, t.request, t.speak, t.id, t.model]
      .some((v) => String(v || "").toLowerCase().includes(kw)));
    const size = state.taskPageSize;
    const pages = Math.max(1, Math.ceil(rows.length / size));
    if (state.taskPage > pages) state.taskPage = pages;
    const start = (state.taskPage - 1) * size;
    const pageRows = rows.slice(start, start + size);

    $("#taskTable tbody").innerHTML = pageRows.map((t) => `
      <tr>
        <td>${fmtTime(t.created_at)}</td>
        <td><div class="t-title">${esc(t.user_name || t.user || "-")}</div>
          <div class="t-sub">${esc(t.user || "")}</div></td>
        <td colspan="2"><div class="t-clip" title="${esc(t.speak || t.request)}">${esc(t.speak || t.request || "-")}</div></td>
        <td>${esc(t.model || "-")}</td>
        <td>${fmtDuration(t.duration)}</td>
        <td>${statusTag("succeeded")}</td>
        <td class="actions">${t.playable ? `<button class="btn ghost sm" data-tts-play="${esc(t.id)}" type="button">播放</button>` : ""}</td>
      </tr>`).join("");
    $("#taskEmpty").style.display = rows.length ? "none" : "block";

    const range = $("#taskRange");
    if (range) {
      range.textContent = rows.length
        ? `第 ${start + 1}–${start + pageRows.length} 条，共 ${rows.length} 条`
        : "共 0 条";
    }
    renderTaskPager(pages);
  }

  function renderTtsCurrent() {
    const host = $("#paneCurrent");
    if (!host) return;
    const snap = state.ttsSnap || {};
    const active = snap.active;
    const waiting = snap.waiting || [];
    const limit = snap.queue_limit == null ? 3 : snap.queue_limit;

    let nowCard;
    if (active) {
      const stageStep = { "提取台词": 1, "TTS 生成与变声": 2, "发送语音": 4 };
      const cur = Number(stageStep[active.stage] || 1);
      const steps = [["提取台词", 1], ["TTS 生成", 2], ["RVC 变声", 3], ["发送语音", 4]]
        .map(([name, idx]) => [name, idx < cur ? "done" : idx === cur ? "cur" : "todo"]);
      const stepsHtml = steps.map(([name, st]) =>
        `<li class="${st}"><span class="s-ico">${st === "done" ? "✓" : st === "cur" ? "●" : "○"}</span>${esc(name)}</li>`).join("");
      state.activeElapsed = Number(active.elapsed || 0);
      state.activeAt = Date.now();
      nowCard = `
        <article class="card">
          <div class="card-head"><h3>正在处理</h3><span class="tag run">${esc(active.stage || "处理中")}</span></div>
          <div class="now-row">
            <div>
              <div class="t-title">${esc(active.label || active.text || "-")}</div>
              <div class="t-sub">${esc(active.user_name || active.user || "")} · ${esc(active.model || "TTS 语音")}</div>
            </div>
            <div class="hint" id="nowElapsed">已运行 ${Math.round(active.elapsed || 0)} 秒</div>
          </div>
          <ul class="steps">${stepsHtml}</ul>
          <p class="hint">台词经二次 LLM 提取后送 RVC 变声；反应文字会先于语音发出。</p>
        </article>`;
      startElapsedTicker();
    } else {
      stopElapsedTicker();
      nowCard = `<article class="card">
        <div class="card-head"><h3>正在处理</h3><span class="tag">空闲</span></div>
        <p class="hint">当前没有正在进行的 TTS 任务。有人排队时这里会显示进度。</p></article>`;
    }
    const regWaitTts = (snap.registry_waiting || []).filter((r) => r.type === "tts");

    const queueRows = waiting.map((w) => `
      <li><span class="q-pos">#${w.position}</span>
        <span class="t-title">${esc(w.label || w.text || "-")}</span>
        <span class="t-sub">${esc(w.user_name || w.user || "")}</span>
        <span class="hint" style="margin-left:auto">已等 ${Math.round(w.waited || 0)} 秒</span></li>`).join("")
      + regWaitTts.map((r, i) => `
      <li><span class="q-pos">#${waiting.length + i + 1}</span>
        <span class="t-title">${esc(r.label || "-")}</span>
        <span class="t-sub">${esc(r.user || "")} · 准备中</span>
        <span class="hint" style="margin-left:auto">已等 ${Math.round(r.waited || 0)} 秒</span></li>`).join("");

    host.innerHTML = `
      <div class="now-grid">
        ${nowCard}
        <article class="card">
          <div class="card-head"><h3>排队中（${waiting.length + regWaitTts.length}/${limit}）</h3></div>
          ${queueRows ? `<ul class="queue">${queueRows}</ul>` : `<p class="hint">队列为空</p>`}
          <p class="hint">同时只处理一个 TTS 转换，其余按顺序等待；超过 ${limit} 条会跳过新请求。</p>
        </article>
      </div>`;
  }

  async function ttsPlayModal(id) {
    try {
      const d = await api("tts/audio", { params: { id } });
      $("#modalTitle").textContent = "TTS 回放";
      $("#modalContent").innerHTML = `
        <p class="f-hint">${esc(d.speak || "-")} · 触发用户 ${esc(d.user || "-")} · ${fmtDuration(d.duration)}${d.group_name || d.group_id ? ` · 群聊 ${esc(d.group_name || "")}（${esc(d.group_id)}）` : " · 私聊 / 控制台"}</p>
        <div id="ttsPlayerHost"></div>`;
      $("#modal").hidden = false;
      await mountPlayer($("#ttsPlayerHost"), {
        base64: d.audio_base64, mime: d.mime, name: d.name, duration: d.duration,
      });
    } catch (err) {
      toast("回放失败：" + err.message, 4200);
    }
  }

  function renderTaskPager(pages) {
    const box = $("#taskPages");
    if (!box) return;
    const cur = state.taskPage;
    const items = [];
    const add = (p) => items.push(
      `<button data-page="${p}" class="${p === cur ? "active" : ""}" type="button">${p}</button>`);
    if (pages <= 7) {
      for (let i = 1; i <= pages; i += 1) add(i);
    } else {
      add(1);
      if (cur > 3) items.push('<span class="gap">…</span>');
      for (let i = Math.max(2, cur - 1); i <= Math.min(pages - 1, cur + 1); i += 1) add(i);
      if (cur < pages - 2) items.push('<span class="gap">…</span>');
      add(pages);
    }
    box.innerHTML = items.join("");
    const prev = $("#taskPrev");
    const next = $("#taskNext");
    if (prev) prev.disabled = cur <= 1;
    if (next) next.disabled = cur >= pages;
  }

  function showTaskDetail(id) {
    const t = state.taskItems.find((x) => x.id === id);
    if (!t) return;
    const rows = [
      ["任务号", t.id], ["状态", t.status], ["来源", t.source],
      ["歌曲 / 输入", t.song || t.input_name], ["音色", t.model],
      ["升降调", t.pitch == null ? "-" : (Number(t.pitch) > 0 ? `+${t.pitch}` : t.pitch) + " 半音"],
      ["触发群聊", t.group_name ? `${t.group_name}（${t.group_id}）` : (t.group_id || "私聊 / 控制台")],
      ["输出时长", fmtDuration(t.duration)], ["请求时长", t.requested],
      ["片段长度", t.clip], ["模式", t.mode], ["时间", fmtTime(t.created_at)],
      ["错误", t.error || "-"],
    ];
    $("#modalTitle").textContent = "任务详情";
    $("#modalContent").innerHTML = `<dl class="meta">${rows
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      <div id="taskAudio" class="task-audio"></div>`;
    $("#modal").hidden = false;

    const host = $("#taskAudio");
    if (t.status !== "succeeded") {
      host.innerHTML = `<p class="hint">该任务没有可播放的成品</p>`;
      return;
    }
    host.innerHTML = `<p class="hint">正在加载音频…</p>`;
    api("task_audio", { params: { id: t.id } })
      .then((d) => {
        host.innerHTML = "";
        return mountPlayer(host, {
          base64: d.audio_base64, mime: d.mime, name: d.name, duration: d.duration,
        });
      })
      .catch((err) => {
        host.innerHTML = `<p class="hint">无法播放：${esc(err.message)}</p>`;
      });
  }

  /* ---------------- 当前任务 / 队列 ---------------- */
  function stopElapsedTicker() {
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  }

  function startElapsedTicker() {
    stopElapsedTicker();
    state.elapsedTimer = setInterval(() => {
      const el = $("#nowElapsed");
      if (!el || state.taskMode !== "current") {
        stopElapsedTicker();
        return;
      }
      const secs = (state.activeElapsed || 0) + (Date.now() - (state.activeAt || Date.now())) / 1000;
      el.textContent = `已运行 ${Math.round(secs)} 秒`;
    }, 1000);
  }

  function pauseAllAudio() {
    (state.audioEls || []).forEach((a) => {
      try {
        a.pause();
      } catch (e) {
        /* ignore */
      }
    });
    state.audioEls = [];
  }

  function stopActivePolling() {
    if (state.activeTimer) {
      clearTimeout(state.activeTimer);
      state.activeTimer = null;
    }
  }

  async function loadActiveTasks() {
    const host = $("#paneCurrent");
    if (!host) return;
    if (state.taskBiz === "tts") {
      try {
        const d = await api("tts/tasks", { params: { limit: 200 } });
        state.ttsItems = d.items || [];
        state.ttsSnap = d;
      } catch (err) { /* 保持上一次快照 */ }
      renderTtsCurrent();
      if (state.tab === "tasks" && state.taskMode === "current") {
        state.activeTimer = setTimeout(loadActiveTasks, 1000);
      }
      return;
    }
    try {
      const d = await api("active_tasks");
      renderCurrent(d);
    } catch (err) {
      host.innerHTML = `<p class="empty">读取失败：${esc(err.message)}</p>`;
    }
    if (state.tab === "tasks" && state.taskMode === "current") {
      state.activeTimer = setTimeout(loadActiveTasks, 1000);
    }
  }

  function renderCurrent(d) {
    const host = $("#paneCurrent");
    const active = d.active;
    const waiting = d.waiting || [];
    const limit = d.limit == null ? 0 : d.limit;

    let nowCard;
    if (active) {
      // 步骤：1 取歌 2 人声分离 3 变声推理 4 发送；网关有信号时用真实阶段
      const cur = Number(active.step || 2);
      const steps = [
        ["取歌与音源准备", 1],
        ["人声分离", 2],
        ["变声推理", 3],
        ["发送音频", 4],
      ].map(([name, idx]) => [name, idx < cur ? "done" : idx === cur ? "cur" : "todo"]);
      const stepsHtml = steps.map(([name, st]) =>
        `<li class="${st}"><span class="s-ico">${st === "done" ? "✓" : st === "cur" ? "●" : "○"}</span>${esc(name)}</li>`).join("");
      state.activeElapsed = Number(active.elapsed || 0);
      state.activeAt = Date.now();
      nowCard = `
        <article class="card">
          <div class="card-head"><h3>正在处理</h3><span class="tag run">${esc(active.stage || "处理中")}</span></div>
          <div class="now-row">
            <div>
              <div class="t-title">${esc(active.song || active.label || "-")}</div>
              <div class="t-sub">${esc(active.artist || "歌手未知")} · ${esc(active.model || "")} · 触发用户 ${esc(active.user_name || active.user || "-")}</div>
            </div>
            <div class="hint" id="nowElapsed">已运行 ${Math.round(active.elapsed || 0)} 秒</div>
          </div>
          <ul class="steps">${stepsHtml}</ul>
          <p class="hint">${active.stage_from === "gateway"
            ? "步骤来自 Gateway 中间文件，实时反映进度。"
            : "Gateway 未提供分步信号，步骤按阶段估算。"}</p>
        </article>`;
      startElapsedTicker();
    } else {
      stopElapsedTicker();
      nowCard = `<article class="card">
        <div class="card-head"><h3>正在处理</h3><span class="tag">空闲</span></div>
        <p class="hint">当前没有正在进行的翻唱。有人在排队时这里会显示进度。</p></article>`;
    }
    const regWaitCover = (d.registry_waiting || []).filter((r) => r.type === "cover");

    const queueRows = waiting.map((w) => `
      <li><span class="q-pos">#${w.position}</span>
        <span class="t-title">${esc(w.song || w.label || "-")}</span>
        <span class="t-sub">${esc(w.user_name || w.user || "")}</span>
        <span class="hint" style="margin-left:auto">已等 ${Math.round(w.waited || 0)} 秒</span></li>`).join("")
      + regWaitCover.map((r, i) => `
      <li><span class="q-pos">#${waiting.length + i + 1}</span>
        <span class="t-title">${esc(r.label || "-")}</span>
        <span class="t-sub">${esc(r.user || "")} · 准备中</span>
        <span class="hint" style="margin-left:auto">已等 ${Math.round(r.waited || 0)} 秒</span></li>`).join("");

    host.innerHTML = `
      <div class="now-grid">
        ${nowCard}
        <article class="card">
          <div class="card-head"><h3>排队中（${waiting.length + regWaitCover.length}/${limit}）</h3></div>
          ${queueRows ? `<ul class="queue">${queueRows}</ul>` : `<p class="hint">队列为空</p>`}
          <p class="hint">同时只处理一个翻唱，其余按顺序等待；超过 ${limit} 人会提示稍后再试。</p>
        </article>
      </div>`;
  }

  function switchTaskMode(mode) {
    state.taskMode = mode;
    $$("#taskModeSeg .seg-item").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    const cur = $("#paneCurrent");
    const his = $("#paneHistory");
    if (cur) cur.hidden = mode !== "current";
    if (his) his.hidden = mode !== "history";
    const bar = $("#taskHistoryBar");
    const pager = $("#taskPagerBar");
    if (bar) bar.hidden = mode !== "history";
    if (pager) pager.hidden = mode !== "history";
    if (mode === "history") {
      stopActivePolling();
      loadTasks();
    } else {
      loadActiveTasks();
    }
  }

  /* ---------------- 模型 ---------------- */
  async function loadModels() {
    try {
      const d = await api("models");
      state.models = d;
      try {
        const cfg = await api("config");
        const vals = cfg.values || {};
        state.modelEdits = {
          cover: String(vals["rvc.models.rvc_default_model"] || "").replace(/\.pth$/i, ""),
          tts: String(vals["tts.model.tts_rvc_model"] || "").replace(/\.pth$/i, ""),
        };
      } catch (e) { state.modelEdits = { cover: "", tts: "" }; }
      $("#modelsHint").textContent = d.gateway_ok
        ? `${(d.models || []).length} 个模型${d.default_missing ? " · 默认音色不在列表中" : ""}`
        : "Gateway 不可用：" + (d.gateway_error || "");
      $("#modelsDir").textContent = d.dir ? `Gateway 模型目录：${d.dir}` : "";
      const def = String(d.default || "").replace(/\.pth$/i, "");
      $("#modelRows").innerHTML = (d.models || []).map((m) => `
        <div class="model-row ${m.name === def ? "selected" : ""}">
          <div class="model-name">${esc(m.filename)}
            <small>${m.alias ? esc(m.alias) : "未设置别名"}${m.has_index === false ? " · 无 index(自动无索引推理)" : m.has_index ? " · index ✓" : ""}${m.missing_in_gateway ? " · Gateway 未发现" : ""}</small></div>
          <input class="input" data-alias="${esc(m.name)}" value="${esc(m.alias || "")}" placeholder="别名" />
          <select class="input" data-gender="${esc(m.name)}">
            ${["未知", "男声", "女声"].map((g) =>
              `<option value="${esc(g)}" ${genderCn(m.gender) === g ? "selected" : ""}>${esc(g)}</option>`).join("")}
          </select>
          <div class="sw-cell"><label class="switch" title="翻唱使用该音色"><input type="radio" name="coverDefault" data-cover="${esc(m.name)}"
            ${state.modelEdits.cover === m.name ? "checked" : ""} /><span class="track"></span></label><small class="sw-tag">翻唱</small></div>
          <div class="sw-cell"><label class="switch" title="TTS 使用该音色"><input type="radio" name="ttsDefault" data-tts="${esc(m.name)}"
            ${state.modelEdits.tts === m.name ? "checked" : ""} /><span class="track"></span></label><small class="sw-tag">TTS</small></div>
        </div>`).join("");
      $("#modelEmpty").style.display = (d.models || []).length ? "none" : "block";
    } catch (err) {
      $("#modelsHint").textContent = "读取失败：" + err.message;
    }
  }

  async function refreshModels() {
    const btn = $("#btnRefreshModels");
    btn.disabled = true; btn.textContent = "刷新中...";
    try {
      const d = await api("models/refresh", { method: "POST", body: {} });
      const miss = d.missing_default ? `；默认音色 ${d.missing_default} 不在新列表（已保留）` : "";
      toast(`刷新完成，${(d.models || []).length} 个模型${miss}`);
      await loadModels();
    } catch (err) {
      toast("刷新失败：" + err.message, 4200);
    } finally {
      btn.disabled = false; btn.textContent = "刷新模型";
    }
  }

  function genderCn(g) {
    return { male: "男声", female: "女声", unknown: "未知", "男声": "男声", "女声": "女声", "未知": "未知" }[g] || "未知";
  }

  async function saveModelsUnified() {
    const kw = [];
    const genders = {};
    const extra = {};
    let cover = "", tts = "";
    const coverChecked = document.querySelector('input[name="coverDefault"]:checked');
    const ttsChecked = document.querySelector('input[name="ttsDefault"]:checked');
    $$("#modelRows .model-row").forEach((row) => {
      const filename = ($("[data-alias]", row) || {}).dataset ? row.querySelector("[data-alias]").dataset.alias : "";
      const alias = row.querySelector("[data-alias]") ? row.querySelector("[data-alias]").value.trim() : "";
      const gender = row.querySelector("[data-gender]") ? row.querySelector("[data-gender]").value : "未知";
      kw.push(`${filename.endsWith(".pth") ? filename : filename + ".pth"}|||${alias}`);
      const stem = filename.replace(/\.pth$/i, "");
      genders[stem] = gender;
      if (coverChecked && coverChecked.dataset.cover === filename) cover = stem;
      if (ttsChecked && ttsChecked.dataset.tts === filename) tts = filename;
    });
    const values = {
      "rvc.models.rvc_models_keywords": kw,
      "rvc.models.rvc_default_model": cover,
      "tts.model.tts_rvc_model": tts ? `${tts}.pth` : "",
      "tuning.gender.model_genders": genders,
      "tuning.gender.model_genders_extra": extra,
    };
    const btn = $("#btnSaveModels");
    btn.disabled = true;
    try {
      const d = await api("config/save", { method: "POST", body: { values } });
      toast(d.changed && d.changed.length ? `已保存 ${d.changed.length} 项` : "没有变化");
      if (d.errors && d.errors.length) toast("部分未保存：" + d.errors.join("；"), 5200);
      await loadModels();
    } catch (err) {
      toast("保存失败：" + err.message, 5200);
    } finally {
      btn.disabled = false;
    }
  }

  async function saveModelRow(name) {
    const aliasEl = $(`[data-alias="${CSS.escape(name)}"]`);
    const genderEl = $(`[data-gender="${CSS.escape(name)}"]`);
    try {
      await api("models/alias", { method: "POST", body: { model: name, alias: aliasEl.value.trim() } });
      await api("models/gender", { method: "POST", body: { model: name, gender: genderEl.value } });
      toast("已保存");
      loadModels();
    } catch (err) {
      toast("保存失败：" + err.message, 4200);
    }
  }

  async function setDefaultModel(name) {
    try {
      await api("models/default", { method: "POST", body: { model: name } });
      toast(`默认音色 → ${name}`);
      loadModels();
    } catch (err) {
      toast("设置失败：" + err.message, 4200);
    }
  }

  /* ---------------- TTS 控制台（CUSTOM-7） ---------------- */
  async function prepareTts() {
    try {
      const [prov, models] = await Promise.all([api("tts/providers"), api("models")]);
      const sel = $("#ttsProvider");
      const providers = prov.providers || [];
      sel.innerHTML = providers.length
        ? providers.map((p) => `<option value="${esc(p.id)}">${esc(p.id || p.class)}${p.model ? `（${esc(p.model)}）` : ""}</option>`).join("")
        : `<option value="">（未配置 TTS 服务商）</option>`;
      const wanted = prov.configured || prov.using || (providers[0] && providers[0].id) || "";
      if (wanted && providers.some((p) => p.id === wanted)) sel.value = wanted;
      $("#ttsProviderInfo").textContent = providers.length
        ? `Provider ${sel.value || "-"} · 已配置 ${providers.length} 个`
        : "未配置 TTS 服务商（请先到 AstrBot 服务商配置添加）";
      const msel = $("#ttsModel");
      const options = (models.models || []).filter((m) => !m.missing_in_gateway);
      msel.innerHTML = options.map((m) =>
        `<option value="${esc(m.name)}">${esc(m.alias || m.name)}${m.alias ? `（${esc(m.name)}）` : ""}</option>`).join("")
        || `<option value="">（无可用音色）</option>`;
      const ttsDef = String(prov.tts_model || "").replace(/\.pth$/i, "");
      if (ttsDef && options.some((m) => m.name === ttsDef)) msel.value = ttsDef;
      $("#ttsModelInfo").textContent = prov.tts_model ? `TTS 音色 ${prov.tts_model}` : "";
      $("#ttsStatus").textContent = prov.tts_enable ? "就绪" : "就绪（提示：TTS 语音回复当前处于关闭状态，试听不受影响）";
    } catch (err) {
      $("#ttsStatus").textContent = "初始化失败：" + err.message;
    }
  }

  async function runTts() {
    const btn = $("#ttsRun");
    const text = ($("#ttsText").value || "").trim();
    if (!text) { $("#ttsStatus").textContent = "请先输入文本"; return; }
    btn.disabled = true;
    $("#ttsStatus").textContent = "准备中...";
    $("#ttsResult").hidden = true;
    $("#ttsOutputs").innerHTML = "";
    try {
      const body = {
        text,
        provider_id: $("#ttsProvider").value || "",
        model: $("#ttsModel").value || "",
        reverb: $("#ttsReverb").value,
        pitch: Number($("#ttsPitch").value || 0),
        output_format: $("#ttsFormat").value,
      };
      const d = await api("tts/generate", { method: "POST", body });
      $("#ttsStatus").textContent = "已提交：TTS 生成 → RVC 变声（CPU，请稍候）";
      pollJob(d.job_id, "#ttsStatus", (job) => {
        btn.disabled = false;
        if (job.status === "succeeded") showOutputs(job.id, "#ttsOutputs", "#ttsResult");
        else if (job.status === "failed") toast("TTS 失败：" + (job.error || "未知错误"), 5200);
      }, 1000);
    } catch (err) {
      $("#ttsStatus").textContent = "失败：" + err.message;
      btn.disabled = false;
    }
  }

  /* ---------------- 音频效果（Reverb / echo） ---------------- */
  const AUDIO_KEYS_COVER = [
    "reverb_enabled", "reverb_wet", "reverb_dry", "reverb_room_size",
    "reverb_decay", "reverb_damping", "reverb_pre_delay", "reverb_width",
  ];
  const AUDIO_KEYS_TTS = [
    "tts_reverb_enabled", "tts_reverb_wet", "tts_reverb_dry", "tts_reverb_room_size",
    "tts_reverb_decay", "tts_reverb_damping", "tts_reverb_pre_delay", "tts_reverb_width",
  ];
  const AUDIO_KEYS_ECHO = ["echo_enabled", "echo_wet", "echo_decay"];

  function bindFieldInputs(root) {
    $$("[data-key]", root).forEach((el) => {
      const evt = el.dataset.type === "bool" ? "change" : "input";
      el.addEventListener(evt, () => {
        state.dirty[el.dataset.key] = el.dataset.type === "bool" ? el.checked : el.value;
        const badge = el.closest(".field") && el.closest(".field").querySelector(".dirty");
        if (badge) badge.style.display = "inline";
      });
    });
  }

  function audioCard(title, desc, fields) {
    if (!fields.length) return "";
    return `<article class="card audio-card">
      <div class="card-head"><h3>${esc(title)}</h3></div>
      <p class="f-hint">${esc(desc)}</p>
      <div class="group-body">${fields.map(renderField).join("")}</div>
    </article>`;
  }

  async function loadAudioTab() {
    try {
      await loadConfig();
      const byKey = {};
      state.fields.forEach((f) => { byKey[f.key] = f; });
      const pick = (keys) => keys.map((k) => byKey[k]).filter(Boolean);
      let html = audioCard(
        "歌曲翻唱 Reverb（卷积混响）",
        "只作用于翻唱成品的人声：合成 IR 卷积混响，8 个参数全部真实生效。默认关闭；关闭时输出与原流程完全一致。wet=0.18/dry=1.0/room=0.65/decay=1.4s 是推荐起点。",
        pick(AUDIO_KEYS_COVER));
      html += audioCard(
        "TTS 语音 Reverb（独立配置）",
        "只作用于 TTS 转换后的语音，与歌曲翻唱的混响互不影响、可分别开关。",
        pick(AUDIO_KEYS_TTS));
      const echoFields = pick(AUDIO_KEYS_ECHO);
      if (echoFields.length) {
        html += `<article class="card audio-card">
          <div class="card-head"><h3>legacy aecho 回声（高级）</h3></div>
          <p class="f-hint">旧的重复回声效果（不是混响）。与 Reverb 互斥：Reverb 开启时本组不生效。默认关闭，展开修改。</p>
          <details><summary class="f-hint">展开高级选项</summary>
            <div class="group-body">${echoFields.map(renderField).join("")}</div>
          </details>
        </article>`;
      }
      $("#audioCards").innerHTML = html;
      bindFieldInputs($("#audioCards"));
      $("#audioHint").textContent = "改完点「保存音频效果设置」；混响参数按次请求读取，无需重载插件。";
    } catch (err) {
      $("#audioHint").textContent = "读取失败：" + err.message;
    }
  }

  async function saveAudioCfg() {
    await saveConfig();
    await loadAudioTab();
  }

  /* ---------------- 设置 ---------------- */
  async function loadConfig() {
    try {
      const [d, models] = await Promise.all([api("config"), api("models").catch(() => ({}))]);
      state.fields = d.fields || [];
      state.values = d.values || {};
      state.dirty = {};
      state.needsReload = d.reload_required || [];
      // CUSTOM-9：默认音色 / TTS 音色模型下拉用网关实时列表（新模型刷新即见）
      const rows = (models && models.models ? models.models : []).filter((m) => !m.missing_in_gateway);
      state.gatewayModelStems = rows.map((m) => m.name);
      state.gatewayModelFiles = rows.map((m) => m.filename);
      $("#cfgHint").textContent = "Cookie / Token 等敏感项请到 AstrBot 原生配置页修改";
      renderConfig();
    } catch (err) {
      $("#cfgHint").textContent = "读取失败：" + err.message;
    }
  }

  function selectCfgGroup(index) {
    $$("#cfgGroups .group").forEach((sec) => {
      sec.hidden = Number(sec.dataset.gi) !== index;
    });
    $$("#cfgGroupSeg .seg-item").forEach((b) => b.classList.toggle("active", Number(b.dataset.gi) === index));
    state.cfgGroup = index;
  }

  /* CUSTOM-7：设置页分类整合 —— 9 张 schema 卡片在页面上归并为 5 个大类，
     卡片内部保留原 schema 卡片名作小节标题；只影响本页展示，不改配置结构与键名。 */
  const CFG_CATEGORY = {
    basic: "基础与翻唱流程",
    selection: "基础与翻唱流程",
    rvc: "音色与推理",
    tuning: "音色与推理",
    separation: "音频输出与本地翻唱",
    local_audio: "音频输出与本地翻唱",
    tts: "TTS 语音回复",
    sources: "音源与高级",
    advanced: "音源与高级",
  };

  /* CUSTOM-7.2：把散在各卡片的通知类开关归并到「通知与播报」大类，
     并按 成功通知 / 过程通知 / 管理员告警 分小节；只影响本页展示，不改配置键。 */
  const CFG_KEY_CATEGORY = {
    "selection.smart.enable_llm_success_notify": "通知与播报",
    "selection.smart.enable_config_report": "通知与播报",
    "selection.smart.notify_progress": "通知与播报",
    "selection.smart.notify_retry": "通知与播报",
    "tuning.notify_persona_id": "通知与播报",
    "sources.alert_admin_on_cookie_expired": "通知与播报",
    "sources.cookie_alert_interval_hours": "通知与播报",
    "advanced.queue_position_notice": "通知与播报",
  };
  const CFG_NOTIFY_SUB = {
    "selection.smart.enable_llm_success_notify": "成功通知（二选一或同时开）",
    "selection.smart.enable_config_report": "成功通知（二选一或同时开）",
    "tuning.notify_persona_id": "成功通知（二选一或同时开）",
    "selection.smart.notify_progress": "过程通知",
    "selection.smart.notify_retry": "过程通知",
    "sources.alert_admin_on_cookie_expired": "管理员告警",
    "sources.cookie_alert_interval_hours": "管理员告警",
    "advanced.queue_position_notice": "排队通知",
  };

  /* CUSTOM-7.1：音色列表编辑器 —— 把 rvc_models_keywords（别名）与 model_genders（性别）
     合并成一个表格，每行 = 模型 + 别名输入框 + 性别下拉；改任意一项同时写两个键的 dirty。 */
  const MODEL_EDITOR_FIELD_KEYS = new Set([
    "rvc.models.rvc_models_keywords",
    "tuning.gender.model_genders",
    "tuning.gender.model_genders_extra",
  ]);

  function modelEditorHtml() {
    const kw = state.values["rvc.models.rvc_models_keywords"] || [];
    // 配置读取接口只下发叶子键（...model_genders.deng 等），从叶子键汇总显示值
    const genders = Object.assign(
      {},
      state.values["tuning.gender.model_genders_extra"] || {},
    );
    state.fields.forEach((f) => {
      if (f.key.startsWith("tuning.gender.model_genders.")) {
        genders[f.key.split(".").pop()] = state.values[f.key];
      }
    });
    // 性别值与 schema 一致，直接用中文（未知/男声/女声）
    const genderOpts = (cur) => ["未知", "男声", "女声"].map((gv) =>
      `<option value="${esc(gv)}" ${cur === gv ? "selected" : ""}>${esc(gv)}</option>`).join("");
    const rows = kw.map((raw, i) => {
      const parts = String(raw || "").split("|||");
      const filename = (parts[0] || "").trim();
      const stem = filename.replace(/\.pth$/i, "");
      return `<tr data-row="${i}" data-filename="${esc(filename)}">
        <td class="me-name">${esc(stem)}</td>
        <td><input class="input me-alias" data-row="${i}" value="${esc((parts[1] || "").trim())}" placeholder="别名（可选）" /></td>
        <td><select class="input me-gender" data-row="${i}">${genderOpts(genders[stem] || "unknown")}</select></td>
      </tr>`;
    }).join("");
    return `<div class="field"><label>音色列表（别名 / 性别）
        <span class="badge-warn dirty" style="display:none" data-me-dirty>已修改</span></label>
      <div class="f-ctrl">
        <table class="me-table">
          <thead><tr><th>模型</th><th>别名</th><th>性别</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="3" class="hint">暂无音色，先刷新模型列表</td></tr>`}</tbody>
        </table>
        <p class="f-hint">别名用于对话里按名字选模型；性别用于自动升降调。模型由网关扫描提供，新增/删除模型请用「模型管理」页刷新。</p>
      </div></div>`;
  }

  function bindModelEditor() {
    const kwBase = Array.isArray(state.values["rvc.models.rvc_models_keywords"])
      ? state.values["rvc.models.rvc_models_keywords"].slice() : [];
    const schemaStems = state.fields
      .filter((f) => f.key.startsWith("tuning.gender.model_genders."))
      .map((f) => f.key.split(".").pop());
    const genderBase = Object.assign(
      {},
      state.values["tuning.gender.model_genders_extra"] || {},
    );
    state.fields.forEach((f) => {
      if (f.key.startsWith("tuning.gender.model_genders.")) {
        genderBase[f.key.split(".").pop()] = state.values[f.key];
      }
    });

    const markDirty = () => {
      const host = $("#cfgGroups");
      const badge = host && host.querySelector("[data-me-dirty]");
      if (badge) badge.style.display = "inline";
    };
    const writeKeywords = () => {
      const list = kwBase.slice();
      $$("#cfgGroups tr[data-filename]").forEach((tr) => {
        const i = Number(tr.dataset.row);
        const alias = ($(`.me-alias[data-row="${i}"]`, tr.closest("table")) || {}).value || "";
        list[i] = `${tr.dataset.filename}|||${String(alias).trim()}`;
      });
      state.dirty["rvc.models.rvc_models_keywords"] = list;
    };
    const writeGenders = () => {
      const merged = Object.assign({}, genderBase);
      $$("#cfgGroups tr[data-filename]").forEach((tr) => {
        const i = Number(tr.dataset.row);
        const sel = $(`.me-gender[data-row="${i}"]`, tr.closest("table"));
        if (!sel) return;
        const stem = tr.dataset.filename.replace(/\.pth$/i, "");
        merged[stem] = sel.value;
      });
      const main = {};
      const extra = {};
      Object.keys(merged).forEach((stem) => {
        (schemaStems.indexOf(stem) >= 0 ? main : extra)[stem] = merged[stem];
      });
      state.dirty["tuning.gender.model_genders"] = main;
      state.dirty["tuning.gender.model_genders_extra"] = extra;
    };
    $$("#cfgGroups .me-alias").forEach((el) =>
      el.addEventListener("input", () => { writeKeywords(); markDirty(); }));
    $$("#cfgGroups .me-gender").forEach((el) =>
      el.addEventListener("change", () => { writeGenders(); markDirty(); }));
  }

  function renderConfig() {
    const cats = new Map();
    const editorGroups = new Set();  // 含 rvc_models_keywords 的小节 → 注入音色列表编辑器
    state.fields.forEach((f) => {
      // model_genders 的每个模型（deng/nuoyemu/rym）是独立叶子键，一并过滤
      if (MODEL_EDITOR_FIELD_KEYS.has(f.key) || f.key.startsWith("tuning.gender.model_genders.")) {
        if (f.key === "rvc.models.rvc_models_keywords") editorGroups.add(f.group);
        return;
      }
      const prefix = String(f.key || "").split(".")[0];
      const cat = CFG_KEY_CATEGORY[f.key] || CFG_CATEGORY[prefix] || f.group || "其他";
      if (!cats.has(cat)) cats.set(cat, new Map());
      const inner = cats.get(cat);
      const g = CFG_KEY_CATEGORY[f.key] ? (CFG_NOTIFY_SUB[f.key] || f.group) : (f.group || "其他");
      if (!inner.has(g)) inner.set(g, []);
      inner.get(g).push(f);
    });
    const html = [];
    const names = [];
    cats.forEach((inner, cat) => {
      const gi = names.length;
      names.push(cat);
      const sections = [];
      inner.forEach((fields, g) => {
        const editor = editorGroups.has(g) ? modelEditorHtml() : "";
        if (g === cat) {
          sections.push(`${editor}<div class="group-body">${fields.map(renderField).join("")}</div>`);
        } else {
          sections.push(`${editor}<h4 class="cfg-sub">${esc(g)}</h4>
            <div class="group-body">${fields.map(renderField).join("")}</div>`);
        }
      });
      html.push(`<section class="group" data-gi="${gi}"><h3>${esc(cat)}</h3>${sections.join("")}</section>`);
    });
    $("#cfgGroups").innerHTML = html.join("");
    const seg = $("#cfgGroupSeg");
    if (seg) {
      seg.innerHTML = names.map((g, i) =>
        `<button class="seg-item ${i === 0 ? "active" : ""}" data-gi="${i}" type="button">${esc(g)}</button>`).join("");
      $$("#cfgGroupSeg .seg-item").forEach((b) => b.addEventListener("click", () => selectCfgGroup(Number(b.dataset.gi))));
    }
    // 保存/重新读取后停留在当前分类，不跳回第一个
    const keep = Math.min(Number(state.cfgGroup) || 0, names.length - 1);
    selectCfgGroup(Math.max(0, keep));
    bindModelEditor();
    $$("#cfgGroups [data-key]").forEach((el) => {
      const evt = el.dataset.type === "bool" ? "change" : "input";
      el.addEventListener(evt, () => {
        state.dirty[el.dataset.key] = el.dataset.type === "bool" ? el.checked : el.value;
        const badge = el.closest(".field") && el.closest(".field").querySelector(".dirty");
        if (badge) badge.style.display = "inline";
      });
    });
  }

  const DYNAMIC_MODEL_FIELDS = {
    "rvc.models.rvc_default_model": () => state.gatewayModelStems,
    "tts.model.tts_rvc_model": () => state.gatewayModelFiles,
  };

  function renderField(f) {
    const value = state.values[f.key];
    const tag = (state.needsReload || []).includes(f.key)
      ? '<span class="badge-warn">需重载插件生效</span>' : "";
    const dirty = '<span class="badge-warn dirty" style="display:none">已修改</span>';
    const hint = f.hint ? `<p class="f-hint">${esc(f.hint)}</p>` : "";
    let ctrl;
    const dynModels = DYNAMIC_MODEL_FIELDS[f.key];
    if (dynModels) {
      const opts = dynModels();
      const cur = String(value ?? "");
      const list = cur && !opts.includes(cur) ? [cur, ...opts] : opts.slice();
      if (!cur || f.key === "rvc.models.rvc_default_model") list.unshift("");
      const labelOf = (o) => (o === "" ? (f.key === "rvc.models.rvc_default_model" ? "（留空 = 用列表第 1 个）" : "（留空 = 用列表第 1 个）") : o);
      ctrl = `<select class="input" data-key="${esc(f.key)}" data-type="string">
        ${list.map((o) => `<option value="${esc(o)}" ${String(value ?? "") === o ? "selected" : ""}>${esc(labelOf(o))}</option>`).join("")}
      </select>${opts.length ? "" : `<p class="f-hint">网关模型列表为空/离线：可直接输入模型文件名，或先到「模型管理」刷新。</p>`}`;
    } else if (f.sensitive) {
      ctrl = `<input class="input" data-key="${esc(f.key)}" data-type="string" type="password"
        placeholder="${state.sensitiveConfigured && state.sensitiveConfigured[f.key] ? "已配置（留空不修改）" : "未配置"}" />`;
    } else if (f.type === "bool") {
      ctrl = `<label class="switch"><input type="checkbox" data-key="${esc(f.key)}" data-type="bool"
        ${value ? "checked" : ""} /><span class="track"></span></label>`;
    } else if (f.options && f.options.length) {
      ctrl = `<select class="input" data-key="${esc(f.key)}" data-type="string">
        ${f.options.map((o) => `<option value="${esc(o)}" ${String(value) === String(o) ? "selected" : ""}>${esc(o)}</option>`).join("")}
      </select>`;
    } else if (f.type === "int" || f.type === "float") {
      const step = f.type === "int" ? "1" : "0.01";
      ctrl = `<input class="input" type="number" data-key="${esc(f.key)}" data-type="${f.type}" step="${step}"
        ${f.minimum != null ? `min="${f.minimum}"` : ""} ${f.maximum != null ? `max="${f.maximum}"` : ""}
        value="${esc(value ?? "")}" />`;
    } else if (f.type === "list") {
      ctrl = `<textarea class="input" data-key="${esc(f.key)}" data-type="list">${esc(Array.isArray(value) ? value.join("\n") : value || "")}</textarea>`;
    } else if (f.type === "dict" || f.type === "object") {
      ctrl = `<textarea class="input" data-key="${esc(f.key)}" data-type="${f.type}">${esc(JSON.stringify(value ?? {}, null, 1))}</textarea>`;
    } else if (f.type === "text") {
      ctrl = `<textarea class="input" data-key="${esc(f.key)}" data-type="string">${esc(value ?? "")}</textarea>`;
    } else {
      ctrl = `<input class="input" data-key="${esc(f.key)}" data-type="string" value="${esc(value ?? "")}" />`;
    }
    return `<div class="field"><label>${esc(f.label)} ${tag} ${dirty}</label>
      <div class="f-ctrl">${ctrl}${hint}</div></div>`;
  }

  async function saveConfig() {
    const keys = Object.keys(state.dirty);
    if (!keys.length) { toast("没有修改"); return; }
    const values = {};
    for (const key of Object.keys(state.dirty)) {
      // 全部提交：音色列表编辑器写的是对象节点键（不在 state.fields 里）
      values[key] = state.dirty[key];
    }
    const btn = $("#btnSaveCfg");
    btn.disabled = true;
    try {
      const d = await api("config/save", { method: "POST", body: { values } });
      state.needsReload = d.needs_reload || [];
      toast(d.changed && d.changed.length
        ? `已保存 ${d.changed.length} 项${d.needs_reload && d.needs_reload.length ? "（含需重载项）" : ""}`
        : "没有变化");
      if (d.errors && d.errors.length) toast("部分未保存：" + d.errors.join("；"), 5200);
      await loadConfig();
    } catch (err) {
      toast("保存失败：" + err.message, 5200);
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------------- RVC 转换 ---------------- */
  async function prepareRvc() {
    try {
      const d = await api("models");
      state.models = d;
      const options = (d.models || []).filter((m) => !m.missing_in_gateway);
      const sel = $("#rvcModel");
      const keep = sel.value;
      sel.innerHTML = options.map((m) =>
        `<option value="${esc(m.name)}">${esc(m.alias || m.name)}${m.alias ? `（${esc(m.name)}）` : ""}</option>`).join("")
        || `<option value="">（无可用音色）</option>`;
      const def = String(d.default || "").replace(/\.pth$/i, "");
      if (keep && options.some((m) => m.name === keep)) sel.value = keep;
      else if (def && options.some((m) => m.name === def)) sel.value = def;
      $("#rvcGw").textContent = d.gateway_ok ? "Gateway 在线" : "Gateway 离线";
      $("#rvcGw").title = d.dir || "";
      $("#rvcModelDir").textContent = d.dir || "";
      setStatus("#stGateway", d.gateway_ok ? "ok" : "err", `Gateway <b>${d.gateway_ok ? "正常" : "异常"}</b>`);
    } catch (err) {
      $("#rvcStatus").textContent = "模型读取失败：" + err.message;
    }
  }

  function readFileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const s = String(reader.result || "");
        resolve(s.slice(s.indexOf(",") + 1));
      };
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file);
    });
  }

  async function uploadAudio(inputSel, noteSel, stateKey) {
    const file = $(inputSel).files && $(inputSel).files[0];
    if (!file) {
      if (state[stateKey]) return state[stateKey];
      throw new Error("请先选择音频文件");
    }
    if (state.uploading) throw new Error("正在上传，请稍候");
    if (file.size > 24 * 1024 * 1024) throw new Error("文件超过 24MB");
    state.uploading = true;
    $(noteSel).textContent = "上传中...";
    try {
      const data = await readFileBase64(file);
      const d = await api("rvc/upload", { method: "POST", body: { name: file.name, data_base64: data } });
      state[stateKey] = d.upload_id;
      $(noteSel).textContent = `${d.name} · ${d.size_text}`;
      return d.upload_id;
    } finally {
      state.uploading = false;
    }
  }

  function rvcParams() {
    const p = {
      key_shift: Number($("#rvcPitch").value || 0),
      f0_method: $("#rvcF0").value,
      index_rate: Number($("#rvcIndexRate").value || 0),
      rms_mix_rate: Number($("#rvcRms").value || 0),
      protect: Number($("#rvcProtect").value || 0),
      vocal_volume_db: Number($("#rvcVocalDb").value || 0),
      output_format: $("#rvcFormat").value,
      chorus_seconds: Number($("#rvcSeconds").value || 20),
      fade_seconds: Number($("#rvcFade").value || 0),
      speaker_id: Number($("#rvcSpeaker").value || 0),
    };
    const st = $("#rvcStart").value.trim();
    const et = $("#rvcEnd").value.trim();
    const du = $("#rvcDuration").value.trim();
    if (st) p.start_time = st;
    if (et) p.end_time = et;
    if (du) p.duration = du;
    // 人声混响（仅本次转换试听，勾选才提交）
    if ($("#rvcReverbOn").checked) {
      p.reverb_enabled = true;
      p.reverb_wet = Number($("#rvcRevWet").value || 0.18);
      p.reverb_dry = Number($("#rvcRevDry").value || 1.0);
      p.reverb_room_size = Number($("#rvcRevRoom").value || 0.65);
      p.reverb_decay = Number($("#rvcRevDecay").value || 1.4);
      p.reverb_damping = Number($("#rvcRevDamp").value || 0.45);
      p.reverb_pre_delay = Number($("#rvcRevPre").value || 18);
      p.reverb_width = Number($("#rvcRevWidth").value || 0.8);
    }
    return p;
  }

  async function runConvert() {
    const btn = $("#rvcRun");
    btn.disabled = true;
    $("#rvcStatus").textContent = "准备中...";
    try {
      const uploadId = await uploadAudio("#rvcFile", "#rvcUploadNote", "uploadId");
      const body = Object.assign({ upload_id: uploadId, model: $("#rvcModel").value }, rvcParams());
      $("#rvcStatus").textContent = "提交中...";
      const d = await api("rvc/convert", { method: "POST", body });
      $("#rvcResult").hidden = true;
      $("#rvcOutputs").innerHTML = "";
      $("#rvcStatus").textContent = "已提交，CPU 推理可能持续数分钟";
      pollJob(d.job_id, "#rvcStatus", (job) => {
        $("#rvcRun").disabled = false;
        if (job.status === "succeeded") showOutputs(job.id, "#rvcOutputs", "#rvcResult");
      });
    } catch (err) {
      $("#rvcStatus").textContent = "失败：" + err.message;
      btn.disabled = false;
    }
  }

  /* ---------------- 分离 ---------------- */
  async function prepareSeparate() {
    try {
      const d = await api("rvc/sep_models");
      const sel = $("#sepModel");
      const keep = sel.value;
      sel.innerHTML = (d.models || []).map((m) =>
        `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`).join("")
        || `<option value="">（无分离模型）</option>`;
      if (keep && (d.models || []).some((m) => m.id === keep)) sel.value = keep;
      else if (d.default) sel.value = d.default;
      $("#sepInfo").textContent = d.models && d.models.length
        ? `可用分离模型 ${d.models.length} 个，默认 ${d.default}`
        : "未读取到分离模型";
    } catch (err) {
      $("#sepStatus").textContent = "分离模型读取失败：" + err.message;
    }
  }

  async function runSeparate() {
    const btn = $("#sepRun");
    btn.disabled = true;
    state.sepRunning = true;
    $("#sepStatus").textContent = "准备中...";
    try {
      const uploadId = await uploadAudio("#sepFile", "#sepUploadNote", "sepUploadId");
      const body = {
        upload_id: uploadId,
        model: $("#sepModel").value,
        mode: $("#sepMode").value,
        output_format: $("#sepFormat").value,
      };
      const d = await api("rvc/separate", { method: "POST", body });
      $("#sepOutputs").innerHTML = "";
      $("#sepStatus").textContent = "已提交，分离中（约 1-2 分钟）";
      pollJob(d.job_id, "#sepStatus", (job) => {
        btn.disabled = false;
        state.sepRunning = false;
        if (job.status === "succeeded") {
          $("#sepInfo").textContent = `完成 · 模型 ${job.sep_model || "-"} · 导出 ${job.format || "-"}`;
          showOutputs(job.id, "#sepOutputs", null);
        }
      });
    } catch (err) {
      $("#sepStatus").textContent = "失败：" + err.message;
      btn.disabled = false;
      state.sepRunning = false;
    }
  }

  /* ---------------- 任务轮询与输出 ---------------- */
  function pollJob(jobId, statusSel, onDone, intervalMs) {
    clearTimeout(state.timer);
    state.jobId = jobId;
    const gap = Math.max(500, Number(intervalMs) || 2500);
    const tick = async () => {
      if (state.jobId !== jobId) return;
      try {
        const d = await api("rvc/status", { params: { job_id: jobId } });
        const elapsed = d.elapsed ? `（已 ${Math.round(d.elapsed)}s）` : "";
        $(statusSel).textContent = `${d.stage || d.status}${elapsed}`;
        if (d.status === "succeeded" || d.status === "failed" || d.status === "unknown") {
          if (onDone) onDone(d);
          return;
        }
        state.timer = setTimeout(tick, gap);
      } catch (err) {
        $(statusSel).textContent = "状态查询失败：" + err.message;
        state.timer = setTimeout(tick, gap * 2);
      }
    };
    tick();
  }

  /* ---------------- 波形播放器 ---------------- */
  function fmtClock(sec) {
    const n = Math.max(0, Number(sec) || 0);
    return `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
  }

  function audioBlobFromBase64(b64, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime || "audio/wav" });
  }

  async function decodePeaks(arrayBuffer, buckets) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("浏览器不支持音频解码");
    const ctx = new Ctx();
    try {
      const buf = await ctx.decodeAudioData(arrayBuffer);
      const ch = buf.getChannelData(0);
      const step = Math.max(1, Math.floor(ch.length / buckets));
      const peaks = new Array(buckets).fill(0);
      for (let i = 0; i < buckets; i += 1) {
        let max = 0;
        const start = i * step;
        for (let j = 0; j < step; j += 1) {
          const v = Math.abs(ch[start + j] || 0);
          if (v > max) max = v;
        }
        peaks[i] = max;
      }
      const norm = Math.max(0.0001, Math.max.apply(null, peaks));
      return { peaks: peaks.map((v) => v / norm), duration: buf.duration };
    } finally {
      ctx.close();
    }
  }

  async function mountPlayer(host, opts) {
    host.innerHTML = `
      <div class="player">
        <button class="p-play" type="button" aria-label="播放">▶</button>
        <div class="p-track"><canvas></canvas></div>
        <span class="p-time">0:00 / --:--</span>
        <a class="btn ghost sm p-dl" download>下载</a>
      </div>`;
    const blob = audioBlobFromBase64(opts.base64, opts.mime);
    const url = URL.createObjectURL(blob);
    state.urls.push(url);
    const audio = new Audio(url);
    audio.preload = "metadata";
    state.audioEls.push(audio);
    const playBtn = host.querySelector(".p-play");
    const canvas = host.querySelector("canvas");
    const timeEl = host.querySelector(".p-time");
    const dl = host.querySelector(".p-dl");
    dl.href = url;
    dl.download = opts.name || "output.wav";

    let peaks = null;
    let duration = Number(opts.duration) || 0;
    let dragging = false;
    let raf = 0;

    const draw = () => {
      if (!peaks) return;
      const w = canvas.clientWidth || 320;
      const h = canvas.clientHeight || 44;
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
      }
      const g = canvas.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const cs = getComputedStyle(document.documentElement);
      const played = (cs.getPropertyValue("--blue") || "#2f6fed").trim();
      const rest = (cs.getPropertyValue("--line-strong") || "#d8dce1").trim();
      const prog = duration ? Math.min(1, audio.currentTime / duration) : 0;
      const n = peaks.length;
      const bw = Math.max(2, Math.floor(w / n) - 1);
      peaks.forEach((p, i) => {
        const x = (i * w) / n;
        const bh = Math.max(2, p * (h - 8));
        g.fillStyle = (i + 0.5) / n <= prog ? played : rest;
        g.fillRect(x, (h - bh) / 2, bw, bh);
      });
    };

    const updateTime = () => {
      timeEl.textContent = `${fmtClock(audio.currentTime)} / ${fmtClock(duration || audio.duration)}`;
    };

    const loop = () => {
      if (audio.paused) return;
      draw();
      raf = requestAnimationFrame(loop);
    };

    audio.addEventListener("loadedmetadata", () => {
      if (!duration) duration = audio.duration;
      updateTime();
      draw();
    });
    audio.addEventListener("play", () => { playBtn.textContent = "❚❚"; loop(); });
    audio.addEventListener("pause", () => { playBtn.textContent = "▶"; cancelAnimationFrame(raf); });
    audio.addEventListener("ended", () => {
      playBtn.textContent = "▶";
      audio.currentTime = 0;
      updateTime();
      draw();
    });
    audio.addEventListener("timeupdate", () => { updateTime(); if (audio.paused) draw(); });

    playBtn.addEventListener("click", () => {
      if (audio.paused) {
        audio.play().catch((err) => toast("播放失败：" + err.message));
      } else {
        audio.pause();
      }
    });

    const seekTo = (clientX) => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      if (duration) audio.currentTime = ratio * duration;
      updateTime();
      draw();
    };
    canvas.addEventListener("pointerdown", (e) => {
      dragging = true;
      canvas.setPointerCapture(e.pointerId);
      seekTo(e.clientX);
    });
    canvas.addEventListener("pointermove", (e) => { if (dragging) seekTo(e.clientX); });
    canvas.addEventListener("pointerup", () => { dragging = false; });
    canvas.addEventListener("pointercancel", () => { dragging = false; });

    updateTime();
    try {
      const decoded = await decodePeaks(await blob.arrayBuffer(), 180);
      peaks = decoded.peaks;
      duration = decoded.duration || duration;
      updateTime();
      draw();
    } catch (err) {
      // 解码失败时退回原生控件，保证能听
      const fallback = document.createElement("audio");
      fallback.controls = true;
      fallback.src = url;
      host.innerHTML = "";
      host.appendChild(fallback);
    }
  }

  async function showOutputs(jobId, containerSel, revealSel) {
    const container = $(containerSel);
    try {
      const d = await api("rvc/result", { params: { job_id: jobId } });
      const outs = d.outputs || [];
      if (!outs.length) { container.innerHTML = `<p class="empty">没有可播放的结果</p>`; return; }
      container.innerHTML = outs.map((o, i) => `
        <div class="output-item">
          <div class="o-head"><span>${esc(o.label_text || o.label)} · ${esc(o.name)} · ${fmtDuration(o.duration)}</span>
            <span class="hint">${esc(o.size_text || "")}</span></div>
          <div class="player-host" data-idx="${i}"></div>
        </div>`).join("");
      const hosts = $$(".player-host", container);
      outs.forEach((o, i) => {
        if (o.error) {
          hosts[i].innerHTML = `<p class="hint">${esc(o.error)}</p>`;
          return;
        }
        mountPlayer(hosts[i], {
          base64: o.audio_base64, mime: o.mime, name: o.name, duration: o.duration,
        });
      });
      if (revealSel) $(revealSel).hidden = false;
    } catch (err) {
      container.innerHTML = `<p class="empty">读取结果失败：${esc(err.message)}</p>`;
    }
  }

  /* ---------------- 诊断 ---------------- */
  async function loadDiagnostics() {
    try {
      const d = await api("diagnostics");
      $("#diagHint").textContent = "只读状态检查，不触发推理";
      $("#diagChecks").innerHTML = (d.checks || []).map((c) => `
        <li data-ok="${c.ok ? "1" : "0"}" data-level="${esc(c.level || "")}">
          <span class="dot"></span>
          <div><div class="c-name">${esc(c.name)}</div><div class="c-detail">${esc(c.detail)}</div></div>
        </li>`).join("");
      $("#diagPaths").innerHTML = Object.entries(d.paths || {})
        .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      const errs = d.errors || [];
      $("#diagErrors").innerHTML = errs.length
        ? errs.map((e) => `<li><span class="l-time">${esc(e.time || "")}</span>${esc(e.text)}
            ${e.context ? `<span class="l-ctx">${esc(e.context)}</span>` : ""}</li>`).join("")
        : `<li>暂无相关错误</li>`;
    } catch (err) {
      $("#diagHint").textContent = "诊断失败：" + err.message;
    }
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    $$(".nav-item").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    $$("[data-goto]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.goto)));
    $("#btnReloadData").addEventListener("click", () => switchTab(state.tab));

    $$("#taskModeSeg .seg-item").forEach((b) => b.addEventListener("click", () => switchTaskMode(b.dataset.mode)));
    $$("#taskBizSeg .seg-item").forEach((b) => b.addEventListener("click", () => switchTaskBiz(b.dataset.biz)));
    $$("#taskSeg .seg-item").forEach((b) => b.addEventListener("click", () => {
      state.taskStatus = b.dataset.status;
      state.taskPage = 1;
      $$("#taskSeg .seg-item").forEach((x) => x.classList.toggle("active", x === b));
      renderTasks();
    }));
    $("#btnTasksReload").addEventListener("click", loadTasks);
    $("#taskSearch").addEventListener("input", () => {
      state.taskPage = 1;
      renderTasks();
    });
    $("#taskPageSize").addEventListener("change", (e) => {
      state.taskPageSize = Number(e.target.value) || 30;
      state.taskPage = 1;
      renderTasks();
    });
    $("#taskPrev").addEventListener("click", () => {
      if (state.taskPage > 1) {
        state.taskPage -= 1;
        renderTasks();
      }
    });
    $("#taskNext").addEventListener("click", () => {
      state.taskPage += 1;
      renderTasks();
    });
    $("#taskPages").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-page]");
      if (!btn) return;
      state.taskPage = Number(btn.dataset.page) || 1;
      renderTasks();
    });
    $("#taskTable").addEventListener("click", (e) => {
      const play = e.target.closest("[data-tts-play]");
      if (play) { ttsPlayModal(play.dataset.ttsPlay); return; }
      const btn = e.target.closest("[data-task]");
      if (btn) showTaskDetail(btn.dataset.task);
    });
    $("#modalClose").addEventListener("click", () => {
      $("#modal").hidden = true;
      pauseAllAudio();
    });
    $("#modal").addEventListener("click", (e) => {
      if (e.target === $("#modal")) {
        $("#modal").hidden = true;
        pauseAllAudio();
      }
    });
    $("#clipRun").addEventListener("click", runClip);

    $$("#rvcSeg .seg-item").forEach((b) => b.addEventListener("click", () => switchRvcMode(b.dataset.mode)));

    $("#btnRefreshModels").addEventListener("click", refreshModels);
    $("#btnSaveModels").addEventListener("click", saveModelsUnified);
    $("#modelRows").addEventListener("click", (e) => {
      const radio = e.target.closest('input[name="coverDefault"], input[name="ttsDefault"]');
      if (radio) return;  // 启用单选由「统一保存」提交
    });
    $("#btnSaveCfg").addEventListener("click", saveConfig);
    $("#btnReloadCfg").addEventListener("click", loadConfig);

    $("#rvcRun").addEventListener("click", runConvert);
    $("#rvcReset").addEventListener("click", () => {
      $("#rvcPitch").value = 0; $("#rvcIndexRate").value = 0.75;
      $("#rvcRms").value = 0.35; $("#rvcProtect").value = 0.33;
      $("#rvcVocalDb").value = 0; $("#rvcFade").value = 0.5;
      $("#rvcSeconds").value = 25; $("#rvcStart").value = ""; $("#rvcEnd").value = "";
      $("#rvcDuration").value = "";
      $("#rvcReverbOn").checked = false;
      $("#rvcRevWet").value = 0.18; $("#rvcRevDry").value = 1.0;
      $("#rvcRevRoom").value = 0.65; $("#rvcRevDecay").value = 1.4;
      $("#rvcRevDamp").value = 0.45; $("#rvcRevPre").value = 18;
      $("#rvcRevWidth").value = 0.8;
      $("#rvcStatus").textContent = "参数已重置";
    });
    $("#sepRun").addEventListener("click", runSeparate);
    $("#ttsRun").addEventListener("click", runTts);
    $("#btnSaveAudio").addEventListener("click", saveAudioCfg);
    $("#btnReloadAudio").addEventListener("click", loadAudioTab);
    $("#btnDiag").addEventListener("click", loadDiagnostics);

    window.addEventListener("hashchange", () => {
      const name = location.hash.replace("#", "");
      if (name && name !== state.tab) switchTab(name);
    });
  }

  const FRONTEND_VERSION = "1.0.0-r10";

  async function boot() {
    console.log("[RVC 控制台] 前端资源版本:", FRONTEND_VERSION);
    const brand = document.querySelector(".brand");
    if (brand) brand.title = "前端资源版本 " + FRONTEND_VERSION;
    bind();
    const initial = (location.hash || "#overview").replace("#", "");
    try {
      await ensureBridge();
      banner("");
    } catch (err) {
      banner(err.message);
      setStatus("#stPlugin", "err", "插件 <b>页面桥接缺失</b>");
      setStatus("#stGateway", "err", "Gateway <b>未知</b>");
    }
    const tabs = ["overview", "tasks", "rvc", "models", "settings", "diagnostics"];
    switchTab(tabs.includes(initial) ? initial : "overview");
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
