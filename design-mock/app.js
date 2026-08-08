(function () {
  var TRAIL = {
    submit:  { t: "01 提交", d: "选择文件夹并上传作业" },
    result:  { t: "02 结果", d: "查看证据链与评语" },
    teacher: { t: "03 教师台", d: "确认、改判与终审" },
    board:   { t: "04 看板", d: "学情、讲评与飞书协同" }
  };

  var REVIEW_META = {
    wang: { name: "王同学", subject: "数学 · 方程应用", score: "72", conf: "68.0",
      ocr: "设甲为 x kg，乙为 (20−x) kg。列得 3x+2(20−x)=46 …" },
    li: { name: "李同学", subject: "物理 · 力学", score: "41", conf: "41.0",
      ocr: "F = ma，取 m=2kg，a 书写不清…" },
    zhao: { name: "赵同学", subject: "数学 · 代数", score: "74", conf: "74.0",
      ocr: "整理得 2x − 5 = 11，x = 8（系数疑似笔误）" },
    chen: { name: "陈同学", subject: "英语 · 阅读", score: "93", conf: "93.0",
      ocr: "Main idea: the passage argues that…" },
    zhou: { name: "周同学", subject: "数学 · 方程应用", score: "88", conf: "91.0",
      ocr: "设甲为 x … 检验：3×6+2×14=46，成立。" }
  };

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function showView(name) {
    $$("[data-view]").forEach(function (el) {
      var on = el.getAttribute("data-view") === name;
      el.hidden = !on;
      el.classList.toggle("is-on", on);
    });
    window.scrollTo(0, 0);
  }

  function showTab(name) {
    showView("app");
    $$(".tab").forEach(function (t) {
      t.classList.toggle("is-on", t.getAttribute("data-tab") === name);
    });
    $$("[data-screen]").forEach(function (s) {
      var on = s.getAttribute("data-screen") === name;
      s.hidden = !on;
      s.classList.toggle("is-on", on);
    });
    var meta = TRAIL[name] || TRAIL.submit;
    var title = $("#trail-title");
    var desc = $("#trail-desc");
    if (title) title.textContent = meta.t;
    if (desc) desc.textContent = meta.d;
    if (history.replaceState) history.replaceState(null, "", "#" + name);
    else location.hash = name;
  }

  function go(target) {
    if (!target) return;
    if (target === "home" || target === "stage" || target === "hero") {
      showView("stage");
      if (history.replaceState) history.replaceState(null, "", "#hero");
      return;
    }
    if (target === "gate") {
      showView("gate");
      if (history.replaceState) history.replaceState(null, "", "#gate");
      return;
    }
    if (target === "states") {
      showView("states");
      return;
    }
    if (TRAIL[target]) {
      showTab(target);
      return;
    }
    if (target === "app") showTab("submit");
  }

  function showToast(title, body) {
    var toast = $("#toast");
    if (!toast) return;
    var b = toast.querySelector("b");
    var span = toast.querySelector("span");
    if (b && title) b.textContent = title;
    if (span && body) span.textContent = body;
    toast.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { toast.hidden = true; }, 2200);
  }

  function setActive(nodes, el) {
    nodes.forEach(function (n) { n.classList.remove("is-on"); });
    if (el) el.classList.add("is-on");
  }

  function setActiveFolder(name) {
    name = (name || "").trim();
    if (!name) return;
    var af = $("#active-folder");
    if (af) af.textContent = name;
    $$("#screen-submit .sheet").forEach(function (sheet) {
      var h3 = sheet.querySelector("h3");
      if (!h3) return;
      var title = h3.textContent || "";
      if (title.indexOf("夹内") !== -1) {
        var hi = sheet.querySelector(".hint-inline");
        if (hi) hi.textContent = name;
      }
      if (title.indexOf("上传") !== -1) {
        var sub = sheet.querySelector(".sub");
        if (sub) {
          sub.innerHTML =
            "目标：<strong>" + name + "</strong> · 单张先确认转写，多张进入批量流水线";
        }
      }
    });
  }

  function filterTeacherRows() {
    var st = ($("select[data-f='status']") || {}).value || "all";
    var subj = ($("select[data-f='subject']") || {}).value || "all";
    var tag = ($("select[data-f='tag']") || {}).value || "all";
    var rows = $$("#teacher-tbody tr");
    var n = 0;
    rows.forEach(function (tr) {
      var rs = tr.getAttribute("data-status") || "";
      var reviewed = tr.getAttribute("data-reviewed") === "1";
      var rSubj = tr.getAttribute("data-subject") || "";
      var rTag = tr.getAttribute("data-tag") || "";
      var ok = true;
      if (st === "pending") ok = !reviewed;
      else if (st === "reviewed") ok = reviewed;
      else if (st === "green" || st === "yellow" || st === "red") ok = rs === st;
      if (ok && subj !== "all") ok = rSubj === subj;
      if (ok && tag !== "all") ok = rTag.indexOf(tag) !== -1;
      tr.hidden = !ok;
      if (ok) n += 1;
    });
    var count = $("#teacher-count");
    if (count) count.textContent = String(n);
  }

  function openReview(id) {
    var meta = REVIEW_META[id] || REVIEW_META.wang;
    var title = $("#rv-title");
    var summary = $("#rv-summary");
    var ocr = $("#rv-ocr");
    var score = $("#rv-score");
    if (title) title.textContent = "教师终审 · " + meta.name;
    if (summary) {
      summary.innerHTML =
        meta.subject + " ｜ AI 判分 <b class=\"mono\">" + meta.score +
        " / 100</b>，置信度 <b class=\"mono\">" + meta.conf + "</b>";
    }
    if (ocr) ocr.textContent = meta.ocr;
    if (score) score.value = meta.score;
    var mask = $("#review-mask");
    if (mask) mask.hidden = false;
  }

  function closeReview() {
    var mask = $("#review-mask");
    if (mask) mask.hidden = true;
  }

  function showTeacherNote(msg) {
    var el = $("#teacher-note");
    if (!el) return;
    el.hidden = false;
    el.textContent = msg;
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.hidden = true; }, 4000);
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest(
      "[data-go], [data-tab], #back-home, #reset-btn, #rv-submit," +
      ".folder, .chip, #feishu-push, #feishu-sync," +
      "#gen-outline, #copy-outline, [data-confirm], [data-review]," +
      "[data-close-review], #review-mask"
    );
    if (!t) return;

    if (t.id === "review-mask") {
      closeReview();
      return;
    }
    if (t.hasAttribute("data-close-review")) {
      e.preventDefault();
      closeReview();
      return;
    }

    if (t.id === "back-home" || t.id === "reset-btn") {
      e.preventDefault();
      closeReview();
      go("home");
      return;
    }
    if (t.hasAttribute("data-tab")) {
      e.preventDefault();
      closeReview();
      showTab(t.getAttribute("data-tab"));
      return;
    }
    if (t.hasAttribute("data-go")) {
      e.preventDefault();
      closeReview();
      go(t.getAttribute("data-go"));
      return;
    }

    if (t.classList.contains("folder")) {
      setActive($$(".folder-grid .folder"), t);
      var nm = t.querySelector(".name");
      if (nm) setActiveFolder(nm.textContent);
      return;
    }

    if (t.classList.contains("chip")) {
      var parent = t.parentElement;
      if (parent) setActive($$(".chip", parent), t);
      return;
    }

    if (t.hasAttribute("data-review")) {
      e.preventDefault();
      openReview(t.getAttribute("data-review"));
      return;
    }

    if (t.id === "rv-submit") {
      e.preventDefault();
      closeReview();
      showToast("已终审", "记录已写入，置信因子已回灌");
      showTeacherNote("终审已记录。该题教师通过率因子已回灌，同题未终审作答的置信度将按新因子重算。");
      return;
    }
    if (t.hasAttribute("data-confirm")) {
      e.preventDefault();
      showToast("已确认", "已记入终审");
      showTeacherNote("已确认通过。通过率因子已回灌。");
      return;
    }
    if (t.id === "feishu-push") {
      e.preventDefault();
      showToast("已推送", "审核提醒卡片已发送");
      return;
    }
    if (t.id === "feishu-sync") {
      e.preventDefault();
      showToast("已同步", "学情台账已写入多维表格");
      return;
    }
    if (t.id === "gen-outline") {
      e.preventDefault();
      showToast("已生成", "讲评大纲已更新");
      return;
    }
    if (t.id === "copy-outline") {
      e.preventDefault();
      var outline = $(".outline");
      var text = outline ? outline.textContent.trim() : "";
      if (navigator.clipboard && text) {
        navigator.clipboard.writeText(text).catch(function () {});
      }
      showToast("已复制", "可粘贴到备课文档");
    }
  });

  // 阻止弹层内容点击冒泡关闭
  var dialog = $(".dialog");
  if (dialog) {
    dialog.addEventListener("click", function (e) { e.stopPropagation(); });
  }

  $$("select[data-f]").forEach(function (sel) {
    sel.addEventListener("change", filterTeacherRows);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeReview();
  });

  var code = $("#code");
  if (code) {
    code.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        go("submit");
      }
    });
  }

  // tagpick 切换
  document.addEventListener("change", function (e) {
    var lab = e.target.closest(".tagpick label");
    if (!lab || !e.target.matches("input[type=checkbox]")) return;
    lab.classList.toggle("is-on", e.target.checked);
  });

  function fromHash() {
    var h = (location.hash || "").replace(/^#/, "");
    if (!h || h === "hero") showView("stage");
    else go(h);
  }
  window.addEventListener("hashchange", fromHash);
  fromHash();
  filterTeacherRows();
})();
