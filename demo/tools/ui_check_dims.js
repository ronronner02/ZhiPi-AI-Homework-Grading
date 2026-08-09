/**
 * 判别分体系自检：题库外作业的结果页口径 + 教师表判别标记 + 逐维度改分。
 *
 * 为什么要真的跑一遍而不看源码：本项目有一类 bug 看源码看不出来——选择器
 * 命中的元素 display:inline 会把 width 静默丢掉，盒子塌成 0×0，源码每行都
 * "对"，页面上什么都没有。所以这里一律量 computed style 与真实可点性。
 *
 * 会真实上传一张题库外的照片（真实 VLM + 真实批改，约 40-90 秒），因此
 * **会写演示会话**（只影响调用者自己那个 session）。
 *
 * 用法（工作目录 demo/）：
 *   node -e "const{readFileSync}=require('fs');const e=readFileSync('tools/ui_check_dims.js','utf8');
 *            require('child_process').spawnSync('node',['tools/probe.mjs','--eval',e,'--url','http://127.0.0.1:8024/'],{stdio:'inherit'})"
 *
 * 等待一律轮询到「目标真的出现」，不用固定 sleep：固定 sleep 在真实模型调用
 * 面前必然不够，而等不够的表现是静默的（识别没回来就点批改会落进
 * submitImageGrade 的 `if (!RECOG) return`，看起来像"按钮没反应"）。
 *
 * bad 与 errors 两个数组都为空即通过。
 */
