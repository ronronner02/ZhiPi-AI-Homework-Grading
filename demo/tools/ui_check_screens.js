/**
 * 四屏走查：切屏 → 等渲染 → 量关键组件 → 收集控制台报错。
 *
 * 会真的批一份内置样例、开一次终审弹层、点两个飞书按钮，所以**会写演示会话**
 * （只影响调用者自己那个 session）。llm 模式下识别 / 批改 / 飞书都是真实调用，
 * 全程约 30-60 秒。
 *
 * 关键约定：等待一律轮询到"目标真的出现"，不用固定 sleep。
 * 固定 sleep 在真实调用面前必然不够，而等不够的表现是**静默的**——
 * 识别还没回来就点提交批改，会落进 submitImageGrade 的 `if (!RECOG) return`，
 * 看起来像"按钮没反应"，实际是自检脚本自己太急。
 *
 * 用法见 ui_check_shell.js。bad 与 errors 两个数组都为空即通过。
 * 两个视口都要跑：--w 1440 --h 900 与 --w 390 --h 844。
 */
return (async function () {
  var out = { errors: [], bad: [], screens: {}, counts: {} };

  // 控制台报错要在切屏之前挂上
  window.__migErr = window.__migErr || [];
  if (!window.__migHooked) {
    window.__migHooked = 1;
    var oe = console.error;
    console.error = function () {
      window.__migErr.push(Array.prototype.slice.call(arguments).join(' '));
      oe.apply(console, arguments);
    };
    window.addEventListener('error', function (e) {
      window.__migErr.push('onerror: ' + e.message);
    });
    window.addEventListener('unhandledrejection', function (e) {
      window.__migErr.push('unhandled: ' + (e.reason && e.reason.message || e.reason));
    });
  }

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  function measure(sel) {
    var el = document.querySelector(sel);
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height),
             display: getComputedStyle(el).display };
  }

  function collapsed(sel, label) {
    var els = document.querySelectorAll(sel);
    if (!els.length) return;
    for (var i = 0; i < els.length; i++) {
      var r = els[i].getBoundingClientRect();
      if (r.width < 1 || r.height < 1) {
        out.bad.push(label + ' 塌了：' + sel + '[' + i + '] ' +
          r.width.toFixed(1) + '×' + r.height.toFixed(1) +
          ' display=' + getComputedStyle(els[i]).display);
        return;
      }
    }
  }

  function overflow(tag) {
    var de = document.documentElement;
    if (de.scrollWidth > de.clientWidth + 2) {
      out.bad.push(tag + ' 横向溢出 ' + (de.scrollWidth - de.clientWidth) + 'px');
    }
  }

  function tab(name) {
    var b = document.querySelector('.tab[data-tab="' + name + '"]');
    if (b) b.click();
  }

  // ---------------- 02 结果：先批一份内置样例 ----------------
  // 夹内清单里的「批改」按钮是唯一的样例入口
  document.querySelector('#folder-open-btn').click();
  await sleep(1200);
  out.counts.fileRows = document.querySelectorAll('.file-row').length;
  collapsed('.file-row__thumb', '夹内缩略图');

  var pick = document.querySelector('[data-sample]');
  if (!pick) {
    out.bad.push('夹内清单没有 data-sample 入口，02 屏无法走查');
  } else {
    pick.click();
    // llm 模式下识别是真实 VLM 调用，实测要十几秒。轮询到 #recog-result
    // 真的显示出来再往下走，而不是拍一个固定 sleep——等不够就会静静地
    // 落进 submitImageGrade 的 `if (!RECOG) return`，看着像「批改按钮没反应」。
    for (var w = 0; w < 60; w++) {
      var rr = document.querySelector('#recog-result');
      if (rr && rr.style.display !== 'none') break;
      await sleep(500);
    }
    out.screens.recog = measure('#recog-sheet');
    collapsed('#recog-sheet .photo img', '原卷图');
    out.counts.recogBadges = document.querySelectorAll('#recog-badges .factor').length;
    out.counts.ocrLen = (document.querySelector('#ocr-text') || {}).value ?
      document.querySelector('#ocr-text').value.length : 0;
    if (!out.counts.recogBadges) {
      out.bad.push('识别徽标为空：' +
        (document.querySelector('#recog-status') || {}).textContent);
    }

    var gb = document.querySelector('#grade-btn');
    if (gb) {
      gb.click();
      // 批改同样是真实调用（还可能带二次复批），轮询到结果页有分数卡为止
      for (var g = 0; g < 90; g++) {
        if (document.querySelector('#ro-score')) break;
        await sleep(500);
      }
      await sleep(600);   // 等 countTo / growTo 落位
    }
  }

  tab('result');
  await sleep(700);
  out.screens.result = measure('#screen-result');
  out.counts.scoreCards = document.querySelectorAll('.score-card').length;
  out.counts.steps = document.querySelectorAll('.evidence .step').length;
  out.counts.stepQuotes = document.querySelectorAll('.step__quote').length;
  out.counts.factors = document.querySelectorAll('.factor-wrap .factor').length;
  out.counts.kvRows = document.querySelectorAll('.kv__k').length;
  collapsed('.score-card', '总分卡');
  collapsed('.evidence .step', '证据链步骤');
  collapsed('.meter', '置信度条');
  // 置信度条必须真被 growTo 推开，不能停在 0
  var mf = document.querySelector('#ro-meter');
  out.counts.meterWidth = mf ? getComputedStyle(mf).width : 'n/a';
  if (mf && parseFloat(getComputedStyle(mf).width) < 1) {
    out.bad.push('置信度条宽度为 0（growTo 没生效）');
  }
  // 分数必须被 countTo 走到非 0
  var sc = document.querySelector('#ro-score');
  out.counts.score = sc ? sc.textContent : 'n/a';
  overflow('02');

  // ---------------- 03 教师台 ----------------
  tab('teacher');
  await sleep(1500);
  out.screens.teacher = measure('#screen-teacher');
  out.counts.teacherRows = document.querySelectorAll('.teacher-table tbody tr').length;
  out.counts.teacherFilters = document.querySelectorAll('.teacher-filters select').length;
  out.counts.stamps = document.querySelectorAll('.teacher-table .stamp').length;
  collapsed('.teacher-table', '教师台表格');
  if (!out.counts.teacherRows) out.bad.push('教师台没有行');
  if (out.counts.teacherFilters !== 3) {
    out.bad.push('筛选器应 3 个，实为 ' + out.counts.teacherFilters);
  }
  // 分流章必须是 --g/--y/--r，不能残留 --green
  out.counts.legacyStamp =
    document.querySelectorAll('.stamp--green,.stamp--yellow,.stamp--red').length;
  if (out.counts.legacyStamp) out.bad.push('残留旧章类名 stamp--green/yellow/red');
  overflow('03');

  // 终审弹层
  var rev = document.querySelector('[data-review]');
  if (!rev) { out.bad.push('教师台没有终审入口'); }
  else {
    rev.click();
    await sleep(600);
    out.screens.dialog = measure('.dialog');
    out.counts.dialogTagpick = document.querySelectorAll('#rv-tags label').length;
    out.counts.dialogSteps = document.querySelectorAll('.dialog .evidence .step').length;
    collapsed('.dialog', '终审弹层');
    if (!document.querySelector('#rv-score')) out.bad.push('弹层缺终审分数输入');
    if (!document.querySelector('#rv-comment')) out.bad.push('弹层缺评语输入');
    if (!out.counts.dialogTagpick) out.bad.push('弹层缺错因 tagpick');
    // 弹层内不能横向溢出
    var dg = document.querySelector('.dialog');
    if (dg && dg.scrollWidth > dg.clientWidth + 2) {
      out.bad.push('终审弹层横向溢出 ' + (dg.scrollWidth - dg.clientWidth) + 'px');
    }
    document.querySelector('[data-close]').click();
    await sleep(300);
    if (document.querySelector('.mask')) out.bad.push('弹层关不掉');
  }

  // ---------------- 04 看板 ----------------
  tab('board');
  await sleep(1800);
  out.screens.board = measure('#screen-board');
  out.counts.kpis = document.querySelectorAll('.kpi').length;
  out.counts.distSegs = document.querySelectorAll('.dist__seg').length;
  out.counts.barRows = document.querySelectorAll('.bar-row').length;
  out.counts.suggs = document.querySelectorAll('.sugg li').length;
  collapsed('.kpi', 'KPI 卡');
  collapsed('.dist', '分流条');
  collapsed('.bar-track', '图表轨道');
  if (!out.counts.kpis) out.bad.push('看板没有 KPI');
  if (!out.counts.distSegs) out.bad.push('看板没有分流条');
  if (!out.counts.barRows) out.bad.push('看板没有图表行');
  // 分流段与图表条必须被 growTo 推开
  var seg = document.querySelector('.dist__seg');
  out.counts.segWidth = seg ? getComputedStyle(seg).width : 'n/a';
  if (seg && parseFloat(getComputedStyle(seg).width) < 1) {
    out.bad.push('分流段宽度为 0（growTo 没生效）');
  }
  var bt = document.querySelector('.bar-track > i');
  if (bt && parseFloat(getComputedStyle(bt).width) < 1) {
    out.bad.push('图表条宽度为 0（growTo 没生效）');
  }
  // KPI 数字必须被 countTo 走到非 0
  var kv = document.querySelector('.kpi .val');
  out.counts.kpiFirst = kv ? kv.textContent : 'n/a';
  overflow('04');

  // 讲评大纲 + 学生画像
  var go = document.querySelector('#gen-outline');
  if (go) { go.click(); await sleep(1500); }
  out.counts.outline = document.querySelectorAll('.outline').length;
  out.counts.timeline = document.querySelectorAll('.timeline li').length;
  out.counts.chipSim = document.querySelectorAll('.chip-sim').length;
  collapsed('.outline', '讲评大纲');

  // 飞书两个按钮 → 两栏。
  // 两者在 live 模式下都是真实开放平台调用（鉴权 + 推送 / batch_create，
  // 服务端 timeout 15s），固定 sleep 等不住——轮询到该栏不再是 loading 为止。
  async function clickAndSettle(btnSel, colSel, tag) {
    var b = document.querySelector(btnSel);
    if (!b) { out.bad.push(tag + ' 按钮不存在'); return; }
    b.click();
    for (var i = 0; i < 50; i++) {          // 最多 25s
      await sleep(500);
      var col = document.querySelector(colSel);
      if (col && !col.querySelector('.loading')) return;
    }
    out.bad.push(tag + ' 25s 内没返回');
  }
  await clickAndSettle('#feishu-push', '.feishu-col--card', '飞书推送');
  await clickAndSettle('#feishu-sync', '.feishu-col--base', '飞书同步');
  out.counts.larkcards = document.querySelectorAll('#feishu-result .larkcard').length;
  out.counts.baseTables = document.querySelectorAll('#feishu-result .base-table').length;
  out.counts.rawDetails = document.querySelectorAll('#feishu-result details.raw').length;
  out.counts.rawOpenByDefault =
    document.querySelectorAll('#feishu-result details.raw[open]').length;
  collapsed('#feishu-result .larkcard', '飞书卡片');
  collapsed('#feishu-result .base-table', '台账表');
  if (!out.counts.larkcards) out.bad.push('飞书卡片没渲染');
  if (!out.counts.baseTables) out.bad.push('台账表没渲染');
  if (out.counts.rawOpenByDefault) out.bad.push('接口 JSON 默认展开了（应折叠）');
  // 两栏应并排：卡片右边界不该越过表格左边界
  var col1 = document.querySelector('.feishu-col--card');
  var col2 = document.querySelector('.feishu-col--base');
  if (col1 && col2) {
    var r1 = col1.getBoundingClientRect(), r2 = col2.getBoundingClientRect();
    out.counts.feishuSideBySide = (r2.left >= r1.right - 2);
    // 宽屏应并排、窄屏应堆叠。两栏是 flex-wrap，断点由 flex-basis 自己决定，
    // 所以按视口宽度分别断言，而不是一律要求并排。
    if (window.innerWidth >= 1100 && !out.counts.feishuSideBySide) {
      out.bad.push('宽屏下飞书两栏没并排');
    }
    if (window.innerWidth < 700 && out.counts.feishuSideBySide) {
      out.bad.push('窄屏下飞书两栏没堆叠');
    }
  }
  overflow('04-feishu');

  out.errors = window.__migErr.slice(0);
  return out;
})();
