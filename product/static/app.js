(() => {
  "use strict";
  const state = { config: null, rows: [], questions: [], analytics: null, filter: "all", search: "", selected: null, errorTags: [] };
  const $ = (id) => document.getElementById(id);

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    let body = null;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (response.status === 401 && path !== "/api/auth/login") showLogin();
    if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`);
    return body;
  }

  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.classList.remove("hidden");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => node.classList.add("hidden"), 3200);
  }

  function showLogin() { $("loginOverlay").classList.remove("hidden"); }
  function hideLogin() { $("loginOverlay").classList.add("hidden"); $("loginError").classList.add("hidden"); }

  function metric(label, value, unit = "") {
    const card = document.createElement("div"); card.className = "metric";
    const p = document.createElement("p"); p.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    card.append(p, strong);
    if (unit) { const small = document.createElement("small"); small.textContent = unit; card.append(small); }
    return card;
  }

  function statusText(status) { return ({ red: "人工优先", yellow: "需要确认", green: "可抽查" })[status] || "待处理"; }

  function renderMetrics() {
    const graded = state.rows.filter((row) => row.result);
    const counts = { red: 0, yellow: 0, green: 0 };
    graded.forEach((row) => { counts[row.status] = (counts[row.status] || 0) + 1; });
    const reviewed = graded.filter((row) => row.reviewed).length;
    const container = $("workbenchMetrics"); container.replaceChildren(
      metric("已进入批改", graded.length, "份"),
      metric("人工优先", counts.red, "份"),
      metric("需要确认", counts.yellow, "份"),
      metric("终审进度", graded.length ? Math.round(reviewed / graded.length * 100) : 0, "%")
    );
  }

  function renderTable() {
    const query = state.search.toLowerCase();
    const rows = state.rows.filter((row) => {
      if (!row.result) return state.filter === "all";
      if (state.filter !== "all" && row.status !== state.filter) return false;
      const haystack = `${row.student_name} ${row.subject} ${row.question_title}`.toLowerCase();
      return !query || haystack.includes(query);
    });
    const body = $("resultsBody"); body.replaceChildren();
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      const student = document.createElement("td"); student.className = "student-cell";
      const name = document.createElement("strong"); name.textContent = row.student_name;
      const title = document.createElement("span"); title.textContent = row.question_title || "待补充题目";
      student.append(name, title);
      const subject = document.createElement("td"); subject.textContent = row.subject || "—";
      const scoreTd = document.createElement("td"); scoreTd.className = "score";
      scoreTd.textContent = row.result ? `${row.total_score}` : "—";
      if (row.result) { const max = document.createElement("small"); max.textContent = ` / ${row.max_score}`; scoreTd.append(max); }
      const confidenceTd = document.createElement("td");
      if (row.result) { const wrap = document.createElement("span"); wrap.className = "confidence"; const dot = document.createElement("i"); dot.className = `dot ${row.status}`; wrap.append(dot, document.createTextNode(`${row.confidence}%`)); confidenceTd.append(wrap); }
      else confidenceTd.textContent = "待转写";
      const reviewTd = document.createElement("td"); reviewTd.className = `review-state ${row.reviewed ? "done" : ""}`; reviewTd.textContent = row.reviewed ? `已终审 · ${row.final_score} 分` : "未终审";
      const actionTd = document.createElement("td");
      const button = document.createElement("button"); button.className = "row-button"; button.textContent = row.result ? (row.reviewed ? "查看 / 修改" : "教师终审") : "补充转写";
      button.addEventListener("click", () => row.result ? openReview(row) : recognizeOrTranscribe(row)); actionTd.append(button);
      tr.append(student, subject, scoreTd, confidenceTd, reviewTd, actionTd); body.append(tr);
    });
    $("resultCount").textContent = `显示 ${rows.length} / ${state.rows.length} 份`;
    $("tableEmpty").classList.toggle("hidden", rows.length > 0);
  }

  function renderQuestions() {
    const select = $("questionSelect"); select.replaceChildren();
    const custom = document.createElement("option"); custom.value = ""; custom.textContent = "自定义题目"; select.append(custom);
    state.questions.forEach((question) => { const option = document.createElement("option"); option.value = question.question_id; option.textContent = `${question.subject} · ${question.title}`; select.append(option); });
    if (state.questions.length) select.value = state.questions[0].question_id;
    $("customQuestion").classList.toggle("hidden", Boolean(select.value));
  }

  function renderAnalytics() {
    const data = state.analytics; if (!data) return;
    $("analyticsMetrics").replaceChildren(
      metric("参与学生", data.student_count, "人"), metric("平均得分率", data.average_score_pct, "%"),
      metric("红色人工队列", data.distribution.red, "份"), metric("教师终审进度", data.review_progress_pct, "%")
    );
    const weak = $("weakPoints"); weak.replaceChildren();
    (data.weak_knowledge_points || []).slice(0, 8).forEach((item) => {
      const row = document.createElement("div"); row.className = "bar-row";
      const label = document.createElement("span"); label.textContent = item.name;
      const track = document.createElement("div"); track.className = "bar-track"; const fill = document.createElement("div"); fill.className = "bar-fill"; fill.style.width = `${Math.min(100, item.error_rate)}%`; track.append(fill);
      const value = document.createElement("strong"); value.textContent = `${item.error_rate}%`; row.append(label, track, value); weak.append(row);
    });
    if (!weak.children.length) { const empty = document.createElement("p"); empty.textContent = "暂无足够数据"; empty.className = "empty"; weak.append(empty); }
    const tags = $("errorTags"); tags.replaceChildren();
    (data.error_tag_distribution || []).forEach((item) => { const chip = document.createElement("span"); chip.className = "tag-chip"; chip.append(document.createTextNode(item.name)); const count = document.createElement("strong"); count.textContent = item.count; chip.append(count); tags.append(chip); });
    if (!tags.children.length) { const empty = document.createElement("p"); empty.textContent = "暂无集中错因"; empty.className = "empty"; tags.append(empty); }
    const suggestions = $("suggestions"); suggestions.replaceChildren(); (data.teaching_suggestions || []).forEach((text) => { const li = document.createElement("li"); li.textContent = text; suggestions.append(li); });
  }

  function switchView(name) {
    document.querySelectorAll(".view").forEach((node) => node.classList.toggle("active", node.id === `${name}View`));
    document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
    $("pageTitle").textContent = ({ workbench: "教师工作台", upload: "提交与批改", analytics: "班级学情" })[name];
    if (name === "analytics") refreshAnalytics().catch((error) => toast(error.message));
  }

  function openReview(row) {
    state.selected = row;
    $("reviewTitle").textContent = `${row.student_name} · ${row.question_title}`;
    $("reviewTranscript").textContent = row.ocr_text || "暂无转写文本";
    const image = $("reviewImage"); image.classList.toggle("hidden", !row.file_url); if (row.file_url) image.src = row.file_url;
    $("aiScore").value = `${row.total_score} / ${row.max_score}`;
    $("finalScore").max = row.max_score; $("finalScore").value = row.review ? row.review.final_score : row.total_score;
    $("finalComment").value = row.review ? row.review.final_comment || "" : row.result.student_feedback || "";
    const selectedTags = new Set(row.review && row.review.final_error_tags !== null ? row.review.final_error_tags : row.result.error_tags || []);
    const options = $("errorTagOptions"); options.replaceChildren(); state.errorTags.forEach((tag) => { const label = document.createElement("label"); label.className = "tag-option"; const input = document.createElement("input"); input.type = "checkbox"; input.value = tag; input.checked = selectedTags.has(tag); const span = document.createElement("span"); span.textContent = tag; label.append(input, span); options.append(label); });
    const evidence = $("evidenceList"); evidence.replaceChildren(); (row.result.step_analysis || []).forEach((step) => { const item = document.createElement("div"); item.className = "evidence-item"; const strong = document.createElement("strong"); strong.textContent = `${step.step} · ${step.score}/${step.max_score}`; const p = document.createElement("p"); p.textContent = step.evidence ? `“${step.evidence}” — ${step.reason}` : step.reason; item.append(strong, p); evidence.append(item); });
    $("reviewDialog").showModal();
  }

  async function refreshAnalytics() { state.analytics = await api("/api/analytics/class"); renderAnalytics(); }

  async function recognizeOrTranscribe(row) {
    try {
      let transcript = "";
      if (state.config.vlm_enabled) {
        toast("正在识别作业原图…");
        const recognized = await api("/api/recognize", { method: "POST", body: JSON.stringify({ submission_id: row.submission_id }) });
        transcript = window.prompt("请核对多模态识别文本，确认无误后再批改：", recognized.text) || "";
        if (!transcript.trim()) return;
      } else {
        transcript = window.prompt("未配置多模态模型。请粘贴或核对学生作答转写：", row.ocr_text || "") || "";
        if (!transcript.trim()) return;
      }
      await api("/api/grade", { method: "POST", body: JSON.stringify({ submission_id: row.submission_id, transcript }) });
      await refreshAll();
      toast(state.config.llm_enabled ? "识别与批改已完成" : "已保存转写并转入人工终审");
    } catch (error) { toast(error.message); }
  }

  async function refreshAll() {
    const [config, questions, teacher] = await Promise.all([api("/api/config"), api("/api/questions"), api("/api/teacher/results")]);
    state.config = config; state.questions = questions.questions || []; state.rows = teacher.results || []; state.errorTags = teacher.error_tags_enum || [];
    $("modeLabel").textContent = config.llm_enabled ? "真实模型批改" : "安全演示 / 人工兜底";
    $("persistLabel").textContent = "SQLite + 文件持久化已启用"; $("workspaceLabel").textContent = `工作区 ${config.workspace_id}`;
    $("logoutButton").classList.toggle("hidden", !config.access_code_required);
    renderMetrics(); renderTable(); renderQuestions(); await refreshAnalytics();
  }

  async function fileToBase64(file) {
    return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",", 2)[1]); reader.onerror = () => reject(new Error("无法读取本地图片")); reader.readAsDataURL(file); });
  }

  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll(".filter").forEach((node) => node.classList.remove("active")); button.classList.add("active"); state.filter = button.dataset.status; renderTable(); }));
  $("searchInput").addEventListener("input", (event) => { state.search = event.target.value.trim(); renderTable(); });
  $("questionSelect").addEventListener("change", (event) => $("customQuestion").classList.toggle("hidden", Boolean(event.target.value)));
  $("refreshButton").addEventListener("click", () => refreshAll().then(() => toast("数据已刷新")).catch((error) => toast(error.message)));

  $("uploadForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = $("uploadSubmit"); const file = form.elements.file.files[0];
    if (!file) return; if (file.size > 8 * 1024 * 1024) { toast("图片超过 8 MB，请压缩后重试"); return; }
    button.disabled = true; button.textContent = "正在安全上传…";
    try {
      const payload = { filename: file.name, content_type: file.type || "application/octet-stream", data_base64: await fileToBase64(file), student_name: form.elements.student_name.value, question_id: form.elements.question_id.value || null, question_text: form.elements.question_text ? form.elements.question_text.value || null : null, subject: form.elements.subject ? form.elements.subject.value || null : null, transcript: form.elements.transcript.value || null, folder_id: "inbox" };
      const result = await api("/api/submissions/upload", { method: "POST", body: JSON.stringify(payload) });
      const panel = $("uploadResult"); panel.textContent = result.state === "awaiting_transcription" ? `已归档 ${result.submission_id}，请稍后补充转写并批改。` : (result.state === "manual_review" ? `已归档 ${result.submission_id}；未配置模型，已进入人工终审队列。` : `已完成批改：${result.result.total_score}/${result.result.max_score} 分。`); panel.classList.remove("hidden");
      await refreshAll(); toast("作业已安全保存");
    } catch (error) { toast(error.message); } finally { button.disabled = false; button.textContent = "上传并进入批改"; }
  });

  $("reviewForm").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!state.selected) return;
    const tags = Array.from($("errorTagOptions").querySelectorAll("input:checked")).map((input) => input.value);
    try { await api("/api/teacher/review", { method: "POST", body: JSON.stringify({ submission_id: state.selected.submission_id, teacher_action: Number($("finalScore").value) === Number(state.selected.total_score) ? "confirmed" : "modified", final_score: Number($("finalScore").value), final_error_tags: tags, final_comment: $("finalComment").value }) }); $("reviewDialog").close(); await refreshAll(); toast("教师终审已持久化"); } catch (error) { toast(error.message); }
  });
  $("closeReview").addEventListener("click", () => $("reviewDialog").close()); $("cancelReview").addEventListener("click", () => $("reviewDialog").close());
  $("outlineButton").addEventListener("click", async () => { try { const data = await api("/api/lecture-outline"); await navigator.clipboard.writeText(data.content); toast("讲评提纲已复制"); } catch (error) { toast(error.message); } });
  $("loginForm").addEventListener("submit", async (event) => { event.preventDefault(); const errorNode = $("loginError"); try { await api("/api/auth/login", { method: "POST", body: JSON.stringify({ access_code: event.currentTarget.elements.access_code.value }) }); hideLogin(); await refreshAll(); } catch (error) { errorNode.textContent = error.message; errorNode.classList.remove("hidden"); } });
  $("logoutButton").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); showLogin(); } catch (error) { toast(error.message); } });

  (async () => {
    try { const auth = await api("/api/auth/status"); if (auth.required && !auth.authenticated) { showLogin(); return; } await refreshAll(); }
    catch (error) { toast(error.message); }
  })();
})();