return (async function () {
  var out = { errors: [], bad: [], ok: [], info: {} };

  window.__dimErr = window.__dimErr || [];
  if (!window.__dimHooked) {
    window.__dimHooked = 1;
    var oe = console.error;
    console.error = function () {
      window.__dimErr.push(Array.prototype.slice.call(arguments).join(' '));
      oe.apply(console, arguments);
    };
    window.addEventListener('error', function (e) {
      window.__dimErr.push('onerror: ' + e.message);
    });
    window.addEventListener('unhandledrejection', function (e) {
      window.__dimErr.push('unhandled: ' + (e.reason && e.reason.message || e.reason));
    });
  }

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  // 轮询到条件成立；超时返回 false 而不是抛，让后续检查继续跑完
  async function until(fn, ms, label) {
    var t0 = Date.now();
    while (Date.now() - t0 < ms) {
      var v;
      try { v = fn(); } catch (e) { v = false; }
      if (v) return v;
      await sleep(250);
    }
    out.bad.push('等超时(' + Math.round(ms / 1000) + 's): ' + label);
    return false;
  }

  function vis(sel, label) {
    var el = document.querySelector(sel);
    if (!el) { out.bad.push(label + ' 找不到 ' + sel); return null; }
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    if (r.width < 1 || r.height < 1) {
      out.bad.push(label + ' 盒子塌了 ' + sel + ' → ' + r.width.toFixed(1) + '×' +
        r.height.toFixed(1) + ' display=' + cs.display);
    } else if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') {
      out.bad.push(label + ' 不可见 ' + sel + ' display=' + cs.display +
        ' visibility=' + cs.visibility + ' opacity=' + cs.opacity);
    } else {
      out.ok.push(label + ' ' + Math.round(r.width) + '×' + Math.round(r.height));
    }
    return el;
  }

  // 真实可点性：b.click() 会绕过遮挡层，量不出"按钮被别的卡片盖住"
  async function clickable(el, label) {
    // 找不到元素也要记进 bad：静默 return false 会让整段检查提前结束，
    // 表现成"什么都没查出来"，比报错更难发现。
    if (!el) { out.bad.push(label + ' 元素不存在'); return false; }
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) { out.bad.push(label + ' 盒子塌了，点不到'); return false; }
    // 一律滚到视口中间再量，并等滚动落定：页面用的是 smooth 滚动，
    // 滚完立刻取坐标会拿到滚动过程中的位置，elementFromPoint 命中 null，
    // 看起来像"按钮被遮挡"，其实是量得太早。
    el.scrollIntoView({ block: 'center', behavior: 'auto' });
    await sleep(250);
    r = el.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) {
      out.bad.push(label + ' 滚动后仍在视口外 cx=' + Math.round(cx) + ' cy=' + Math.round(cy));
      return false;
    }
    var top = document.elementFromPoint(cx, cy);
    if (!top) { out.bad.push(label + ' 命中点无元素'); return false; }
    if (top !== el && !el.contains(top) && !top.contains(el)) {
      out.bad.push(label + ' 被遮挡：命中 ' + top.tagName + '.' +
        (top.className || '').toString().slice(0, 40));
      return false;
    }
    out.ok.push(label + ' 可点');
    return true;
  }

  // ---- 1. 进应用，上传一张题库外的照片 ----
  var enter = document.querySelector('#enter-app, [data-enter], .hero .btn--primary');
  if (enter) { enter.click(); await sleep(600); }

  var TAB = function (name) {
    var t = document.querySelector('[data-tab="' + name + '"]');
    if (t) { t.click(); return true; }
    return false;
  };

  TAB('upload');
  await sleep(400);

  // 造一张题库外的题图：内容不重要，重要的是不命中内置样例。
  // 用 canvas 画印刷题干 + 手写作答，转 blob 走真实 VLM。
  var cv = document.createElement('canvas');
  cv.width = 900; cv.height = 620;
  var g = cv.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, cv.width, cv.height);
  g.fillStyle = '#111'; g.font = '26px serif';
  g.fillText('三、(12分) 已知函数 f(x) = x^3 - 3x + 1。', 40, 70);
  g.fillText('(1) 求 f(x) 的单调区间；', 60, 115);
  g.fillText('(2) 求 f(x) 在 [-2, 2] 上的最大值与最小值。', 60, 155);
  g.font = '25px cursive';
  g.fillStyle = '#1a3a8f';
  g.fillText('解：f\'(x) = 3x^2 - 3 = 3(x-1)(x+1)', 60, 230);
  g.fillText('令 f\'(x) = 0 得 x = 1 或 x = -1', 60, 275);
  g.fillText('故 f 在 (-∞,-1) 递增, (-1,1) 递减, (1,+∞) 递增', 60, 320);
  g.fillText('f(-2) = -1, f(-1) = 3, f(1) = -1, f(2) = 3', 60, 365);
  g.fillText('所以最大值为 3, 最小值为 -1', 60, 410);

  var blob = await new Promise(function (r) { cv.toBlob(r, 'image/png'); });
  var file = new File([blob], 'openq.png', { type: 'image/png' });
  var input = document.querySelector('input[type="file"]');
  if (!input) { out.bad.push('找不到文件输入框'); return out; }
  var dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));

  // ---- 2. 等识别，检查题库外作业的徽标 ----
  var recogOk = await until(function () {
    var rr = document.querySelector('#recog-result');
    return rr && rr.style.display !== 'none' &&
      document.querySelector('#ocr-text') &&
      document.querySelector('#ocr-text').value.length > 0;
  }, 120000, '识别返回');

  if (recogOk) {
    var badges = document.querySelector('#recog-badges');
    var bt = badges ? badges.textContent : '';
    out.info.badges = bt.replace(/\s+/g, ' ').trim();
    if (bt.indexOf('学科归类') < 0 && bt.indexOf('自动判题') < 0 &&
        bt.indexOf('命中内置样例') < 0) {
      out.bad.push('识别徽标没显示学科归类/自动判题：' + out.info.badges);
    }
    if (bt.indexOf('学科归类') >= 0 && bt.indexOf('体系判别分') < 0) {
      out.bad.push('题库外作业没标出判分口径「体系判别分」');
    }
    vis('#recog-badges', '识别徽标');
  }

  // ---- 3. 提交批改（这一步以前会卡死在「未能判定题目」） ----
  var gradeBtn = document.querySelector('#grade-btn');
  if (!(await clickable(gradeBtn, '提交批改按钮'))) {
    out.info.gradeHint = (document.querySelector('#grade-hint') || {}).textContent || '';
    return out;
  }
  gradeBtn.click();

  var gradeOk = await until(function () {
    var rb = document.querySelector('#result-body');
    return rb && rb.textContent.indexOf('置信') >= 0 &&
      document.querySelector('#ro-score');
  }, 180000, '批改返回结果页');

  var hint = document.querySelector('#grade-hint');
  if (hint && hint.textContent.trim()) out.info.gradeHint = hint.textContent.trim();

  // ---- 4. 结果页：判别分口径 ----
  if (gradeOk) {
    var rb = document.querySelector('#result-body');
    var txt = rb.textContent;
    out.info.scoreLab = (function () {
      var c = rb.querySelector('.score-card .lab');
      return c ? c.textContent.trim() : '(无)';
    })();
    out.info.score = (document.querySelector('#ro-score') || {}).textContent;

    if (txt.indexOf('体系判别分') >= 0) {
      out.ok.push('结果页已标注体系判别分');
      vis('.score-card__note', '判别分口径说明');
      if (txt.indexOf('非试卷分值') < 0) out.bad.push('未说明「非试卷分值」');
      if (txt.indexOf('判分基准') >= 0) {
        out.ok.push('结果页显示判分基准');
        vis('.basis-warn', '基准未经人工确认警示');
      } else {
        out.bad.push('题库外作业没显示「判分基准」行');
      }
    } else if (txt.indexOf('总分') >= 0) {
      out.info.note = '这份命中了题库内题目，判别分口径不适用';
    } else {
      out.bad.push('结果页既无「体系判别分」也无「总分」');
    }

    var naChip = rb.querySelector('.factor--na');
    out.info.naChip = naChip ? naChip.textContent.replace(/\s+/g, ' ').trim() : null;
    if (naChip && naChip.textContent.indexOf('null') >= 0) {
      out.bad.push('因子块显示了 null');
    }
  }

  // ---- 5. 教师工作台：判别标记 + 逐维度改分 ----
  TAB('teacher');
  var rowsOk = await until(function () {
    return document.querySelectorAll('.teacher-table tbody tr').length > 0;
  }, 60000, '教师表格出行');

  if (rowsOk) {
    var mark = document.querySelector('.basis-mark');
    out.info.basisMark = mark ? mark.textContent.trim() : null;
    if (mark) vis('.basis-mark', '判别标记');

    // 开终审弹层，量逐维度改分格子。
    // 必须挑**判别分那一行**：默认第一行是内置样例（题库内题目），
    // 它本来就没有维度，拿它验会得出"逐维改分不适用"的假通过。
    var rvBtn = null;
    var trs = document.querySelectorAll('.teacher-table tbody tr');
    for (var ti = 0; ti < trs.length; ti++) {
      if (trs[ti].querySelector('.basis-mark')) {
        rvBtn = trs[ti].querySelector('[data-review]');
        if (rvBtn) break;
      }
    }
    if (!rvBtn) {
      out.bad.push('教师表里找不到带「判别」标记的行，无法验逐维改分');
      rvBtn = document.querySelector('[data-review]');
    }
    if (await clickable(rvBtn, '终审按钮')) {
      rvBtn.click();
      var dlgOk = await until(function () {
        return document.querySelector('#rv-score');
      }, 20000, '终审弹层打开');

      if (dlgOk) {
        var grid = document.querySelector('#rv-dims');
        if (grid) {
          vis('#rv-dims', '逐维度改分区');
          var cells = grid.querySelectorAll('input[data-dim]');
          out.info.dimCount = cells.length;
          if (cells.length !== 5) out.bad.push('维度格子数应为 5，实际 ' + cells.length);
          for (var i = 0; i < cells.length; i++) {
            vis('#rv-dims input[data-dim="' + cells[i].dataset.dim + '"]',
                '维度输入 ' + cells[i].dataset.dim);
            await clickable(cells[i], '维度输入 ' + cells[i].dataset.dim);
          }
          // 总分应随维度自动求和
          var scoreEl = document.querySelector('#rv-score');
          var before = Number(scoreEl.value);
          var first = cells[0];
          var oldV = Number(first.value);
          var newV = (oldV > 0) ? oldV - 1 : Number(first.max);
          first.value = newV;
          first.dispatchEvent(new Event('input', { bubbles: true }));
          await sleep(150);
          var after = Number(scoreEl.value);
          out.info.sumBefore = before;
          out.info.sumAfter = after;
          out.info.readonly = scoreEl.readOnly;
          if (after === before) {
            out.bad.push('改维度分后总分没跟着变（' + before + ' → ' + after + '）');
          } else if (Math.abs((after - before) - (newV - oldV)) > 0.001) {
            out.bad.push('总分变化量不等于维度变化量：Δ总分=' +
              (after - before) + ' Δ维度=' + (newV - oldV));
          } else {
            out.ok.push('总分自动求和正确 ' + before + ' → ' + after);
          }
          if (!scoreEl.readOnly) out.bad.push('有维度格子时总分输入框应 readonly');

          // 越界应标红
          first.value = Number(first.max) + 5;
          first.dispatchEvent(new Event('input', { bubbles: true }));
          await sleep(150);
          if (!first.classList.contains('is-bad')) {
            out.bad.push('维度分越界未标红');
          } else {
            out.ok.push('维度分越界已标红');
          }
        } else {
          out.info.noDimGrid = '该行无 dimension，逐维改分不适用（题库内题目）';
        }
        var close = document.querySelector('[data-close]');
        if (close) close.click();
      }
    }
  }

  out.errors = (window.__dimErr || []).slice(0);
  return out;
})();
