/*!
 * 智批π · 前端业务逻辑
 *
 * 依赖：icons.js（内联 Lucide 图标）、motion.js（弹簧动效）。三者均为本地文件，
 * 全站零外部请求，断网可完整演示。
 *
 * 界面约定：不使用任何表情符号；所有语义标记走 Icons.icon()。
 */
(function () {
  'use strict';

  var I = window.Icons.icon;
  var M = window.Motion;

  /* ======================================================================
     0. 基础工具
     ====================================================================== */

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  /** 统一请求：把后端的 detail 文案提出来，让错误提示对体验者有意义。 */
  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (r.ok) return r.json();
      return r.text().then(function (body) {
        var msg = body;
        try {
          var j = JSON.parse(body);
          msg = j.detail || body;
          if (Array.isArray(msg)) msg = msg.map(function (d) { return d.msg || ''; }).join('；');
        } catch (e) { /* 非 JSON 错误体，原样透出 */ }
        if (r.status === 401) {
          // 访问口令过期：回首页重新验证，避免体验者面对一堆 401
          location.reload();
        }
        var err = new Error(msg || ('请求失败 ' + r.status));
        err.status = r.status;
        throw err;
      });
    });
  }

  function spinner(text) {
    return '<div class="loading"><span class="spin">' +
      I('loader', { size: 16 }) + '</span>' + esc(text || '处理中') + '</div>';
  }

  function emptyBox(text, iconName, actionHtml) {
    /* DESIGN §7.2 composed empty：图标 + 说明；可选 CTA HTML */
    return '<div class="empty">' +
      '<div class="empty__ico">' + I(iconName || 'info', { size: 36 }) + '</div>' +
      '<p class="empty__desc">' + esc(text) + '</p>' +
      (actionHtml ? '<div class="empty__actions">' + actionHtml + '</div>' : '') +
      '</div>';
  }

  /* ======================================================================
     1. 常量
     ====================================================================== */

  var STATUS_TEXT = { green: '自动通过', yellow: '教师确认', red: '转人工' };
  var STATUS_VAR = { green: 'var(--riso-green)', yellow: 'var(--riso-amber)', red: 'var(--riso-red)' };
  var ENGINE_TEXT = { vlm: '多模态大模型识别', 'sample-match': '离线演示识别 · 样例匹配' };

  var FACTOR_LABEL = {
    ocr_clarity: 'OCR 识别清晰度',
    answer_match: '答案匹配程度',
    rubric_coverage: 'Rubric 覆盖度',
    llm_self_consistency: 'LLM 自检一致性',
    teacher_pass_rate: '历史教师通过率'
  };
  // 权重来自设计方案 §9.7，展示出来让评委看到置信度是算的不是编的
  var FACTOR_WEIGHT = {
    ocr_clarity: 0.25, answer_match: 0.25, rubric_coverage: 0.20,
    llm_self_consistency: 0.20, teacher_pass_rate: 0.10
  };

  var TRAIL = [
    { tab: 'submit', n: '01', t: '选文件夹 / 上传', tip: '自建文件夹、指定上传目标；也可打开 Demo 样例夹点内置照片。整夹可一键批改并自动飞书提醒。' },
    { tab: 'result', n: '02', t: '过程级批改', tip: '每一步判分都附「引用学生原文」的证据链，右侧是置信度五因子明细。' },
    { tab: 'teacher', n: '03', t: '教师终审', tip: '改一份的分数或错因——提交后，同题其他作答的置信度会按教师的通过率实时变化。' },
    { tab: 'board', n: '04', t: '班级学情', tip: '薄弱点、错因分布与讲评课件大纲，根据批改结果实时聚合。' }
  ];

  /* ======================================================================
     2. 状态
     ====================================================================== */

  var CONFIG = {};
  var CURRENT = null;      // 当前展示的批改结果
  var RECOG = null;        // 最近一次识别结果
  var TEACHER_ROWS = [];
  var TAG_ENUM = [];
  var OUTLINE_MD = '';
  var TRAIL_AT = 0;
  var ACTIVE = 'submit';
  var FOLDERS = [];        // 文件夹列表摘要
  var ACTIVE_FOLDER = 'demo';
  var SAMPLE_IMAGES = [];  // 内置样例（打开 Demo 夹时展示）

  /* ======================================================================
     3. 叙事首屏
     ====================================================================== */

  function buildCollage(images) {
    var box = $('#collage');
    if (!box || !images || !images.length) return;
    // 挑三张不同学科的照片，视觉上更像一叠真实作业而不是同一题
    var picked = [], seen = {};
    images.forEach(function (im) {
      if (picked.length < 3 && !seen[im.subject]) { seen[im.subject] = 1; picked.push(im); }
    });
    while (picked.length < 3 && images.length >= 3) picked.push(images[picked.length]);
    var speeds = [-0.045, 0.03, -0.015];
    var rots = [-5.5, 4, -1.5];
    box.innerHTML = picked.map(function (im, i) {
      return '<figure data-parallax="' + speeds[i] + '" data-parallax-rotate="' + rots[i] + '">' +
        '<img src="' + esc(im.url) + '" alt="" loading="lazy"></figure>';
    }).join('');
    M.parallax(box);
  }

  function fillHeroStats(dist) {
    if (!dist) return;
    ['green', 'yellow', 'red'].forEach(function (k, i) {
      var el = $('[data-stat="' + k + '"]');
      if (!el) return;
      setTimeout(function () { M.countTo(el, dist[k] || 0, { digits: 0 }); }, 220 + i * 130);
    });
  }

  function enterApp() {
    M.pageTurn($('#stage'), $('#app'), function () {
      $('#stage').style.display = 'none';
      $('#app').classList.add('is-on');
      window.scrollTo(0, 0);
      switchTab('submit');
      // 到这里吸顶栏才真正可见、才有高度可量。init() 里那次量的是隐藏状态，
      // 拿到 0 会被守卫挡掉，只能落在 CSS 兜底值上。
      syncMastheadHeight();
    });
  }

  function backHome() {
    M.pageTurn($('#app'), $('#stage'), function () {
      $('#app').classList.remove('is-on');
      $('#stage').style.display = '';
      window.scrollTo(0, 0);
    });
  }

  /* ======================================================================
     4. 外壳：页签、引导、说明、重置
     ====================================================================== */

  function switchTab(name) {
    var changed = (ACTIVE !== name);
    ACTIVE = name;
    ['submit', 'result', 'teacher', 'board'].forEach(function (t) {
      $('#screen-' + t).classList.toggle('is-on', t === name);
    });
    $$('.tab').forEach(function (b) {
      b.classList.toggle('is-on', b.dataset.tab === name);
    });
    // 换屏时回到顶部。不做的话，批量上传十几张后页面停在长列表的底部，
    // 点「查看批改结果」会直接落在新页面的中段——顶部的批次切换下拉与
    // 总分/置信度全在视口外，看起来像是「什么都没显示」。
    // 只在真的换了屏时滚，避免同屏内重渲染（切下拉）也把页面拽走。
    if (changed) window.scrollTo(0, 0);
    renderTrail();
    INTRO_OPEN = (name === 'submit');
    renderIntro();
    if (name === 'result' && BATCH_LIST) {
      // 从批量页过来：CURRENT 已在最后一份批改成功时被赋值，但页面上什么都没有——
      // 因为渲染只发生在 selectBatchResult 里，而它被下面的「已匹配」判断跳过了。
      // 所以这里必须无条件渲染一次：CURRENT 有值不等于它被画出来了。
      var match = false;
      for (var i = 0; i < BATCH_LIST.length; i++) {
        if (BATCH_LIST[i].result === CURRENT) { match = true; break; }
      }
      if (match) {
        renderResult();
      } else {
        for (var j = 0; j < BATCH_LIST.length; j++) {
          if (BATCH_LIST[j].status === 'ok') { selectBatchResult(j, BATCH_LIST); break; }
        }
      }
    }
    if (name === 'teacher') loadTeacher();
    if (name === 'board') loadBoard();
    Icons.hydrate();
  }

  function renderTrail() {
    var idx = Math.max(0, TRAIL.findIndex(function (s) { return s.tab === ACTIVE; }));
    TRAIL_AT = Math.max(TRAIL_AT, idx);
    $('#trail').innerHTML = TRAIL.map(function (s, i) {
      var cls = (s.tab === ACTIVE) ? 'is-on' : (i < TRAIL_AT ? 'is-done' : '');
      return '<button type="button" class="trail__step ' + cls + '" data-goto="' + s.tab + '">' +
        '<span class="trail__n">' + s.n + '</span>' +
        '<span class="trail__t">' + esc(s.t) + '</span></button>';
    }).join('');
    $('#trail-tip').textContent = TRAIL[idx].tip;
  }

  // 说明块在 01 屏完整展开（陌生人第一眼落在这里），02–04 屏收成一行。
  // 同一段文字在四屏各占 130px 高，会把真正要看的内容整体压到首屏以下。
  var INTRO_OPEN = true;

  function renderIntro() {
    if (localStorage.getItem('zhipi_intro_hidden') === '1') {
      $('#intro-slot').innerHTML = '';
      return;
    }
    var mock = CONFIG.mode !== 'llm';

    if (!INTRO_OPEN) {
      $('#intro-slot').innerHTML =
        '<div class="note note--amber note--thin" id="intro-note">' +
        '<span class="note__ico">' + I('info', { size: 16 }) + '</span>' +
        '<div><b>' + (mock ? '离线演示模式' : '真实大模型模式') + '</b>' +
        '<span class="note__sep">·</span>样例图为合成仿手写，历史趋势为模拟数据' +
        '<span class="note__sep">·</span>操作只影响你自己</div>' +
        '<button class="note__more" type="button" id="intro-more">展开说明</button>' +
        '<button class="note__close" type="button" id="intro-close" title="不再显示">' +
        I('x', { size: 16 }) + '</button></div>';
      return;
    }

    var body =
      '<b>这是什么。</b>面向 K12 多学科作业的「教师可控 AI 批改与错因诊断」原型。' +
      '普通 AI 批改回答「这道题对不对」，它回答的是「错在哪一步、老师敢不敢信、下节课怎么讲」。<br>' +
      '<b>当前为' + (mock ? '离线演示模式。' : '真实大模型模式。') + '</b>' +
      (mock
        ? '批改走内置规则引擎，识别走内置样例照片匹配，全程不联网、不消耗任何 API；' +
          '上传你自己的照片需要服务端配置多模态大模型密钥。'
        : '识别与批改都会真实调用大模型，可上传任意手写作业照片。') +
      '<br><b>数据说明。</b>内置 11 份作答的样例图为程序合成的仿手写，学生画像中的历史趋势为模拟数据' +
      '（界面已标注），真实手写照片的实测数据正在采集中。你的操作只影响你自己这一次体验，' +
      '随时可点右上角「重置演示」。';
    $('#intro-slot').innerHTML =
      '<div class="note note--amber" id="intro-note">' +
      '<span class="note__ico">' + I('info', { size: 18 }) + '</span>' +
      '<div>' + body + '</div>' +
      '<button class="note__close" type="button" id="intro-close" title="不再显示">' +
      I('x', { size: 16 }) + '</button></div>';
  }

  function resetDemo() {
    if (!confirm('重置演示会清空你的教师终审记录、上传作业和自建文件夹，回到初始状态。\n（只影响你自己，不会影响其他正在体验的人）')) return;
    var btn = $('#reset-btn');
    btn.disabled = true;
    api('/api/demo/reset', { method: 'POST' }).then(function () {
      CURRENT = null; RECOG = null; TRAIL_AT = 0; OUTLINE_MD = '';
      FOLDERS = []; ACTIVE_FOLDER = 'demo';
      $('#recog-sheet').style.display = 'none';
      $('#batch-sheet').style.display = 'none';
      $('#folder-detail-sheet').style.display = 'none';
      $('#folder-feishu-note').style.display = 'none';
      $('#result-body').innerHTML = emptyBox('请先在「01 拍照提交」中选择样例或上传照片，识别后点击「提交批改」。');
      markPlate(null);        // 清掉夹内清单上的选中标记
      loadFolders();
      switchTab('submit');
    }).catch(function (e) {
      alert('重置失败：' + e.message);
    }).then(function () { btn.disabled = false; });
  }

  /* ======================================================================
     5. Step 01：文件夹、图库、上传、压缩、识别
     ====================================================================== */

  function activeFolderMeta() {
    for (var i = 0; i < FOLDERS.length; i++) {
      if (FOLDERS[i].folder_id === ACTIVE_FOLDER) return FOLDERS[i];
    }
    return null;
  }

  function updateUploadTargetHint() {
    var meta = activeFolderMeta();
    var name = meta ? meta.name : 'Demo 样例';
    var el = $('#folder-active-hint');
    if (el) el.textContent = '当前上传目标：' + name;
    var t = $('#upload-target-hint');
    if (t) t.innerHTML = '上传目标：<b>' + esc(name) + '</b>（点上方文件夹可切换）';
    var actions = $('#folder-actions');
    if (actions) actions.style.display = meta ? '' : 'none';
    var ren = $('#folder-rename-btn');
    var del = $('#folder-delete-btn');
    if (ren) ren.style.display = (meta && meta.kind !== 'demo') ? '' : 'none';
    if (del) del.style.display = (meta && meta.kind !== 'demo') ? '' : 'none';
  }

  function renderFolders() {
    var grid = $('#folder-grid');
    if (!grid) return;
    if (!FOLDERS.length) {
      grid.innerHTML = emptyBox('暂无文件夹', 'folder');
      updateUploadTargetHint();
      return;
    }
    grid.innerHTML = FOLDERS.map(function (f) {
      var count = f.count || 0;
      // 夹内纸张最多画 3 张：再多也看不出差别，份数由角标给准数
      var papers = '';
      var paperN = Math.max(0, Math.min(3, f.stack || count));
      for (var i = 1; i <= paperN; i++) {
        papers += '<span class="folder-card__paper folder-card__paper--' + i + '"></span>';
      }

      var cls = 'folder-card';
      if (f.kind === 'demo') cls += ' folder-card--demo';
      if (!count) cls += ' folder-card--empty';
      if (f.folder_id === ACTIVE_FOLDER) cls += ' is-on';

      var meta = f.kind === 'demo' ? '系统夹 · ' + count + ' 份' : count + ' 份作业';
      // 无障碍：夹体纯装饰，语义全部交给这一句，读屏不会听到一串空 span
      var label = f.name + '，' + meta +
        (f.folder_id === ACTIVE_FOLDER ? '，当前上传目标' : '');

      return '<button type="button" class="' + cls +
        '" data-folder="' + esc(f.folder_id) + '"' +
        ' aria-pressed="' + (f.folder_id === ACTIVE_FOLDER) + '"' +
        ' aria-label="' + esc(label) + '" title="' + esc(f.name) + '">' +
        '<span class="folder-card__vis" aria-hidden="true">' +
          '<span class="folder-card__back"></span>' +
          papers +
          '<span class="folder-card__front"></span>' +
          (count ? '<span class="folder-card__badge">' + count + '</span>' : '') +
          '<span class="folder-card__now">当前</span>' +
        '</span>' +
        '<span class="folder-card__name">' + esc(f.name) + '</span>' +
        '<span class="folder-card__meta">' + esc(meta) + '</span>' +
        '</button>';
    }).join('');
    updateUploadTargetHint();
  }

  function loadFolders() {
    return api('/api/folders').then(function (d) {
      FOLDERS = d.folders || [];
      ACTIVE_FOLDER = d.active_folder_id || 'demo';
      renderFolders();
    }).catch(function (e) {
      var grid = $('#folder-grid');
      if (grid) grid.innerHTML = emptyBox('文件夹加载失败：' + e.message, 'alert');
    });
  }

  function selectFolder(fid) {
    if (!fid || fid === ACTIVE_FOLDER) {
      // 再点一次仍刷新动作条
      ACTIVE_FOLDER = fid || ACTIVE_FOLDER;
      renderFolders();
      return;
    }
    api('/api/folders/active', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_id: fid })
    }).then(function (d) {
      ACTIVE_FOLDER = d.active_folder_id || fid;
      FOLDERS.forEach(function (f) { f.is_active = (f.folder_id === ACTIVE_FOLDER); });
      renderFolders();
      $('#folder-detail-sheet').style.display = 'none';
      $('#folder-feishu-note').style.display = 'none';
    }).catch(function (e) {
      alert('切换文件夹失败：' + e.message);
    });
  }

  function createFolder() {
    var input = $('#folder-name-input');
    var name = (input && input.value || '').trim();
    if (!name) { alert('请输入文件夹名称'); return; }
    api('/api/folders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    }).then(function (d) {
      FOLDERS = d.folders || [];
      ACTIVE_FOLDER = d.active_folder_id || ACTIVE_FOLDER;
      if (input) input.value = '';
      renderFolders();
    }).catch(function (e) {
      alert('新建失败：' + e.message);
    });
  }

  function renameFolder() {
    var meta = activeFolderMeta();
    if (!meta || meta.kind === 'demo') return;
    var name = prompt('重命名文件夹', meta.name);
    if (name == null) return;
    name = String(name).trim();
    if (!name || name === meta.name) return;
    api('/api/folders/' + encodeURIComponent(meta.folder_id), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    }).then(function (d) {
      FOLDERS = d.folders || FOLDERS;
      renderFolders();
    }).catch(function (e) {
      alert('重命名失败：' + e.message);
    });
  }

  function deleteFolder() {
    var meta = activeFolderMeta();
    if (!meta || meta.kind === 'demo') return;
    if (!confirm('删除文件夹「' + meta.name + '」？\n夹内已批改的上传件仍保留在工作台，只是不再归属此夹。')) return;
    api('/api/folders/' + encodeURIComponent(meta.folder_id), {
      method: 'DELETE'
    }).then(function (d) {
      FOLDERS = d.folders || [];
      ACTIVE_FOLDER = d.active_folder_id || 'demo';
      $('#folder-detail-sheet').style.display = 'none';
      renderFolders();
    }).catch(function (e) {
      alert('删除失败：' + e.message);
    });
  }

  function openFolderDetail() {
    var meta = activeFolderMeta();
    if (!meta) return;
    var sheet = $('#folder-detail-sheet');
    var body = $('#folder-detail-body');
    $('#folder-detail-title').textContent = meta.name + ' · 夹内文件';
    sheet.style.display = '';
    body.innerHTML = spinner('载入夹内文件');
    Icons.hydrate(body);
    api('/api/folders/' + encodeURIComponent(meta.folder_id)).then(function (d) {
      var items = d.items || [];
      if (!items.length) {
        body.innerHTML = emptyBox('这个文件夹还是空的。上传照片会进入当前选中夹。', 'folder');
        Icons.hydrate(body);
        return;
      }
      body.innerHTML = '<div style="display:grid;gap:8px;">' + items.map(function (it) {
        var thumb = it.url
          ? '<span class="folder-item__thumb"><img src="' + esc(it.url) + '" alt=""></span>'
          : '<span class="folder-item__thumb" style="display:flex;align-items:center;justify-content:center;color:var(--ink-soft);">' +
            I('fileText', { size: 18 }) + '</span>';
        var st = it.status ? (STATUS_TEXT[it.status] || it.status) : (it.graded ? '已批改' : '未批改');
        var score = (it.score != null && it.max_score != null)
          ? (it.score + '/' + it.max_score + ' · ') : '';
        var action = (it.kind === 'sample' && it.url)
          ? '<button class="btn btn--ghost btn--sm" type="button" data-sample="' +
            esc(it.item_id) + '" data-url="' + esc(it.url) + '">批改</button>'
          : (it.kind === 'upload'
            ? '<span class="tag tag--mine">已入工作台</span>' : '');
        return '<div class="folder-item">' + thumb +
          '<div class="folder-item__body">' +
          '<div class="folder-item__name">' + esc(it.student_name || it.item_id) + '</div>' +
          '<div class="folder-item__meta">' + esc((it.subject || '') +
            (it.question_title ? ' · ' + it.question_title : '') +
            ' · ' + score + st) + '</div></div>' + action + '</div>';
      }).join('') + '</div>';
      Icons.hydrate(body);
      sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });
    }).catch(function (e) {
      body.innerHTML = emptyBox('加载失败：' + e.message, 'alert');
      Icons.hydrate(body);
    });
  }

  function gradeFolder() {
    var meta = activeFolderMeta();
    if (!meta) return;
    var hint = $('#folder-grade-hint');
    var note = $('#folder-feishu-note');
    var btn = $('#folder-grade-btn');
    if (btn) btn.disabled = true;
    if (hint) hint.innerHTML = '<span class="spin">' + I('loader', { size: 14 }) + '</span> 整夹批改中…';
    if (note) { note.style.display = 'none'; note.innerHTML = ''; }
    Icons.hydrate(hint);
    api('/api/folders/' + encodeURIComponent(meta.folder_id) + '/grade', {
      method: 'POST'
    }).then(function (d) {
      if (hint) {
        var dist = d.distribution || {};
        hint.innerHTML = '完成 <b>' + d.graded_count + '</b> 份 · 绿 ' +
          (dist.green || 0) + ' / 黄 ' + (dist.yellow || 0) + ' / 红 ' + (dist.red || 0);
      }
      // 自动飞书审核提醒：后端已推；这里展示结果
      var fs = d.feishu || {};
      if (note) {
        note.style.display = '';
        var modeBanner = banner(
          fs.mode,
          '飞书未配置或推送降级：以下为将推送的审核提醒卡片',
          '已自动推送飞书审核提醒卡片'
        );
        var card = fs.card && fs.card.card;
        var cardHtml = '';
        if (card) {
          var header = (card.header && card.header.title) ? card.header.title.content : '飞书互动卡片';
          var parts = (card.elements || []).map(function (el) {
            if (el.tag === 'div' && el.text) return '<div style="margin:6px 0;">' + larkMd(el.text.content) + '</div>';
            if (el.tag === 'hr') return '<hr>';
            if (el.tag === 'action' && el.actions) {
              var a = el.actions[0];
              return '<div class="mt-4"><span class="btn btn--sm" style="pointer-events:none;">' +
                esc(a.text.content) + I('arrowRight', { size: 13 }) + '</span></div>';
            }
            if (el.tag === 'note' && el.elements) return '<p class="hint mt-4">' + esc(el.elements[0].content) + '</p>';
            return '';
          }).join('');
          cardHtml = '<div class="larkcard mt-4"><div class="larkcard__head">' + esc(header) +
            '</div><div class="larkcard__body">' + parts + '</div></div>';
        }
        note.innerHTML = modeBanner +
          '<p class="hint">接口消息：' + esc(fs.message || '') + '</p>' + cardHtml +
          '<div class="inline mt-4">' +
          '<button class="btn" type="button" data-goto="teacher">' +
          I('table', { size: 15 }) + '<span>去工作台审核</span></button>' +
          '<button class="btn btn--ghost" type="button" data-goto="board">' +
          '<span>看班级看板</span></button></div>';
        Icons.hydrate(note);
      }
      loadFolders();
      if (meta.kind === 'demo') openFolderDetail();
    }).catch(function (e) {
      if (hint) hint.textContent = '整夹批改失败：' + e.message;
    }).then(function () {
      if (btn) btn.disabled = false;
    });
  }

  /** 批改完成后静默尝试推飞书审核卡片（失败不打断主流程）。 */
  function autoPushFeishu(assignmentName) {
    if (!(CONFIG && CONFIG.feishu_webhook_configured)) return;
    api('/api/feishu/push', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        assignment_name: assignmentName || undefined,
        folder_id: ACTIVE_FOLDER || undefined
      })
    }).then(function (d) {
      // 只在 live 成功时轻提示；demo 降级不打扰
      if (d && d.mode === 'live') {
        var box = $('#folder-feishu-note');
        if (!box) return;
        box.style.display = '';
        box.innerHTML =
          '<div class="note note--green"><span class="note__ico">' +
          I('send', { size: 16 }) + '</span><div>已自动推送飞书审核提醒卡片' +
          (assignmentName ? '（' + esc(assignmentName) + '）' : '') +
          '。</div></div>';
        Icons.hydrate(box);
      }
    }).catch(function () { /* 自动推送失败静默 */ });
  }

  /* 批改链路清单：如实写出这次批改由几个模型参与、哪级复核在跑。
     两级复核（同模型二次复批 / 跨模型交叉验证）都按设计静默降级，
     所以「配齐并生效」和「配了但每次超时」在界面上本来毫无区别——
     这份清单就是为了让这两种状态一眼可分。 */
  function renderChain() {
    var el = $('#chain-list');
    if (!el) return;
    var c = CONFIG.chain;
    if (!c) { el.innerHTML = ''; return; }

    var rows = [];
    // 第一级：主批改。mock 模式下没有后两级可谈，单独说清楚。
    if (CONFIG.mode !== 'llm') {
      rows.push({ on: false, k: '主批改',
                  v: 'mock 规则引擎（未配置大模型凭据，分数由规则算出）' });
      el.innerHTML = rows.map(chainRow).join('');
      return;
    }
    rows.push({ on: true, k: '主批改', v: '大模型逐步批改，每份必跑' });

    rows.push(c.double_check
      ? { on: true,  k: '二次复批',
          v: '同模型再批一遍，两次吻合度作为置信度因子（每份多等约 3 秒）' }
      : { on: false, k: '二次复批',
          v: '未启用 · 置信度里的「自检一致性」退回模型自报值（ZHIPI_DOUBLE_CHECK=1 开启）' });

    if (!c.cross_check) {
      rows.push({ on: false, k: '交叉验证',
                  v: '未启用 · 填入第二模型的链接与密钥即自动启用，用于跨模型复核黄/红件' });
    } else if (c.cross_budget_tight) {
      // 这条是硬警告：预算小于最快一次成功耗时，等于每次必然超时后静默丢弃。
      rows.push({ on: false, k: '交叉验证',
                  v: c.cross_model + ' · 预算仅 ' + c.cross_timeout +
                     ' 秒，实测第二模型多需 25-90 秒，几乎必然超时后被丢弃（调大 ZHIPI_CROSS_TIMEOUT）' });
    } else {
      rows.push({ on: true, k: '交叉验证',
                  v: c.cross_model + ' 独立复核，仅黄/红件触发（这类件多等约 ' +
                     c.cross_timeout + ' 秒内）' });
    }
    el.innerHTML = rows.map(chainRow).join('');
    Icons.hydrate(el);
  }

  function chainRow(r) {
    return '<li class="chain__item' + (r.on ? '' : ' chain__item--off') + '">' +
      '<span class="chain__dot" aria-hidden="true"></span>' +
      '<span class="chain__k">' + esc(r.k) + '</span>' +
      '<span class="chain__v">' + esc(r.v) + '</span>' +
      '<span class="sr-only">（' + (r.on ? '已启用' : '未启用') + '）</span></li>';
  }

  function renderGallery(data) {
    $('#engine-hint').innerHTML = data.vlm_configured
      ? '识别引擎：<b>多模态大模型</b>（已配置密钥，可识别任意手写作业照片）'
      : '识别引擎：<b>离线演示识别</b>（未配置多模态密钥，通过感知哈希匹配内置样例照片）';

    var mb = (CONFIG.guard && CONFIG.guard.max_image_mb) || 8;
    $('#upload-hint').innerHTML = data.vlm_configured
      ? '支持 PNG / JPG / WebP，超过 1MB 会自动压缩后上传（原图 &#8804; ' + mb + 'MB）'
      : '当前为离线演示模式，<b>只能识别内置样例照片</b>；识别任意照片需服务端配置多模态密钥';

    // 样例照片只在「查看夹内文件」里列出，01b 不再平铺一遍。
    // 之前两处都渲染，同一批 11 张在一屏里出现两次，翻页时尤其明显。
    // SAMPLE_IMAGES 仍要留着——首屏拼贴（buildCollage）用的是它。
    SAMPLE_IMAGES = data.images || [];
    buildCollage(SAMPLE_IMAGES);
    updateUploadTargetHint();
  }

  /* 标记当前选中的样例。
     样例平铺图库撤掉后，承载 data-sample 的是夹内清单里的「批改」按钮，
     所以标记打在它所属的那一行上，而不再找 .plate。 */
  function markPlate(sid) {
    $$('[data-sample]').forEach(function (b) {
      var row = (b.closest && b.closest('.folder-item')) || b;
      row.classList.toggle('is-on', !!sid && b.dataset.sample === sid);
    });
  }

  function blobToB64(blob) {
    return new Promise(function (res, rej) {
      var fr = new FileReader();
      fr.onload = function () { res(fr.result.split(',')[1]); };
      fr.onerror = rej;
      fr.readAsDataURL(blob);
    });
  }

  /* 上传前压缩：手机原图常 4-8MB，base64 后再涨 1/3，弱网必卡。
     小于阈值的图片原样上传——离线样例匹配会先比对 SHA-256，重编码会破坏精确命中。 */
  var COMPRESS_SKIP = 1200 * 1024;
  var MAX_EDGE = 1600;

  function compressImage(file) {
    if (file.size <= COMPRESS_SKIP) return Promise.resolve(file);
    var load = window.createImageBitmap
      // from-image：iPhone 竖拍照片带 EXIF 旋转，不声明会被摆正成横的
      ? createImageBitmap(file, { imageOrientation: 'from-image' })
      : new Promise(function (res, rej) {
          var img = new Image();
          img.onload = function () { res(img); };
          img.onerror = rej;
          img.src = URL.createObjectURL(file);
        });
    return load.then(function (bmp) {
      var w0 = bmp.width || bmp.naturalWidth, h0 = bmp.height || bmp.naturalHeight;
      if (!w0 || !h0) return file;
      var k = Math.min(1, MAX_EDGE / Math.max(w0, h0));
      var w = Math.round(w0 * k), h = Math.round(h0 * k);
      var cv = document.createElement('canvas');
      cv.width = w; cv.height = h;
      var ctx = cv.getContext('2d');
      ctx.fillStyle = '#fff';        // PNG 透明底转 JPEG 会变黑，先铺白
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(bmp, 0, 0, w, h);
      if (bmp.close) bmp.close();
      return new Promise(function (res) {
        cv.toBlob(function (blob) {
          res(blob && blob.size < file.size ? blob : file);   // 没变小就别帮倒忙
        }, 'image/jpeg', 0.85);
      });
    }).catch(function () { return file; });   // 压缩失败不阻断，交服务端体积校验兜底
  }

  function selectSample(sid, url) {
    markPlate(sid);
    $('#chosen-img').src = url;
    fetch(url).then(function (r) { return r.blob(); }).then(function (blob) {
      return blobToB64(blob).then(function (b64) {
        return recognize(b64, blob.type || 'image/png', '');
      });
    }).catch(function (e) {
      showRecogError('读取样例图片失败：' + e.message);
    });
  }

  function onFileChosen(ev) {
    var files = Array.prototype.slice.call(ev.target.files || []);
    ev.target.value = '';                      // 先清空，选同一个文件也能再次触发
    if (!files.length) return;
    if (files.length === 1) { handleFile(files[0]); return; }
    runBatch(files);
  }

  function handleFile(file) {
    var maxMB = (CONFIG.guard && CONFIG.guard.max_image_mb) || 8;
    markPlate(null);
    var sheet = $('#recog-sheet');
    sheet.style.display = '';
    $('#recog-result').style.display = 'none';
    $('#recog-status').innerHTML = spinner('正在处理图片');
    sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });

    compressImage(file).then(function (blob) {
      if (blob.size > maxMB * 1024 * 1024) {
        showRecogError('压缩后仍有 ' + (blob.size / 1048576).toFixed(1) + 'MB，超过 ' +
          maxMB + 'MB 上限。请用相册的「编辑 → 缩小」或截图后重试。');
        return;
      }
      $('#chosen-img').src = URL.createObjectURL(blob);
      var note = (blob !== file)
        ? '已自动压缩 ' + (file.size / 1048576).toFixed(1) + 'MB → ' + (blob.size / 1048576).toFixed(1) + 'MB'
        : '';
      return blobToB64(blob).then(function (b64) {
        return recognize(b64, blob.type || file.type || 'image/png', note);
      });
    });
  }

  function showRecogError(msg) {
    $('#recog-status').innerHTML =
      '<div class="note note--red"><span class="note__ico">' + I('alert', { size: 18 }) +
      '</span><div>' + esc(msg) + '</div></div>';
  }

  /* ======================================================================
     5b. 批量流水线：多选照片 → 串行识别 + 批改 → 进度 → 汇总
     ====================================================================== */

  var BATCH_MAX = 20;   // 公开演示单次上限，超限提示拆批
  var BATCH_TOKEN = 0;  // 批次令牌：重入时作废旧批次，防并行交叉
  var BATCH_LIST = null; // 当前批次列表，renderBatchDone/selectBatchResult 共用

  function runBatch(files) {
    if (files.length > BATCH_MAX) {
      alert('单次最多 ' + BATCH_MAX + ' 张，已选 ' + files.length +
        ' 张。请拆成多次上传。');
      return;
    }
    var token = ++BATCH_TOKEN;
    var list = files.map(function (f) {
      return { file: f, name: f.name, status: 'queued', text: '待处理' };
    });
    var sheet = $('#batch-sheet');
    sheet.style.display = '';
    $('#recog-sheet').style.display = 'none';
    $('#batch-done').style.display = 'none';
    sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });
    renderBatch(list);
    processBatch(list, 0, token);
  }

  function renderBatch(list) {
    var done = list.filter(function (it) { return it.status === 'ok' || it.status === 'fail'; }).length;
    var pct = Math.round(done / list.length * 100);
    $('#batch-progress').innerHTML =
      '已处理 <b>' + done + ' / ' + list.length + '</b> 张';
    $('#batch-bar-fill').style.width = pct + '%';
    $('#batch-list').innerHTML = list.map(function (it) {
      var cls = 'factor';
      if (it.status === 'ok') cls = 'factor factor--ok';
      if (it.status === 'fail') cls = 'factor factor--bad';
      var detail = it.html
        ? '<span class="hint" style="flex:1;">' + it.html + '</span>'
        : '<span class="hint">' + esc(it.text) + '</span>';
      return '<div style="display:flex;align-items:center;gap:10px;' +
        'padding:8px 10px;border:1px solid var(--rule);border-radius:10px;">' +
        '<span class="' + cls + '" style="min-width:170px;"><b>' +
        esc(it.name) + '</b></span>' +
        detail +
        (it.status === 'ok' ? '<span class="tag tag--mine">已入工作台</span>' : '') +
        '</div>';
    }).join('');
    if (done === list.length) renderBatchDone(list);
  }

  function renderBatchDone(list) {
    BATCH_LIST = list;
    var ok = list.filter(function (it) { return it.status === 'ok'; }).length;
    var fail = list.length - ok;
    var box = $('#batch-done');
    box.style.display = '';
    box.innerHTML =
      '<div class="note ' + (fail ? 'note--amber' : 'note--green') + '">' +
      '<span class="note__ico">' + I(fail ? 'alert' : 'check', { size: 18 }) + '</span>' +
      '<div><b>批量完成</b>：成功 ' + ok + ' 张' +
      (fail ? '，失败 ' + fail + ' 张' : '') +
      '。已进入当前文件夹与教师工作台，按红黄绿置信度分流——<b>优先审红、黄桶</b>。</div>' +
      '</div>' +
      '<div class="inline mt-4">' +
      '<button class="btn" type="button" data-goto="teacher">' +
      I('table', { size: 16 }) + '<span>去工作台审核</span></button>' +
      '<button class="btn btn--ghost" type="button" data-goto="result">' +
      '<span>查看批改结果</span></button></div>';
    Icons.hydrate(box);
    if (ok > 0) {
      var meta = activeFolderMeta();
      autoPushFeishu((meta ? meta.name : '批量上传') + ' · 批量批改完成');
      loadFolders();
    }
  }

  function selectBatchResult(idx, list) {
    var it = list[idx];
    if (!it || !it.result) return;
    CURRENT = it.result;
    // renderResult 会重画 #result-body 并在末尾重建 picker（含 selected 项），
    // 所以这里不需要再手动同步下拉的选中态。
    //
    // 刻意不做 scrollIntoView：切下拉时用户的视线就在下拉上，把页面滚一下
    // 反而让人失去位置；而且 masthead 是 sticky 的，滚到 #result-body 顶端
    // 会把下拉本身压到吸顶栏后面看不见。
    renderResult();
  }

  /* 移除残留的批次切换条。
     必须显式删：picker 是在 renderResult 之后插进 #result-body 的子节点，
     而 renderResult 重写 innerHTML 时它还不存在，所以不会被自然覆盖。
     不删的话，「先批量、再单张」之后页面上会留着上一批的切换按钮，
     点进去看到的是别人的作业。 */
  function clearBatchPicker() {
    var old = document.getElementById('batch-result-picker');
    if (old && old.parentNode) old.parentNode.removeChild(old);
  }

  function renderBatchPicker() {
    if (!BATCH_LIST) { clearBatchPicker(); return; }
    var okItems = [];
    for (var i = 0; i < BATCH_LIST.length; i++) {
      if (BATCH_LIST[i].status === 'ok') okItems.push(i);
    }
    if (okItems.length < 2) { clearBatchPicker(); return; }
    var curIdx = -1;
    for (var j = 0; j < BATCH_LIST.length; j++) {
      if (BATCH_LIST[j].result === CURRENT) { curIdx = j; break; }
    }
    // 用下拉而不是按钮排：批量上传 14 张时，一行按钮会折成三四行铺满屏幕，
    // 把真正要看的批改结果挤到下面去。下拉恒占一行，且原生控件带键盘导航。
    if (curIdx < 0) curIdx = okItems[0];
    var opts = '';
    for (var k = 0; k < okItems.length; k++) {
      var idx = okItems[k];
      var it = BATCH_LIST[idx];
      var r = it.result || {};
      var label = it.name.length > 26 ? it.name.slice(0, 25) + '…' : it.name;
      // 选项里直接带分数与分流，不用逐个点开才知道哪份该看
      var meta = (r.total_score != null)
        ? '  ' + r.total_score + '/' + r.max_score + ' 分 · ' +
          (STATUS_TEXT[r.status] || r.status || '')
        : '';
      opts += '<option value="' + idx + '"' + (idx === curIdx ? ' selected' : '') + '>' +
        esc((k + 1) + '. ' + label + meta) + '</option>';
    }
    // 标签写成能独立成句的形式。窄屏下这三块会各占一行，
    // 若沿用「查看这批的第 / [下拉] / 份」的句式，最后会剩一个孤零零的「份」。
    var html =
      '<div class="batch-picker">' +
        '<label class="batch-picker__label" for="batch-result-select">' +
          '切换查看' +
        '</label>' +
        '<span class="batch-picker__wrap">' +
          '<select id="batch-result-select" class="batch-picker__select"' +
            ' aria-label="切换查看本批次的批改结果">' + opts + '</select>' +
          '<span class="batch-picker__caret" aria-hidden="true">' +
            I('chevronRight', { size: 15 }) + '</span>' +
        '</span>' +
        '<span class="hint">本批共 ' + okItems.length + ' 份</span>' +
      '</div>';
    var picker = document.getElementById('batch-result-picker');
    if (!picker) {
      picker = document.createElement('div');
      picker.id = 'batch-result-picker';
      var body = $('#result-body');
      body.insertBefore(picker, body.firstChild);
    }
    picker.innerHTML = html;
    Icons.hydrate(picker);
    // change 而不是全局委托：<select> 不走 click 委托那条路
    var sel = picker.querySelector('#batch-result-select');
    if (sel) {
      sel.addEventListener('change', function () {
        selectBatchResult(Number(sel.value), BATCH_LIST);
      });
    }
  }

  function processBatch(list, idx, token) {
    if (idx >= list.length) return;
    var it = list[idx];
    it.status = 'working';
    it.text = '识别中';
    renderBatch(list);
    var maxMB = (CONFIG.guard && CONFIG.guard.max_image_mb) || 8;

    compressImage(it.file).then(function (blob) {
      if (blob.size > maxMB * 1024 * 1024) {
        it.status = 'fail';
        it.text = '超过 ' + maxMB + 'MB 上限';
        renderBatch(list);
        if (token === BATCH_TOKEN) processBatch(list, idx + 1, token);
        return;
      }
      return blobToB64(blob).then(function (b64) {
        return api('/api/recognize-image', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_base64: b64, mime: blob.type || 'image/png' })
        }).then(function (r) {
          if (r.engine === 'none') {
            it.status = 'fail';
            it.text = '未能识别：离线演示模式只识别下方内置样例';
            it.html = RECOG_GUIDE[r.reason] || RECOG_GUIDE.no_match;
            renderBatch(list);
            if (token === BATCH_TOKEN) processBatch(list, idx + 1, token);
            return;
          }
          it.text = '批改中';
          renderBatch(list);
          var body;
          if (r.engine === 'vlm') {
            if (!r.question_id) {
              it.status = 'fail';
              it.text = '未能判定题目';
              renderBatch(list);
              if (token === BATCH_TOKEN) processBatch(list, idx + 1, token);
              return;
            }
            body = { question_id: r.question_id, ocr_text: r.text || '',
                     ocr_clarity: r.clarity, engine: r.engine,
                     folder_id: ACTIVE_FOLDER || undefined };
          } else if (r.matched_submission_id) {
            body = { matched_submission_id: r.matched_submission_id, engine: r.engine,
                     folder_id: ACTIVE_FOLDER || undefined };
          } else {
            it.status = 'fail';
            it.text = '未能判定题目';
            renderBatch(list);
            if (token === BATCH_TOKEN) processBatch(list, idx + 1, token);
            return;
          }
          return api('/api/grade-image', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
          }).then(function (res) {
            it.status = 'ok';
            it.result = res;   // 保留该份完整批改结果，供批量卡片就地展开证据链
            it.text = res.total_score + ' / ' + res.max_score + ' 分 · 置信度 ' +
              Math.round(res.confidence) + ' · ' + esc(STATUS_TEXT[res.status] || res.status);
            if (idx === list.length - 1 && token === BATCH_TOKEN) CURRENT = res;
            renderBatch(list);
            if (token === BATCH_TOKEN) processBatch(list, idx + 1, token);
          });
        });
      });
    }).catch(function (e) {
      it.status = 'fail';
      it.text = '失败：' + e.message;
      renderBatch(list);
      if (token === BATCH_TOKEN) processBatch(list, idx + 1, token);
    });
  }

  /* 识别不了是公开体验里最常见的一步：必须给出路，而不是甩一句「失败」 */
  var RECOG_GUIDE = {
    no_match:
      '这张照片不在内置样例库中。当前是<b>离线演示模式</b>——识别环节通过与内置手写样例照片比对来模拟，' +
      '因此只认得下方图库里的样例。<br>点下方任意一张样例照片，同样能走完整条动线：' +
      '识别 → 过程级批改 → 红黄绿分流 → 教师终审 → 班级看板。',
    vlm_unavailable:
      '真实识别暂时不可用（公开体验的每日额度已用尽）。<b>离线链路完全不受影响</b>——' +
      '点下方任意一张内置样例照片，仍可完整体验批改、分流与教师终审。',
    vlm_failed:
      '多模态识别调用失败，可能是网络或上游服务波动。可稍后重试，' +
      '或先用下方内置样例照片体验完整流程。'
  };

  function recognize(b64, mime, note) {
    RECOG = null;
    var sheet = $('#recog-sheet');
    sheet.style.display = '';
    $('#recog-result').style.display = 'none';
    $('#recog-status').innerHTML = spinner('正在识别手写内容');
    sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });

    return api('/api/recognize-image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_base64: b64, mime: mime })
    }).then(function (r) {
      if (r.engine === 'none') {
        $('#recog-status').innerHTML =
          '<div class="note note--amber"><span class="note__ico">' + I('alert', { size: 18 }) +
          '</span><div><b>未能识别这张照片。</b><br>' +
          (RECOG_GUIDE[r.reason] || RECOG_GUIDE.no_match) +
          '<div class="mt-4"><button class="btn btn--sm" type="button" id="use-sample">' +
          I('image', { size: 14 }) + '<span>用内置样例照片体验</span>' +
          I('arrowRight', { size: 14 }) + '</button></div>' +
          (r.error ? '<p class="hint mt-4">技术详情：' + esc(r.error) + '</p>' : '') +
          '</div></div>';
        return;
      }
      RECOG = r;
      $('#recog-status').innerHTML = '';
      $('#recog-result').style.display = '';

      var badges = [];
      badges.push('<span class="stamp stamp--quiet">' + I('scan', { size: 13 }) + ' ' +
        esc(ENGINE_TEXT[r.engine] || r.engine) + '</span>');
      var clarityCls = r.clarity >= 85 ? 'factor--ok' : (r.clarity >= 60 ? 'factor--warn' : 'factor--bad');
      badges.push('<span class="factor ' + clarityCls + '">卷面清晰度 <b>' + r.clarity + '</b></span>');
      if (r.matched_submission_id) {
        badges.push('<span class="factor factor--ok">命中内置样例 <b>' +
          esc(r.student_name || '') + ' · ' + esc(r.subject || '') + '</b></span>');
      } else if (r.question_id) {
        badges.push('<span class="factor factor--ok">自动判题 <b>' +
          esc(r.subject || '') + ' · ' + esc(r.question_title || '') + '</b></span>');
      }
      if (note) badges.push('<span class="factor">' + esc(note) + '</span>');
      if (r.quota_note) badges.push('<span class="factor factor--warn">' + esc(r.quota_note) + '</span>');
      $('#recog-badges').innerHTML = badges.join('');

      var ta = $('#ocr-text');
      ta.value = r.text || '';
      var editable = (r.engine === 'vlm');
      ta.readOnly = !editable;
      $('#ocr-title').textContent = editable ? '转写文本 · 可修正后再批改' : '转写文本 · 内置样例标准转写';
      $('#ocr-hint').textContent = editable
        ? '识别结果可能有误？直接在上方修正文字再提交——教师对转写的修正会真实进入批改，这正是「教师可控」的落点。'
        : '离线演示模式：命中内置样例，按预置标准转写走批改流水线。';
      $('#grade-hint').textContent = '';
      Icons.hydrate();
    }).catch(function (e) {
      showRecogError(e.message);
    });
  }

  /* 识别失败后的兜底入口：改用内置样例。
     内置样例只挂在 Demo 夹下，所以先切到 Demo 夹再展开夹内清单——
     体验者此刻可能正停在某个自建夹上，直接开当前夹会开出一个空列表。 */
  function useSampleInstead() {
    $('#recog-sheet').style.display = 'none';
    if (ACTIVE_FOLDER === 'demo') { openFolderDetail(); return; }
    api('/api/folders/active', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_id: 'demo' })
    }).then(function (d) {
      ACTIVE_FOLDER = d.active_folder_id || 'demo';
      FOLDERS.forEach(function (f) { f.is_active = (f.folder_id === ACTIVE_FOLDER); });
      renderFolders();
      openFolderDetail();
    }).catch(function () {
      // 切夹失败也要给出路：至少把夹区滚到眼前，让人自己点 Demo 夹
      $('#folder-grid').scrollIntoView({
        behavior: M.reduced ? 'auto' : 'smooth', block: 'center' });
    });
  }

  function submitImageGrade() {
    if (!RECOG) return;
    var hint = $('#grade-hint');
    hint.innerHTML = '<span class="spin">' + I('loader', { size: 14 }) + '</span> 批改中';
    var body;
    if (RECOG.engine === 'vlm') {
      // VLM 模式下一律按「当前转写文本」批改，哪怕命中了内置样例——
      // 否则教师对转写的修正就成了摆设（教师可控原则）
      if (!RECOG.question_id) { hint.textContent = '未能判定题目，无法批改'; return; }
      body = {
        question_id: RECOG.question_id,
        ocr_text: $('#ocr-text').value,
        ocr_clarity: RECOG.clarity,
        engine: RECOG.engine,
        folder_id: ACTIVE_FOLDER || undefined
      };
      if (RECOG.student_name) body.student_name = RECOG.student_name;
    } else if (RECOG.matched_submission_id) {
      body = { matched_submission_id: RECOG.matched_submission_id, engine: RECOG.engine,
               folder_id: ACTIVE_FOLDER || undefined };
    } else {
      hint.textContent = '未能判定题目，无法批改';
      return;
    }
    api('/api/grade-image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      BATCH_LIST = null;  // 单份批改：清掉旧批量缓存
      CURRENT = res;
      hint.textContent = '';
      renderResult();
      switchTab('result');
      window.scrollTo({ top: 0, behavior: M.reduced ? 'auto' : 'smooth' });
      // 上传件批改完成后自动飞书提醒；样例命中不刷群
      if (res && res.source === 'upload') {
        var meta = activeFolderMeta();
        autoPushFeishu((meta ? meta.name + ' · ' : '') +
          (res.student_name || '上传作业') + ' 批改完成');
        loadFolders();
      }
    }).catch(function (e) {
      hint.textContent = '批改失败：' + e.message;
    });
  }

  /* ======================================================================
     6. Step 02：批改结果
     ====================================================================== */

  function stepState(score, max) {
    if (score >= max) return ['correct', 'is-correct', '全部得分'];
    if (score <= 0) return ['wrong', 'is-wrong', '未得分'];
    return ['partial', 'is-partial', '部分得分'];
  }

  function renderResult() {
    var r = CURRENT;
    if (!r) return;

    var steps = stepChainHtml(r.step_analysis);

    var factors = Object.keys(r.confidence_factors).map(function (k) {
      var w = FACTOR_WEIGHT[k];
      return '<span class="factor">' + esc(FACTOR_LABEL[k] || k) +
        (w ? ' <em style="font-style:normal;color:var(--ink-faint);font-family:var(--font-mono);">&#215;' + w.toFixed(2) + '</em>' : '') +
        ' <b>' + r.confidence_factors[k] + '</b></span>';
    }).join('');
    if (r.consistency_check) {
      factors += '<span class="factor factor--ok">二次批改一致性 <b>' +
        r.consistency_check.agreement + '</b>（复核分 ' + r.consistency_check.second_score + '）</span>';
    }
    if (r.cross_check) {
      factors += '<span class="factor ' + (r.cross_check.escalated ? 'factor--bad' : 'factor--ok') +
        '">双模型交叉验证 <b>' + (r.cross_check.escalated ? '结论分歧 · 已转人工' : '结论一致') +
        '</b>（' + esc(r.cross_check.model2 || '模型 2') + ' 判 ' + r.cross_check.model2_score + ' 分）</span>';
    }

    var tags = r.error_tags.length
      ? r.error_tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('')
      : '<span class="muted">无</span>';

    var mine = (r.source === 'upload')
      ? '<div class="note note--green"><span class="note__ico">' + I('circleCheck', { size: 18 }) +
        '</span><div>这份是<b>你刚上传的作业</b>，已编号 <b>' + esc(r.submission_id || '') +
        '</b> 并进入「03 教师工作台」与「04 班级看板」，可以继续走完终审动线。</div></div>' : '';

    $('#result-body').innerHTML =
      mine +
      '<div class="sheet">' +
        '<div class="sheet__head"><div>' +
          '<span class="kicker">Step 02 · Grade</span>' +
          '<h2 class="sheet__title">' + esc(r.student_name) + ' · ' + esc(r.subject) + ' · ' + esc(r.question_title) + '</h2>' +
        '</div><span class="folio">02</span></div>' +

        '<div class="readout">' +
          '<div><div class="readout__k">总分 Score</div>' +
            '<div class="readout__v"><span id="ro-score">0</span><small> / ' + r.max_score + '</small></div></div>' +
          '<div><div class="readout__k">置信度 Confidence</div>' +
            '<div class="readout__v" style="color:' + STATUS_VAR[r.status] + '"><span id="ro-conf">0</span></div>' +
            '<div class="meter"><div class="meter__track"><div class="meter__fill" id="ro-meter" style="background:' + STATUS_VAR[r.status] + '"></div></div>' +
            '<div class="meter__ticks"><span>0</span><span>60</span><span>85</span><span>100</span></div></div></div>' +
          '<div><div class="readout__k">分流 Triage</div>' +
            '<div style="padding-top:8px;"><span class="stamp stamp--' + r.status + '">' +
            I(r.status, { size: 13 }) + ' ' + STATUS_TEXT[r.status] + '</span></div></div>' +
          '<div><div class="readout__k">链路 Pipeline</div>' +
            '<div style="padding-top:8px;">' +
            (r.recognition_engine ? '<span class="factor">识别 <b>' +
              esc(ENGINE_TEXT[r.recognition_engine] || r.recognition_engine) + '</b></span>' : '') +
            '<span class="factor">批改 <b>' + (r.mode === 'llm' ? '真实 LLM' : 'Mock 规则引擎') + '</b></span>' +
            '</div></div>' +
        '</div>' +

        (r.note ? '<p class="hint mt-4">' + esc(r.note) + '</p>' : '') +
        '<h3>置信度五因子 &#183; 设计方案 &#167;9.7</h3>' +
        '<div>' + factors + '</div>' +
      '</div>' +

      '<div class="sheet">' +
        '<div class="sheet__head"><div><span class="kicker">Context</span>' +
        '<h2 class="sheet__title">题目与作答</h2></div></div>' +
        '<h3>题目</h3><div>' + esc(r.question_text) + '</div>' +
        '<h3>标准答案</h3><div class="muted">' + esc(r.standard_answer) + '</div>' +
        '<h3>学生作答 · OCR 转写</h3><div class="pre">' + esc(r.ocr_text) + '</div>' +
      '</div>' +

      '<div class="sheet">' +
        '<div class="sheet__head"><div><span class="kicker">Evidence Chain</span>' +
        '<h2 class="sheet__title">逐批改节点与证据链</h2></div>' +
        '<span class="hint" style="max-width:280px;text-align:right;">每一步判分都引用学生作答原文作为依据，老师是在「审」而不是在「信」。</span></div>' +
        '<div class="steps">' + steps + '</div>' +
        '<h3>知识点</h3><div>' +
          r.knowledge_points.map(function (k) { return '<span class="tag tag--kp">' + esc(k) + '</span>'; }).join('') +
        '</div>' +
        '<h3>错因标签 &#183; &#167;6.11 十类枚举</h3><div>' + tags + '</div>' +
      '</div>' +

      '<div class="sheet">' +
        '<div class="sheet__head"><div><span class="kicker">Feedback</span>' +
        '<h2 class="sheet__title">个性化评语</h2></div></div>' +
        '<div class="quoteblock">' + esc(r.student_feedback) + '</div>' +
        '<h3>教师备注</h3><div class="muted">' + esc(r.teacher_note) + '</div>' +
      '</div>';

    Icons.hydrate();
    M.reveal($('#result-body'));
    M.countTo($('#ro-score'), r.total_score, { digits: 0 });
    M.countTo($('#ro-conf'), r.confidence, { digits: 1 });
    M.growTo($('#ro-meter'), r.confidence, 120);
    renderBatchPicker();
  }

  /* ======================================================================
     7. Step 03：教师工作台
     ====================================================================== */

  function loadTeacher() {
    var body = $('#teacher-body');
    api('/api/teacher/results').then(function (data) {
      TAG_ENUM = data.error_tags_enum || [];
      TEACHER_ROWS = data.results;
      renderTeacherList();
    }).catch(function (e) {
      body.innerHTML = emptyBox('加载失败：' + e.message, 'alert');
      Icons.hydrate(body);
    });
  }

  var TEACHER_FILTER = { status: 'all', subject: 'all', tag: 'all' };

  function renderTeacherList() {
    var body = $('#teacher-body');
    // 红→黄→绿 默认排序，同级置信度升序（最不自信优先）；未审的排前面
    var rows = TEACHER_ROWS.filter(function (r) {
      if (TEACHER_FILTER.status !== 'all') {
        if (TEACHER_FILTER.status === 'unreviewed' && r.reviewed) return false;
        if (TEACHER_FILTER.status === 'reviewed' && !r.reviewed) return false;
        if (['green', 'yellow', 'red'].indexOf(TEACHER_FILTER.status) >= 0 &&
            r.status !== TEACHER_FILTER.status) return false;
      }
      if (TEACHER_FILTER.subject !== 'all' &&
          r.subject + ' · ' + r.question_title !== TEACHER_FILTER.subject) return false;
      if (TEACHER_FILTER.tag !== 'all' &&
          r.error_tags.indexOf(TEACHER_FILTER.tag) < 0) return false;
      return true;
    }).sort(function (a, b) {
      var rk = { red: 0, yellow: 1, green: 2 };
      if (rk[a.status] !== rk[b.status]) return rk[a.status] - rk[b.status];
      if (a.reviewed !== b.reviewed) return a.reviewed ? 1 : -1;
      return a.confidence - b.confidence;
    });

    var subjects = [];
    TEACHER_ROWS.forEach(function (r) {
      var k = r.subject + ' · ' + r.question_title;
      if (subjects.indexOf(k) < 0) subjects.push(k);
    });
    var statusOpts = [['all', '全部状态'], ['unreviewed', '未审'],
      ['reviewed', '已审'], ['red', '红桶'], ['yellow', '黄桶'], ['green', '绿桶']]
      .map(function (o) {
        return '<option value="' + o[0] + '"' +
          (TEACHER_FILTER.status === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
      }).join('');
    var subjOpts = ['<option value="all"' +
      (TEACHER_FILTER.subject === 'all' ? ' selected' : '') + '>全部题目</option>']
      .concat(subjects.map(function (s) {
        return '<option value="' + esc(s) + '"' +
          (TEACHER_FILTER.subject === s ? ' selected' : '') + '>' + esc(s) + '</option>';
      })).join('');
    var tagOpts = ['<option value="all"' +
      (TEACHER_FILTER.tag === 'all' ? ' selected' : '') + '>全部错因</option>']
      .concat(TAG_ENUM.map(function (t) {
        return '<option value="' + esc(t) + '"' +
          (TEACHER_FILTER.tag === t ? ' selected' : '') + '>' + esc(t) + '</option>';
      })).join('');
    var filterBar =
      '<div class="filters" style="display:flex;gap:10px;flex-wrap:wrap;' +
      'align-items:center;margin-bottom:var(--sp-4);">' +
      '<span class="hint" style="margin:0;">筛选：</span>' +
      '<select class="select" data-f="status">' + statusOpts + '</select>' +
      '<select class="select" data-f="subject">' + subjOpts + '</select>' +
      '<select class="select" data-f="tag">' + tagOpts + '</select>' +
      '<span class="hint" style="margin:0;">共 <b>' + rows.length + '</b> 份</span>' +
      '</div>';

    var trs = rows.map(function (r) {
      var actions;
      if (r.reviewed) {
        var label = r.teacher_action === 'modified' ? '已修改' : '已确认';
        var revTags = (r.final_error_tags && r.final_error_tags.length)
          ? '<div class="hint">错因修订：' + r.final_error_tags.map(esc).join('、') + '</div>' : '';
        actions = '<span style="color:var(--riso-green);font-weight:600;display:inline-flex;gap:5px;align-items:center;">' +
          I('check', { size: 14, stroke: 2.4 }) + label + ' ' + r.final_score + ' / ' + r.max_score +
          '</span>' + revTags +
          '<div class="actions"><button class="btn btn--sm btn--ghost" type="button" data-review="' +
          esc(r.submission_id) + '">' + I('eye', { size: 13 }) + '查看/改判</button></div>';
      } else {
        actions = '<div class="actions">' +
          '<button class="btn btn--sm ' + (r.status === 'green' ? 'btn--ghost' : '') +
          '" type="button" data-confirm="' + esc(r.submission_id) + '">' +
          (r.status === 'green' ? '抽查通过' : '确认') + '</button>' +
          '<button class="btn btn--sm btn--ghost" type="button" data-review="' + esc(r.submission_id) + '">' +
          I('penLine', { size: 13 }) + '终审修改</button>' +
          '<button class="btn btn--sm btn--ghost" type="button" data-detail="' +
          esc(r.submission_id) + '">' + I('eye', { size: 13 }) + '查看批改</button></div>';
      }
      var tags = r.error_tags.length
        ? r.error_tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('')
        : '<span class="muted">—</span>';
      var mine = (r.source === 'upload') ? '<span class="tag tag--mine">我的上传</span>' : '';
      return '<tr>' +
        '<td><b>' + esc(r.student_name) + '</b>' + mine + '</td>' +
        '<td>' + esc(r.subject) + ' · ' + esc(r.question_title) + '</td>' +
        '<td class="num"><b>' + r.ai_score + '</b> / ' + r.max_score + '</td>' +
        '<td class="num">' + r.confidence.toFixed(1) + '</td>' +
        '<td><span class="stamp stamp--' + r.status + '">' + I(r.status, { size: 12 }) + ' ' +
          STATUS_TEXT[r.status] + '</span></td>' +
        '<td>' + tags + '</td>' +
        '<td>' + actions + '</td></tr>';
    }).join('');

    body.innerHTML = filterBar + '<div class="tablewrap"><table>' +
      '<thead><tr><th>学生</th><th>题目</th><th>AI 分</th><th>置信度</th>' +
      '<th>分流</th><th>错因</th><th>操作</th></tr></thead>' +
      '<tbody>' + trs + '</tbody></table></div>';
    Icons.hydrate(body);
    var sel = body.querySelectorAll('select[data-f]');
    for (var i = 0; i < sel.length; i++) {
      (function (s) {
        s.addEventListener('change', function () {
          TEACHER_FILTER[s.dataset.f] = s.value;
          renderTeacherList();
        });
      })(sel[i]);
    }
  }

  function showTeacherNote(resp) {
    var el = $('#teacher-note');
    if (!el || !resp || resp.question_pass_rate == null) return;
    el.innerHTML = '<div class="note note--green"><span class="note__ico">' +
      I('shieldCheck', { size: 18 }) + '</span><div>终审已记录。该题教师通过率因子回灌为 <b class="num">' +
      resp.question_pass_rate + '</b>，同题未终审作答的置信度已按新因子重新计算。</div></div>';
    Icons.hydrate(el);
    setTimeout(function () { el.innerHTML = ''; }, 6000);
  }

  function quickReview(id) {
    api('/api/teacher/review', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ submission_id: id, teacher_action: 'confirmed' })
    }).then(function (resp) {
      showTeacherNote(resp);
      loadTeacher();
    }).catch(function (e) { alert('提交失败：' + e.message); });
  }

  // 逐步证据链 HTML（结果页 / 审卷面板共用）
  function stepChainHtml(step_analysis) {
    if (!step_analysis || !step_analysis.length) return '';
    return '<div class="steps">' + step_analysis.map(function (s, i) {
      var st = stepState(s.score, s.max_score);
      var tag = s.error_tag ? '<span class="tag">' + esc(s.error_tag) + '</span>' : '';
      var evid = s.evidence
        ? '<div class="evidence">' + I('quote', { size: 14 }) +
          '<span>' + esc(s.evidence) + '</span></div>' : '';
      var illegible = (s.legible === false)
        ? '<div class="evidence evidence--warn">' + I('alert', { size: 14 }) +
          '<span>该步字迹难以辨认，建议教师人工复核</span></div>' : '';
      return '<div class="step" data-reveal="' + (i * 45) + '">' +
        '<span class="step__mark ' + st[1] + '" title="' + st[2] + '">' +
        I(st[0], { size: 15, stroke: 2.2 }) + '</span>' +
        '<div class="step__head"><span class="step__name">' + esc(s.step) + '</span>' +
        '<span class="step__score num">' + s.score + ' / ' + s.max_score + '</span></div>' +
        '<p class="step__reason">' + esc(s.reason) + ' ' + tag +
        '<span class="tag tag--kp">' + esc(s.knowledge_point) + '</span></p>' +
        evid + illegible + '</div>';
    }).join('') + '</div>';
  }

  // 置信度五因子 HTML（审卷面板用）
  function factorChipsHtml(r) {
    if (!r.confidence_factors) return '';
    return Object.keys(r.confidence_factors).map(function (k) {
      var w = FACTOR_WEIGHT[k];
      return '<span class="factor">' + esc(FACTOR_LABEL[k] || k) +
        (w ? ' <em style="font-style:normal;color:var(--ink-faint);font-family:var(--font-mono);">&#215;' + w.toFixed(2) + '</em>' : '') +
        ' <b>' + r.confidence_factors[k] + '</b></span>';
    }).join('');
  }

  // 审卷面板：同屏 题目 / OCR / 逐步证据链 / 置信度因子 + 终审表单
  function openReview(id) {
    var r = TEACHER_ROWS.find(function (x) { return x.submission_id === id; });
    if (!r) return;
    var cur = {};
    var baseTags = (r.final_error_tags && r.final_error_tags.length) ? r.final_error_tags : r.error_tags || [];
    baseTags.forEach(function (t) { cur[t] = 1; });
    var baseScore = (r.final_score != null) ? r.final_score : r.ai_score;
    var baseComment = r.final_comment || '';
    var tags = TAG_ENUM.map(function (t) {
      return '<label class="' + (cur[t] ? 'is-on' : '') + '">' +
        '<input type="checkbox" value="' + esc(t) + '"' + (cur[t] ? ' checked' : '') + '>' +
        esc(t) + '</label>';
    }).join('');

    var ocrBlock = r.ocr_text
      ? '<div class="hint" style="white-space:pre-wrap;font-family:var(--font-mono);font-size:12px;' +
        'background:var(--paper-warm);border:1px solid var(--rule);border-radius:10px;padding:10px;' +
        'margin-top:var(--sp-2);">' + esc(r.ocr_text) + '</div>'
      : '<div class="hint">（内置样例 · 转写见结果页）</div>';

    $('#modal-root').innerHTML =
      '<div class="mask" id="mask"><div class="dialog dialog--wide" role="dialog" aria-modal="true">' +
      '<div class="sheet__head" style="margin-bottom:var(--sp-4);">' +
        '<div><span class="kicker">Final Review</span>' +
        '<h2 class="sheet__title">教师终审 · ' + esc(r.student_name) + '</h2></div>' +
        '<button class="note__close" type="button" data-close="1">' + I('x', { size: 18 }) + '</button>' +
      '</div>' +
      '<p class="hint review-summary" style="margin:0;">' + esc(r.subject) + ' · ' + esc(r.question_title) +
        ' ｜ AI 判分 <b class="num">' + r.ai_score + ' / ' + r.max_score +
        '</b>，置信度 <b class="num">' + r.confidence.toFixed(1) + '</b>' +
        (r.reviewed ? ' ｜ <span style="color:var(--riso-green);font-weight:600;">已终审</span>' : '') +
        '</p>' +
      '<div class="review-body">' +
        '<div>' +
          '<h3>题目与标准答案</h3>' +
          '<p class="hint">' + esc(r.question_text) + '</p>' +
          '<p class="hint" style="color:var(--riso-green);font-weight:600;">标准答案：' +
            esc(r.standard_answer) + '</p>' +
          '<h3 class="mt-4">识别转写</h3>' + ocrBlock +
          '<h3 class="mt-4">置信度因子</h3>' +
          '<div class="factorwrap">' + factorChipsHtml(r) + '</div>' +
        '</div>' +
        '<div>' +
          '<h3>AI 逐步证据链</h3>' +
          stepChainHtml(r.step_analysis) +
        '</div>' +
      '</div>' +
      '<div class="review-foot">' +
        '<div style="display:grid;grid-template-columns:minmax(0,240px) minmax(0,1fr);gap:var(--sp-4);">' +
          '<div><h3>终审分数</h3>' +
          '<input type="number" id="rv-score" min="0" max="' + r.max_score + '" step="0.5" value="' +
            baseScore + '"></div>' +
          '<div><h3>错因标签改判 &#183; &#167;6.11 十类枚举</h3>' +
          '<div class="tagpick" id="rv-tags">' + tags + '</div></div>' +
        '</div>' +
        '<h3 class="mt-4">评语修订 · 留空则沿用 AI 评语</h3>' +
        '<textarea id="rv-comment" rows="3" placeholder="' +
          esc((r.ai_feedback || '').slice(0, 50)) + '…">' + esc(baseComment) + '</textarea>' +
        '<div class="inline mt-4" style="justify-content:flex-end;">' +
          '<button class="btn btn--ghost" type="button" data-close="1">取消</button>' +
          '<button class="btn" type="button" id="rv-submit" data-id="' + esc(id) +
          '" data-max="' + r.max_score + '">' + I('check', { size: 15 }) + '提交终审</button>' +
        '</div>' +
      '</div></div></div>';
    Icons.hydrate($('#modal-root'));
    var input = $('#rv-score');
    if (input) input.focus();
  }

  function closeModal() { $('#modal-root').innerHTML = ''; }

  function submitReview(id, maxScore) {
    var score = Number($('#rv-score').value);
    if (isNaN(score) || score < 0 || score > maxScore) {
      alert('分数须在 0 ~ ' + maxScore + ' 之间');
      return;
    }
    var tags = $$('#rv-tags input:checked').map(function (cb) { return cb.value; });
    var comment = $('#rv-comment').value.trim();
    // 分数、错因、评语都没实际改动时按「确认」提交，避免无谓压低该题回灌通过率
    var row = TEACHER_ROWS.find(function (x) { return x.submission_id === id; });
    var baseTags = (row && row.final_error_tags && row.final_error_tags.length)
      ? row.final_error_tags : (row ? row.error_tags : []);
    var baseScore = (row && row.final_score != null) ? row.final_score : (row ? row.ai_score : 0);
    var tagsSame = tags.length === baseTags.length &&
      tags.every(function (t) { return baseTags.indexOf(t) >= 0; });
    var untouched = row && score === baseScore && tagsSame && !comment;
    var payload = untouched
      ? { submission_id: id, teacher_action: 'confirmed' }
      : { submission_id: id, teacher_action: 'modified', final_score: score,
          final_error_tags: tags, final_comment: comment || null };

    api('/api/teacher/review', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      closeModal();
      showTeacherNote(resp);
      loadTeacher();
    }).catch(function (e) { alert('提交失败：' + e.message); });
  }

  /* ======================================================================
     8. Step 04：班级看板
     ====================================================================== */

  function severity(rate) {
    if (rate >= 60) return 'var(--riso-red)';
    if (rate >= 40) return 'var(--riso-amber)';
    return 'var(--riso-green)';
  }

  function loadBoard() {
    var body = $('#board-body');
    api('/api/analytics/class').then(function (d) {
      var dist = d.distribution;

      var kpBars = d.weak_knowledge_points.map(function (w, i) {
        return '<div class="bar"><span class="bar__l">' + esc(w.name) + '</span>' +
          '<span class="bar__track"><span class="bar__fill" data-grow="' + w.error_rate +
          '" data-delay="' + (i * 60) + '" style="background:' + severity(w.error_rate) + '"></span></span>' +
          '<span class="bar__v">' + w.wrong + '/' + w.total + ' · ' + w.error_rate + '%</span></div>';
      }).join('');

      var maxCount = Math.max.apply(null, [1].concat(d.error_tag_distribution.map(function (t) { return t.count; })));
      var tagBars = d.error_tag_distribution.map(function (t, i) {
        return '<div class="bar"><span class="bar__l">' + esc(t.name) + '</span>' +
          '<span class="bar__track"><span class="bar__fill" data-grow="' +
          Math.round(t.count / maxCount * 100) + '" data-delay="' + (i * 55) + '"></span></span>' +
          '<span class="bar__v">' + t.count + ' 次</span></div>';
      }).join('');

      var seg = function (k) {
        if (!dist[k]) return '';
        return '<span class="dist__seg" data-grow="' + dist[k + '_pct'] +
          '" style="background:' + STATUS_VAR[k] + '">' + STATUS_TEXT[k] + ' ' + dist[k] + '</span>';
      };

      var uploadNote = d.upload_count
        ? '<div class="note note--green"><span class="note__ico">' + I('circleCheck', { size: 18 }) +
          '</span><div>本看板含<b>你自己上传的 ' + d.upload_count + ' 份作业</b>' +
          '（内置班级作答 ' + d.builtin_count + ' 份）。这些数据只属于你这次体验。</div></div>' : '';

      body.innerHTML = uploadNote +
        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Step 04 · Analytics</span>' +
          '<h2 class="sheet__title">班级学情看板 · ' + esc(d.class_name) + '</h2></div>' +
          '<span class="folio">04</span></div>' +
          '<div class="kpis">' +
            '<div class="kpi"><div class="kpi__k">参与作答</div><div class="kpi__v num" data-count="' + d.student_count + '">0</div></div>' +
            '<div class="kpi"><div class="kpi__k">平均得分率</div><div class="kpi__v num" data-count="' + d.average_score_pct + '" data-digits="1" data-suffix="%">0</div></div>' +
            '<div class="kpi"><div class="kpi__k">薄弱知识点</div><div class="kpi__v num" data-count="' + d.weak_knowledge_points.length + '">0</div></div>' +
            '<div class="kpi"><div class="kpi__k">批改模式</div><div class="kpi__v is-text">' + (d.mode === 'llm' ? '真实 LLM' : 'Mock 规则引擎') + '</div></div>' +
          '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Triage</span>' +
          '<h2 class="sheet__title">红黄绿分流占比</h2></div></div>' +
          '<div class="dist">' + seg('green') + seg('yellow') + seg('red') + '</div>' +
          '<div class="legend">' +
            '<span><i class="swatch" style="background:var(--riso-green)"></i>自动通过 ' + dist.green + ' 人（' + dist.green_pct + '%）</span>' +
            '<span><i class="swatch" style="background:var(--riso-amber)"></i>教师确认 ' + dist.yellow + ' 人（' + dist.yellow_pct + '%）</span>' +
            '<span><i class="swatch" style="background:var(--riso-red)"></i>转人工 ' + dist.red + ' 人（' + dist.red_pct + '%）</span>' +
          '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Knowledge</span>' +
          '<h2 class="sheet__title">知识点错误率</h2></div></div>' +
          '<div class="bars">' + (kpBars || emptyBox('暂无数据')) + '</div>' +
          '<div class="legend">' +
            '<span><i class="swatch" style="background:var(--riso-green)"></i>&lt; 40%</span>' +
            '<span><i class="swatch" style="background:var(--riso-amber)"></i>40% – 60%</span>' +
            '<span><i class="swatch" style="background:var(--riso-red)"></i>&#8805; 60%</span>' +
          '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Error Taxonomy</span>' +
          '<h2 class="sheet__title">错因分布 · &#167;6.11 错因标签</h2></div></div>' +
          '<div class="bars">' + (tagBars || emptyBox('暂无数据')) + '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Next Lesson</span>' +
          '<h2 class="sheet__title">下节课讲评建议</h2></div></div>' +
          '<ul class="sugg">' + d.teaching_suggestions.map(function (s) {
            return '<li>' + I('lightbulb', { size: 16 }) + '<span>' + esc(s) + '</span></li>';
          }).join('') + '</ul>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Courseware</span>' +
          '<h2 class="sheet__title">讲评课件大纲</h2></div></div>' +
          '<p class="hint">基于本次批改数据自动生成结构化大纲（共性错因 + 典型错例证据 + 分层任务 + 复测建议），' +
          '可一键复制为讲评课件底稿，粘贴至希沃白板、飞书文档等备课环境。</p>' +
          '<div class="inline mt-4">' +
            '<button class="btn" type="button" id="gen-outline">' + I('bookOpen', { size: 15 }) + '生成讲评大纲</button>' +
            '<button class="btn btn--ghost" type="button" id="copy-outline" style="display:none;">' +
            I('copy', { size: 15 }) + '<span>复制 Markdown</span></button>' +
          '</div>' +
          '<div id="outline-body" class="mt-4"></div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Student Profile</span>' +
          '<h2 class="sheet__title">学生个人错因画像</h2></div></div>' +
          '<div class="inline">' +
            '<select id="stu-select" style="max-width:220px;"></select>' +
            '<span class="hint" style="flex:1;min-width:220px;">跨题聚合本次批改 + 历史错因时间线' +
            '（历史为<b>模拟数据</b>，用于演示画像形态）</span>' +
          '</div>' +
          '<div id="profile-body" class="mt-4">' + emptyBox('选择学生查看画像', 'users') + '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="sheet__head"><div><span class="kicker">Feishu · &#167;13.2</span>' +
          '<h2 class="sheet__title">飞书协同</h2></div></div>' +
          '<p class="hint">把本班学情推到飞书第二现场：机器人互动卡片提醒教师审核（集成点二），' +
          '多维表格沉淀学情台账并由 AI 字段捷径自动生成错因摘要与学习建议（集成点一 · 主用飞书 AI 能力）。</p>' +
          '<div class="inline mt-4">' +
            '<button class="btn" type="button" id="feishu-push">' + I('send', { size: 15 }) + '推送审核提醒卡片</button>' +
            '<button class="btn btn--ghost" type="button" id="feishu-sync">' + I('table', { size: 15 }) + '同步多维表格学情台账</button>' +
          '</div>' +
          '<div id="feishu-result" class="mt-5"></div>' +
        '</div>';

      Icons.hydrate(body);
      // 数字与条形图入场
      $$('[data-count]', body).forEach(function (el, i) {
        setTimeout(function () {
          M.countTo(el, Number(el.dataset.count), {
            digits: Number(el.dataset.digits) || 0, suffix: el.dataset.suffix || ''
          });
        }, 80 + i * 90);
      });
      $$('[data-grow]', body).forEach(function (el) {
        M.growTo(el, Number(el.dataset.grow), 120 + (Number(el.dataset.delay) || 0));
      });
      initStudents();
    }).catch(function (e) {
      body.innerHTML = emptyBox('加载失败：' + e.message, 'alert');
      Icons.hydrate(body);
    });
  }

  function initStudents() {
    var sel = $('#stu-select');
    if (!sel || sel.options.length) return;
    api('/api/students').then(function (d) {
      sel.innerHTML = d.students.map(function (s) {
        return '<option value="' + esc(s.student_id) + '">' + esc(s.student_name) + '</option>';
      }).join('');
      if (d.students.length) loadProfile();
    }).catch(function () { sel.innerHTML = '<option>加载失败</option>'; });
  }

  function loadProfile() {
    var sel = $('#stu-select'), body = $('#profile-body');
    if (!sel || !sel.value) return;
    body.innerHTML = spinner('聚合画像中');
    Icons.hydrate(body);
    api('/api/analytics/student/' + encodeURIComponent(sel.value)).then(function (p) {
      var freq = p.error_tag_freq || [];
      var fmax = Math.max.apply(null, [1].concat(freq.map(function (t) { return t.count; })));
      var freqBars = freq.map(function (t, i) {
        return '<div class="bar"><span class="bar__l">' + esc(t.name) + '</span>' +
          '<span class="bar__track"><span class="bar__fill" data-grow="' +
          Math.round(t.count / fmax * 100) + '" data-delay="' + (i * 55) + '"></span></span>' +
          '<span class="bar__v">' + t.count + ' 次</span></div>';
      }).join('') || '<div class="muted">本次无错因记录</div>';

      var tl = (p.timeline || []).map(function (item) {
        var tags = (item.error_tags || []).length
          ? item.error_tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('')
          : '<span class="muted">无错因</span>';
        var chip = item.simulated
          ? '<span class="chip-sim">模拟历史</span>'
          : '<span class="chip-sim is-now">本次批改</span>';
        var score = (item.score_pct != null) ? ' · 得分率 ' + item.score_pct + '%' : '';
        return '<li' + (item.simulated ? ' class="is-sim"' : '') + '>' +
          '<b>' + esc(item.assignment) + '</b>' + (item.subject ? ' · ' + esc(item.subject) : '') +
          score + chip + '<div class="mt-4" style="margin-top:5px;">' + tags + '</div></li>';
      }).join('');

      body.innerHTML =
        '<div class="kpis">' +
          '<div class="kpi"><div class="kpi__k">本次作答</div><div class="kpi__v num">' + p.submission_count + '</div></div>' +
          '<div class="kpi"><div class="kpi__k">平均得分率</div><div class="kpi__v num">' + p.average_score_pct + '%</div></div>' +
          '<div class="kpi"><div class="kpi__k">薄弱知识点</div><div class="kpi__v num">' + (p.weak_knowledge_points || []).length + '</div></div>' +
        '</div>' +
        '<h3>错因频次 · 本次作业</h3><div class="bars">' + freqBars + '</div>' +
        '<h3>错因演变时间线</h3><ul class="timeline">' + tl + '</ul>' +
        '<h3>趋势判断</h3><div class="quoteblock">' + esc(p.trend_summary || '') + '</div>';
      Icons.hydrate(body);
      $$('[data-grow]', body).forEach(function (el) {
        M.growTo(el, Number(el.dataset.grow), 100 + (Number(el.dataset.delay) || 0));
      });
    }).catch(function (e) {
      body.innerHTML = emptyBox('画像加载失败：' + e.message, 'alert');
      Icons.hydrate(body);
    });
  }

  function genOutline() {
    var body = $('#outline-body');
    body.innerHTML = spinner('生成中');
    Icons.hydrate(body);
    api('/api/lecture-outline').then(function (d) {
      OUTLINE_MD = d.outline_markdown || '';
      body.innerHTML = '<div class="pre">' + esc(OUTLINE_MD) + '</div>';
      $('#copy-outline').style.display = '';
    }).catch(function (e) {
      body.innerHTML = emptyBox('生成失败：' + e.message, 'alert');
      Icons.hydrate(body);
    });
  }

  function copyOutline() {
    if (!OUTLINE_MD) return;
    var btn = $('#copy-outline');
    var restore = function () {
      btn.innerHTML = I('copy', { size: 15 }) + '<span>复制 Markdown</span>';
    };
    navigator.clipboard.writeText(OUTLINE_MD).then(function () {
      btn.innerHTML = I('check', { size: 15 }) + '<span>已复制</span>';
      setTimeout(restore, 2000);
    }).catch(function () { alert('复制失败，请手动选择文本复制'); });
  }

  /* ---------- 飞书协同 ---------- */

  function banner(mode, demoText, liveText) {
    return mode === 'demo'
      ? '<div class="note note--amber"><span class="note__ico">' + I('alert', { size: 18 }) +
        '</span><div>' + esc(demoText) + '</div></div>'
      : '<div class="note note--green"><span class="note__ico">' + I('circleCheck', { size: 18 }) +
        '</span><div>' + esc(liveText) + '</div></div>';
  }

  // lark_md 极简渲染：先转义再还原换行与 **加粗**（内容全部由后端生成，可控）
  function larkMd(s) {
    return esc(s).replace(/\n/g, '<br>').replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  }

  function pushFeishu() {
    var box = $('#feishu-result');
    box.innerHTML = spinner('推送中');
    Icons.hydrate(box);
    var meta = activeFolderMeta();
    api('/api/feishu/push', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        assignment_name: meta ? (meta.name + ' · 本次作业批改') : undefined,
        folder_id: ACTIVE_FOLDER || undefined
      })
    }).then(function (d) {
      var card = d.card && d.card.card;
      var cardHtml = '';
      if (card) {
        var header = (card.header && card.header.title) ? card.header.title.content : '飞书互动卡片';
        var parts = (card.elements || []).map(function (el) {
          if (el.tag === 'div' && el.text) return '<div style="margin:6px 0;">' + larkMd(el.text.content) + '</div>';
          if (el.tag === 'hr') return '<hr>';
          if (el.tag === 'action' && el.actions) {
            var a = el.actions[0];
            return '<div class="mt-4"><span class="btn btn--sm" style="pointer-events:none;">' +
              esc(a.text.content) + I('arrowRight', { size: 13 }) + '</span>' +
              '<p class="hint" style="margin-top:5px;">按钮链接（占位）：' + esc(a.url) + '</p></div>';
          }
          if (el.tag === 'note' && el.elements) return '<p class="hint mt-4">' + esc(el.elements[0].content) + '</p>';
          return '';
        }).join('');
        cardHtml = '<div class="larkcard"><div class="larkcard__head">' + esc(header) + '</div>' +
          '<div class="larkcard__body">' + parts + '</div></div>';
      }
      box.innerHTML =
        banner(d.mode, '演示模式：未配置飞书凭据，以下为将推送的内容', '已真实推送到飞书自定义机器人') +
        '<p class="hint">接口消息：' + esc(d.message || '') + '</p>' +
        '<h3>飞书互动卡片预览</h3>' + cardHtml +
        '<h3>接口返回 JSON · msg_type=interactive</h3>' +
        '<div class="pre">' + esc(JSON.stringify(d, null, 2)) + '</div>';
      Icons.hydrate(box);
    }).catch(function (e) {
      box.innerHTML = emptyBox('推送失败：' + e.message, 'alert');
      Icons.hydrate(box);
    });
  }

  function syncFeishu() {
    var box = $('#feishu-result');
    box.innerHTML = spinner('同步中');
    Icons.hydrate(box);
    api('/api/feishu/sync-base', { method: 'POST' }).then(function (d) {
      var records = d.records || [];
      var tableHtml = '';
      if (records.length) {
        var cols = ['学生', '题号', '得分', '满分', '错因标签', '置信度', '分流状态', '教师终审'];
        var head = cols.map(function (c) { return '<th>' + c + '</th>'; }).join('');
        var rows = records.map(function (r) {
          var f = r.fields || {};
          return '<tr>' + cols.map(function (c) { return '<td>' + esc(f[c]) + '</td>'; }).join('') + '</tr>';
        }).join('');
        tableHtml = '<h3>多维表格《学情台账》记录预览 · ' + records.length + ' 条</h3>' +
          '<div class="tablewrap"><table><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
          '<p class="hint mt-4">写入后可对「错因标签」「得分 / 满分」等列配置飞书 <b>AI 字段捷径</b>，' +
          '逐行自动生成「一句话错因摘要」与「个性化学习建议」（设计方案 &#167;13.2 集成点一）。</p>';
      }
      box.innerHTML =
        banner(d.mode, '演示模式：未配置飞书凭据，以下为将写入的记录', '已真实写入飞书多维表格《学情台账》') +
        '<p class="hint">接口消息：' + esc(d.message || '') + '</p>' + tableHtml +
        '<h3>接口返回 JSON</h3><div class="pre">' + esc(JSON.stringify(d, null, 2)) + '</div>';
      Icons.hydrate(box);
    }).catch(function (e) {
      box.innerHTML = emptyBox('同步失败：' + e.message, 'alert');
      Icons.hydrate(box);
    });
  }

  /* ======================================================================
     9. 事件绑定（统一委托，避免内联 onclick）
     ====================================================================== */

  function bind() {
    $('#enter-btn').addEventListener('click', enterApp);
    $('#back-home').addEventListener('click', backHome);
    $('#reset-btn').addEventListener('click', resetDemo);
    $('#file-input').addEventListener('change', onFileChosen);
    $('#grade-btn').addEventListener('click', submitImageGrade);

    var createBtn = $('#folder-create-btn');
    if (createBtn) createBtn.addEventListener('click', createFolder);
    var nameInput = $('#folder-name-input');
    if (nameInput) {
      nameInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); createFolder(); }
      });
    }
    var gradeFolderBtn = $('#folder-grade-btn');
    if (gradeFolderBtn) gradeFolderBtn.addEventListener('click', gradeFolder);
    var renameBtn = $('#folder-rename-btn');
    if (renameBtn) renameBtn.addEventListener('click', renameFolder);
    var deleteBtn = $('#folder-delete-btn');
    if (deleteBtn) deleteBtn.addEventListener('click', deleteFolder);
    var openBtn = $('#folder-open-btn');
    if (openBtn) openBtn.addEventListener('click', openFolderDetail);

    $('#tabs').addEventListener('click', function (e) {
      var b = e.target.closest('.tab');
      if (b) switchTab(b.dataset.tab);
    });

    // 拖拽上传：桌面端把照片直接拖进来
    var dz = $('#dropzone');
    ['dragenter', 'dragover'].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault(); dz.classList.add('is-hover');
      });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      dz.addEventListener(ev, function (e) {
        e.preventDefault(); dz.classList.remove('is-hover');
      });
    });
    dz.addEventListener('drop', function (e) {
      var fs = e.dataTransfer && e.dataTransfer.files;
      if (!fs || !fs.length) return;
      var files = Array.prototype.slice.call(fs);
      if (files.length === 1) { handleFile(files[0]); return; }
      runBatch(files);
    });

    // 全局委托：动态生成的按钮都在这里接
    document.addEventListener('click', function (e) {
      var t = e.target;
      var fcard = t.closest && t.closest('[data-folder]');
      if (fcard && fcard.classList.contains('folder-card')) {
        selectFolder(fcard.dataset.folder);
        return;
      }
      // 样例入口现在只有一处：夹内清单里的「批改」按钮
      var pick = t.closest && t.closest('[data-sample]');
      if (pick && pick.dataset.url) {
        selectSample(pick.dataset.sample, pick.dataset.url);
        return;
      }

      var goto = t.closest && t.closest('[data-goto]');
      if (goto) { switchTab(goto.dataset.goto); return; }

      if (t.closest && t.closest('#use-sample')) { useSampleInstead(); return; }
      if (t.closest && t.closest('#intro-close')) {
        localStorage.setItem('zhipi_intro_hidden', '1');
        $('#intro-slot').innerHTML = '';
        return;
      }
      if (t.closest && t.closest('#intro-more')) {
        INTRO_OPEN = true;
        renderIntro();
        Icons.hydrate($('#intro-slot'));
        return;
      }

      var conf = t.closest && t.closest('[data-confirm]');
      if (conf) { quickReview(conf.dataset.confirm); return; }
      var rev = t.closest && t.closest('[data-review]');
      if (rev) { openReview(rev.dataset.review); return; }
      var det = t.closest && t.closest('[data-detail]');
      if (det) { openReview(det.dataset.detail); return; }

      if (t.closest && t.closest('[data-close]')) { closeModal(); return; }
      if (t.id === 'mask') { closeModal(); return; }
      var sub = t.closest && t.closest('#rv-submit');
      if (sub) { submitReview(sub.dataset.id, Number(sub.dataset.max)); return; }

      var pick = t.closest && t.closest('.tagpick label');
      if (pick) {
        e.preventDefault();
        pick.classList.toggle('is-on');
        var cb = pick.querySelector('input');
        cb.checked = !cb.checked;
        return;
      }

      if (t.closest && t.closest('#gen-outline')) { genOutline(); return; }
      if (t.closest && t.closest('#copy-outline')) { copyOutline(); return; }
      if (t.closest && t.closest('#feishu-push')) { pushFeishu(); return; }
      if (t.closest && t.closest('#feishu-sync')) { syncFeishu(); return; }
    });

    document.addEventListener('change', function (e) {
      if (e.target.id === 'stu-select') loadProfile();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeModal();
    });
  }

  /* ======================================================================
     10. 启动
     ====================================================================== */

  /* 把吸顶栏实测高度写进 --masthead-h，供 scroll-margin-top 用。
     CSS 里有兜底值，但栏高随视口变（窄屏品牌行会换行、字号是流体的），
     写死一个数迟早对不上，所以量一次、并在 resize 后重量。 */
  function syncMastheadHeight() {
    var m = document.querySelector('.masthead');
    if (!m) return;
    var h = Math.round(m.getBoundingClientRect().height);
    if (h > 0) document.documentElement.style.setProperty('--masthead-h', h + 'px');
  }

  function init() {
    Icons.hydrate();
    bind();
    renderTrail();
    syncMastheadHeight();
    var rt = null;
    window.addEventListener('resize', function () {
      clearTimeout(rt);
      rt = setTimeout(syncMastheadHeight, 150);
    });

    api('/api/demo/config').then(function (c) {
      CONFIG = c;
      var label = (c.mode === 'llm') ? '真实大模型模式' : '离线演示模式';
      $('#hero-mode').textContent = label;
      $('#mode-stamp').textContent = label;
      if (c.session && c.session.active_folder_id) {
        ACTIVE_FOLDER = c.session.active_folder_id;
      }
      renderIntro();
      renderChain();
      Icons.hydrate();
    }).catch(function () { /* 配置拿不到不阻断主流程 */ });

    loadFolders();

    api('/api/sample-images').then(function (data) {
      renderGallery(data);
      Icons.hydrate();
    }).catch(function (e) {
      // 样例清单拿不到时，把话说在识别引擎那一行——#gallery 容器已经撤掉，
      // 往它上面写会直接抛 null。夹内清单自己会显示各自的加载失败。
      var hint = $('#engine-hint');
      if (hint) hint.innerHTML = '样例清单加载失败：' + esc(e.message);
    });

    // 首屏三色统计取真实分流数据，不写死数字
    api('/api/analytics/class').then(function (d) {
      fillHeroStats(d.distribution);
    }).catch(function () { /* 首屏统计失败不影响进入工作台 */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
