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
      '<div class="empty__ico">' + I(iconName || 'info', { size: 40 }) + '</div>' +
      '<p class="empty__desc">' + esc(text) + '</p>' +
      (actionHtml ? '<div class="empty__actions">' + actionHtml + '</div>' : '') +
      '</div>';
  }

  /* 轻提示。用在「做完了但页面没明显变化」的动作上：终审提交、抽查通过、
     飞书推送、复制大纲。教师终审那条绿色回灌说明另有 #teacher-note，
     两者不重复——一个说「收到了」，一个说「置信度因此变了多少」。 */
  var TOAST_TIMER = 0;

  function toast(title, body, isErr) {
    var el = $('#toast');
    if (!el) return;
    el.className = 'toast' + (isErr ? ' toast--err' : '');
    el.querySelector('b').textContent = title || '';
    el.querySelector('span').textContent = body || '';
    el.hidden = false;
    clearTimeout(TOAST_TIMER);
    TOAST_TIMER = setTimeout(function () { el.hidden = true; }, 2600);
  }

  /* ======================================================================
     1. 常量
     ====================================================================== */

  var STATUS_TEXT = { green: '自动通过', yellow: '教师确认', red: '转人工' };
  var STATUS_VAR = { green: 'var(--st-green)', yellow: 'var(--st-yellow)', red: 'var(--st-red)' };
  /* 分流章的类名后缀。视觉层用 --g/--y/--r，业务层仍只认 green/yellow/red，
     两边靠这张表对上，换设计不用改业务判断。 */
  var STATUS_STAMP = { green: 'g', yellow: 'y', red: 'r' };
  var ENGINE_TEXT = { vlm: '多模态大模型识别' };

  var FACTOR_LABEL = {
    ocr_clarity: 'OCR 识别清晰度',
    answer_match: '答案匹配度',
    rubric_coverage: 'Rubric 覆盖度',
    llm_self_consistency: '二次批改一致性',
    teacher_pass_rate: '历史教师通过率'
  };
  /* 与后端 confidence.WEIGHTS 对齐。答案匹配度只在「教师先传了答案页」时有值，
     无题库批改时为 null，按权重重归一化剔除——不是 0 分。
     双模型交叉验证不在这里：它是后置防线（分差 > 15% 强制转红），不占权重。 */
  var FACTOR_WEIGHT = {
    ocr_clarity: 0.25, answer_match: 0.25, rubric_coverage: 0.20,
    llm_self_consistency: 0.20, teacher_pass_rate: 0.10
  };
  // 展示顺序按公式书写顺序固定，不跟后端字典的插入顺序走
  var FACTOR_ORDER = ['ocr_clarity', 'answer_match', 'rubric_coverage',
                      'llm_self_consistency', 'teacher_pass_rate'];

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
  var DEFAULT_FOLDER = 'bi-math';   // 与后端 folders.DEFAULT_FOLDER_ID 一致
  var ACTIVE_FOLDER = DEFAULT_FOLDER;
  var _FOLDER_DETAIL_OPEN_ID = null; // 当前展开的夹 id，null = 收起
  var DEMO_GROUPS = [];    // 内置样例夹分组（/api/demo-pages）
  var DEMO_ITEMS = {};     // item_id -> 内置样例条目，供各处按 id 取文件
  var BANK_PICKS = {};     // 题库夹里勾选待合并建库的 item_id

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
    // 位置由 --1/--2/--3 三个类定死（错落压叠），视差在其上叠加 transform
    var speeds = [-0.045, 0.03, -0.015];
    var rots = [-5.5, 4, -1.5];
    box.innerHTML = picked.map(function (im, i) {
      return '<figure class="collage__f collage__f--' + (i + 1) + '"' +
        ' data-parallax="' + speeds[i] + '" data-parallax-rotate="' + rots[i] + '">' +
        '<img src="' + esc(im.thumb || im.url) + '" alt="" loading="lazy"></figure>';
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
    INTRO_OPEN = (name === 'submit') && !narrow();
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
  //
  // 手机上一律先收起：那段文字在 390px 宽要占掉约 250px（近三成屏幕），
  // 而它讲的是背景而非操作，第一眼该看到的是文件夹和上传区。「展开说明」
  // 仍在，想读随时点开。
  function narrow() { return window.innerWidth <= 600; }

  var INTRO_OPEN = !narrow();

  function renderIntro() {
    if (localStorage.getItem('zhipi_intro_hidden') === '1') {
      $('#intro-slot').innerHTML = '';
      return;
    }
    var mock = CONFIG.mode !== 'llm';

    if (!INTRO_OPEN) {
      // 每个短句各自成 span：桌面靠 note__sep 的「·」连成一行，窄屏由 CSS
      // 把它们改成 block 各占一行。若把句子留成裸文本节点，CSS 就管不到，
      // 390px 下会折出「合成仿手／写」这种词中断行。
      var bits = [
        '样例为三科真实作业，历史趋势为模拟数据',
        '操作只影响你自己'
      ].map(function (s) {
        return '<span class="note__sep">·</span><span class="note__bit">' + s + '</span>';
      }).join('');
      $('#intro-slot').innerHTML =
        '<div class="note note--amber note--thin" id="intro-note">' +
        '<span class="note__ico">' + I('info', { size: 16 }) + '</span>' +
        '<div><span class="note__bit"><b>' +
        (mock ? '离线演示模式' : '真实大模型模式') + '</b></span>' + bits + '</div>' +
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
        // 内置样例换成真实作业照片后，离线的感知哈希样例匹配一并下线了：
        // 它只能认出程序合成的那 11 张图。所以 mock 模式下「有分数可看」
        // 与「能识别照片」不再是一回事，这里必须说清，否则体验者会以为
        // 断网也能走通拍照批改。
        ? '教师工作台与班级学情可直接查看（内置作答的批改结果为预置数据）；' +
          '识别手写照片需要服务端配置多模态大模型密钥，当前不可用。'
        : '识别与批改都会真实调用大模型，可上传任意手写作业照片。') +
      '<br><b>数据说明。</b>内置样例是三科真实作业照片（学生页 19 份 + 教师答案页 19 份，' +
      '卷面无姓名）；教师工作台里的 11 份作答与学生画像中的历史趋势为预置演示数据' +
      '（界面已标注）。你的操作只影响你自己这一次体验，随时可点右上角「重置演示」。';
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
      FOLDERS = []; ACTIVE_FOLDER = DEFAULT_FOLDER; BANK_PICKS = {};
      _FOLDER_DETAIL_OPEN_ID = null;
      $('#recog-sheet').style.display = 'none';
      $('#batch-sheet').style.display = 'none';
      $('#folder-detail-sheet').style.display = 'none';
      $('#folder-feishu-note').style.display = 'none';
      $('#result-body').innerHTML = emptyBox('请先在「01 拍照提交」中选择内置样例或上传照片，识别后点击「提交批改」。');
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

  /* 夹的「角色」决定夹内清单给什么按钮：
     学生夹 → 加入待批清单；题库夹 → 建题库；自建夹 → 只列上传件。 */
  function folderRole(meta) {
    if (!meta || meta.kind !== 'builtin') return 'user';
    return meta.folder_id === 'bi-bank' ? 'teacher' : 'student';
  }

  function updateUploadTargetHint() {
    var meta = activeFolderMeta();
    var name = meta ? meta.name : '内置样例';
    var role = folderRole(meta);
    // 动线条上那句「当前上传目标：___」只填夹名，前缀写死在 index.html 里
    var el = $('#folder-active-hint');
    if (el) el.textContent = name;
    var t = $('#upload-target-hint');
    if (t) t.innerHTML = '上传目标：<b>' + esc(name) + '</b>（点上方文件夹可切换）';
    var actions = $('#folder-actions');
    if (actions) actions.style.display = meta ? '' : 'none';
    var ren = $('#folder-rename-btn');
    var del = $('#folder-delete-btn');
    if (ren) ren.style.display = (meta && meta.kind !== 'builtin') ? '' : 'none';
    if (del) del.style.display = (meta && meta.kind !== 'builtin') ? '' : 'none';
    // 内置学生夹的主按钮是「全部加入待批清单」而不是「整夹一键批改」：
    // 每份内置样例都要走一次真实识别 + 整页批改，一次点击烧掉五六次模型调用，
    // 代价与「点错一个按钮」不对等。题库夹没有「批改」这回事，直接隐藏。
    var gBtn = $('#folder-grade-btn');
    var gLbl = $('#folder-grade-label');
    if (gBtn) {
      gBtn.style.display = (role === 'teacher') ? 'none' : '';
      if (gLbl) {
        gLbl.textContent = (role === 'student')
          ? '全部加入待批清单' : '整夹一键批改';
      }
    }
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
      var on = (f.folder_id === ACTIVE_FOLDER);

      var cls = 'folder';
      if (!count) cls += ' folder--empty';
      if (on) cls += ' is-on';

      var meta = f.kind === 'builtin'
        ? (f.folder_id === 'bi-bank' ? '内置答案页 · ' + count + ' 份'
                                     : '内置样例 · ' + count + ' 份')
        : count + ' 份作业';
      // 无障碍：夹面的角标与描边都是视觉信号，语义全部压进这一句
      var label = f.name + '，' + meta + (on ? '，当前上传目标' : '');

      return '<button type="button" class="' + cls +
        '" data-folder="' + esc(f.folder_id) + '"' +
        ' aria-pressed="' + on + '"' +
        ' aria-label="' + esc(label) + '" title="' + esc(f.name) + '">' +
        (on ? '<span class="now" aria-hidden="true">当前</span>' : '') +
        '<span class="name">' + esc(f.name) + '</span>' +
        '<span class="meta">' + esc(meta) + '</span>' +
        '</button>';
    }).join('');
    updateUploadTargetHint();
  }

  function loadFolders() {
    return api('/api/folders').then(function (d) {
      FOLDERS = d.folders || [];
      ACTIVE_FOLDER = d.active_folder_id || DEFAULT_FOLDER;
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
    if (!meta || meta.kind === 'builtin') return;
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
    if (!meta || meta.kind === 'builtin') return;
    if (!confirm('删除文件夹「' + meta.name + '」？\n夹内已批改的上传件仍保留在工作台，只是不再归属此夹。')) return;
    api('/api/folders/' + encodeURIComponent(meta.folder_id), {
      method: 'DELETE'
    }).then(function (d) {
      FOLDERS = d.folders || [];
      ACTIVE_FOLDER = d.active_folder_id || DEFAULT_FOLDER;
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
    var btn   = $('#folder-open-btn');
    // 同一个夹再点一次 → 收起
    if (_FOLDER_DETAIL_OPEN_ID === meta.folder_id && sheet.style.display !== 'none') {
      sheet.style.display = 'none';
      _FOLDER_DETAIL_OPEN_ID = null;
      if (btn) btn.textContent = '查看夹内文件';
      return;
    }
    _FOLDER_DETAIL_OPEN_ID = meta.folder_id;
    if (btn) btn.textContent = '收起夹内文件';
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
      // 内置条目缓存下来：夹内清单里的按钮只带 item_id，取文件时按 id 回查
      items.forEach(function (it) {
        if (it.kind === 'builtin') DEMO_ITEMS[it.item_id] = it;
      });
      body.innerHTML = (folderRole(meta) === 'teacher')
        ? renderBankFolder(items)
        : '<div class="file-list">' + items.map(fileRow).join('') + '</div>';
      Icons.hydrate(body);
      if (folderRole(meta) === 'teacher') syncBankPickBar();
      sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });
    }).catch(function (e) {
      body.innerHTML = emptyBox('加载失败：' + e.message, 'alert');
      Icons.hydrate(body);
    });
  }

  /* 题库夹按学科分节：19 份答案页平铺成一列，找「这科的答案」得一行行扫。
     多选合并是必需的而不是锦上添花——语文的两份答案册属于同一套答案，
     分成两套题库会让学生页只对上一半。 */
  function renderBankFolder(items) {
    var order = [], groups = {};
    items.forEach(function (it) {
      var key = it.subject || '其他';
      if (!groups[key]) { groups[key] = []; order.push(key); }
      groups[key].push(it);
    });
    var html = '<p class="hint">勾选同一套答案的若干份（如分成两册的答案页），' +
      '再点「合并建库」建成<b>一套</b>题库；只用一份时直接点该行的「用这份建题库」。</p>' +
      '<div class="toolbar mt-4" id="bank-pick-bar">' +
      '<button class="btn btn--primary btn--sm" type="button" id="bank-merge-btn">' +
      '<span data-icon="layers" data-icon-size="14"></span>' +
      '<span id="bank-merge-label">合并建库</span></button>' +
      '<button class="btn btn--ghost btn--sm" type="button" id="bank-pick-clear">清空勾选</button>' +
      '<span class="hint-inline" id="bank-pick-hint"></span></div>';
    order.forEach(function (key) {
      html += '<h4 class="block-title mt-4">' + esc(key) + '</h4>' +
        '<div class="file-list">' + groups[key].map(fileRow).join('') + '</div>';
    });
    return html;
  }

  /* 夹内清单的一行。三种形态：内置学生页（加入待批清单）、
     内置教师答案页（勾选 + 用这份建题库）、本会话上传件（只读）。 */
  function fileRow(it) {
    var builtin = (it.kind === 'builtin');
    var thumbSrc = builtin ? it.thumb : it.url;
    var thumb = thumbSrc
      ? '<span class="file-row__thumb"><img src="' + esc(thumbSrc) + '" alt="" loading="lazy"></span>'
      : '<span class="file-row__thumb">' + I('fileText', { size: 18 }) + '</span>';

    var action = '', pick = '', line2;
    if (builtin && it.role === 'teacher') {
      pick = '<label class="file-row__pick"><input type="checkbox" data-bank-pick="' +
        esc(it.item_id) + '"' + (BANK_PICKS[it.item_id] ? ' checked' : '') +
        ' aria-label="勾选合并建库"></label>';
      action = '<button class="btn btn--ghost btn--sm" type="button" data-demo-bank="' +
        esc(it.item_id) + '">用这份建题库</button>';
    } else if (builtin) {
      action = '<button class="btn btn--ghost btn--sm" type="button" data-demo-stage="' +
        esc(it.item_id) + '">加入待批清单</button>';
    } else if (it.kind === 'upload') {
      action = '<span class="tag tag--mine">已入工作台</span>';
    }

    if (builtin) {
      var bits = [];
      if (it.subject) bits.push(it.subject);
      bits.push(it.is_pdf ? ('PDF · ' + (it.page_count || 1) + ' 页') : '图片');
      if (it.bytes) bits.push((it.bytes / 1048576).toFixed(1) + 'MB');
      if (it.pair_titles && it.pair_titles.length) {
        bits.push((it.role === 'teacher' ? '对应学生页：' : '对应答案页：') +
                  it.pair_titles.join('、'));
      }
      line2 = bits.join(' · ');
    } else {
      var st = it.status ? (STATUS_TEXT[it.status] || it.status) : (it.graded ? '已批改' : '未批改');
      var score = (it.score != null && it.max_score != null)
        ? (it.score + '/' + it.max_score + ' · ') : '';
      line2 = (it.subject || '') + (it.question_title ? ' · ' + it.question_title : '') +
        ' · ' + score + st;
    }

    return '<div class="file-row' + (pick ? ' file-row--pick' : '') + '">' + pick + thumb +
      '<div><div class="file-row__name">' +
      esc(it.title || it.student_name || it.item_id) + '</div>' +
      '<div class="meta-line">' + esc(line2) + '</div></div>' + action + '</div>';
  }

  function syncBankPickBar() {
    var n = Object.keys(BANK_PICKS).length;
    var lbl = $('#bank-merge-label');
    var btn = $('#bank-merge-btn');
    var hint = $('#bank-pick-hint');
    if (lbl) lbl.textContent = n ? ('合并建库（已选 ' + n + ' 份）') : '合并建库';
    if (btn) btn.disabled = !n;
    if (hint) {
      hint.textContent = n
        ? '这 ' + n + ' 份会合成一套题库'
        : '先勾选要合并的答案页';
    }
  }

  /* 取一份内置样例的文件字节，包成 File——后面 stageFiles / buildBank
     吃的都是 File，于是内置样例与体验者自己上传的作业走的是同一条链路。 */
  function fetchDemoFile(it) {
    return fetch(it.url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.blob();
    }).then(function (blob) {
      var name = (it.stage_name || it.title || it.item_id) + (it.is_pdf ? '.pdf' : '.jpg');
      return new File([blob], name, { type: it.mime || blob.type || 'image/jpeg' });
    });
  }

  /** 内置样例 → 待批清单（可一次多份，顺序取文件避免同时几十个请求）。 */
  function stageDemoItems(ids) {
    var list = ids.map(function (id) { return DEMO_ITEMS[id]; })
                  .filter(function (it) { return !!it; });
    if (!list.length) return;
    var files = [];
    var chain = Promise.resolve();
    list.forEach(function (it) {
      chain = chain.then(function () {
        return fetchDemoFile(it).then(function (f) { files.push(f); });
      });
    });
    chain.then(function () {
      stageFiles(files);
      toast('已加入待批清单', list.length + ' 份内置作业已进入待批清单，可改名后再批改');
    }).catch(function (e) {
      toast('取内置样例失败', e.message);
    });
  }

  /** 内置答案页 → 建题库（多份合成一套）。 */
  function bankFromDemoItems(ids) {
    var list = ids.map(function (id) { return DEMO_ITEMS[id]; })
                  .filter(function (it) { return !!it; });
    if (!list.length) return;
    var box = $('#teacher-status');
    if (box) {
      box.innerHTML = spinner('正在取内置答案页（' + list.length + ' 份）');
      box.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'center' });
    }
    var files = [];
    var chain = Promise.resolve();
    list.forEach(function (it) {
      chain = chain.then(function () {
        return fetchDemoFile(it).then(function (f) { files.push(f); });
      });
    });
    chain.then(function () {
      buildBank(files);
    }).catch(function (e) {
      if (box) {
        box.innerHTML = '<div class="note note--red"><span class="note__ico">' +
          I('alert', { size: 18 }) + '</span><div><b>取内置答案页失败。</b><br>' +
          esc(e.message) + '</div></div>';
        Icons.hydrate(box);
      }
    });
  }

  /** 「全部加入待批清单」：把当前内置学生夹里的内置样例一次性进清单。 */
  function stageWholeFolder() {
    var meta = activeFolderMeta();
    if (!meta) return;
    var hint = $('#folder-grade-hint');
    if (hint) hint.innerHTML = '<span class="spin">' + I('loader', { size: 14 }) +
      '</span> 正在取内置作业…';
    Icons.hydrate(hint);
    api('/api/folders/' + encodeURIComponent(meta.folder_id)).then(function (d) {
      var ids = (d.items || []).filter(function (it) {
        return it.kind === 'builtin' && it.role !== 'teacher';
      }).map(function (it) {
        DEMO_ITEMS[it.item_id] = it;
        return it.item_id;
      });
      if (!ids.length) {
        if (hint) hint.textContent = '这个夹里没有内置作业可加入。';
        return;
      }
      if (hint) hint.textContent = '已加入 ' + ids.length + ' 份，见下方待批清单。';
      stageDemoItems(ids);
    }).catch(function (e) {
      if (hint) hint.textContent = '取夹内文件失败：' + e.message;
    });
  }

  function gradeFolder() {
    var meta = activeFolderMeta();
    if (!meta) return;
    // 内置学生夹：主按钮是「全部加入待批清单」，不在这里烧模型调用
    if (folderRole(meta) === 'student') { stageWholeFolder(); return; }
    if (folderRole(meta) === 'teacher') return;
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
          '<div class="toolbar" style="margin:14px 0 0;">' +
          '<button class="btn" type="button" data-goto="teacher">' +
          I('table', { size: 15 }) + '<span>去工作台审核</span></button>' +
          '<button class="btn btn--ghost" type="button" data-goto="board">' +
          '<span>看班级看板</span></button></div>';
        Icons.hydrate(note);
      }
      loadFolders();
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
          v: '同一模型再批一遍，两次吻合度作为置信度因子' }
      : { on: false, k: '二次复批',
          v: '未启用 · 置信度里的「二次批改一致性」退回模型自报值' });

    // 刻意不显示第二模型的具体型号：型号会换，界面不该跟着变；
    // 教师要知道的是「有没有第二个独立模型在复核」，不是它叫什么。
    if (!c.cross_check) {
      rows.push({ on: false, k: '交叉验证',
                  v: '未启用 · 配好大模型二即自动启用（后置防线，不占置信度权重）' });
    } else if (c.cross_budget_tight) {
      // 这条是硬警告：预算小于最快一次成功耗时，等于每次必然超时后静默丢弃。
      rows.push({ on: false, k: '交叉验证',
                  v: '大模型二 · 预算仅 ' + c.cross_timeout +
                     ' 秒，实测第二模型多需 25-90 秒，几乎必然超时后被丢弃（需调大超时预算）' });
    } else {
      rows.push({ on: true, k: '交叉验证',
                  v: '大模型二独立复核，仅黄/红件触发；分差超过满分 15% 一票否决转人工' });
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

  /* 内置样例清单（/api/demo-pages）：只用来写引擎提示与首屏拼贴，
     真正的入口在夹内清单里——同一批样例平铺两遍，翻页时会看见两次。 */
  function renderGallery(data) {
    DEMO_GROUPS = (data && data.groups) || [];
    var students = [];
    DEMO_GROUPS.forEach(function (g) {
      (g.items || []).forEach(function (it) {
        DEMO_ITEMS[it.item_id] = it;
        if (it.role !== 'teacher') students.push(it);
      });
    });

    var counts = DEMO_GROUPS.filter(function (g) { return g.role !== 'teacher'; })
      .map(function (g) { return g.name + ' ' + (g.items || []).length + ' 份'; })
      .join(' / ');
    var bank = DEMO_GROUPS.filter(function (g) { return g.role === 'teacher'; })[0];
    var bankN = bank ? (bank.items || []).length : 0;

    $('#engine-hint').innerHTML = data.vlm_configured
      ? '识别引擎：<b>多模态大模型</b>（可识别任意手写作业照片）。已内置三科<b>真实作业</b>：' +
        esc(counts) + '，另有 <b>' + bankN + '</b> 份教师答案页在「题库」夹——' +
        '点文件夹再点「查看夹内文件」即可选用。'
      : '识别引擎：<b>未配置</b>（服务端缺少多模态密钥）。内置样例是真实作业原件，' +
        '识别需要联网调用大模型，请配置 <code>ZHIPI_VLM_API_KEY</code> 后重试。';

    var mb = (CONFIG.guard && CONFIG.guard.max_image_mb) || 8;
    $('#upload-hint').innerHTML = data.vlm_configured
      ? '支持 PNG / JPG / WebP / PDF，超过 1MB 会自动压缩后上传（原图 &#8804; ' + mb + 'MB）'
      : '当前服务端未配置多模态密钥，<b>暂时无法识别任何作业照片</b>（内置样例同样需要联网识别）';

    buildCollage(students);
    updateUploadTargetHint();
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
     小于阈值的图片原样上传——内置样例打包时已压到长边 2000（多数 400-800KB），
     正好落在阈值下，于是链路上只有一次编码。 */
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

  // ——————————————————————————————————————————————————————————————
  // 教师页 → 会话题库
  // 有题库，学生作业就按教师给的标准答案逐题比对，置信度的「答案匹配度」
  // 这一维才有基准可算；没题库时该维留空、按权重重归一化剔除。
  // ——————————————————————————————————————————————————————————————
  var BANKS = [];        // 本会话已建的题库清单
  var ACTIVE_BANK = '';  // 批改学生页时选用的题库

  function isPdf(file) {
    return file.type === 'application/pdf' || /\.pdf$/i.test(file.name || '');
  }

  /* 把一份上传（图片或 PDF）交给服务端拆页并留底。
     留底是必须的：批改痕迹要画回学生自己那张纸，服务端手里得有原图。 */
  function uploadPages(file, role) {
    var maxMB = (CONFIG.guard && CONFIG.guard.max_image_mb) || 8;
    // PDF 不压缩：压的是图片像素，PDF 压不了，也不该被 canvas 重编码
    var prep = isPdf(file) ? Promise.resolve(file) : compressImage(file);
    return prep.then(function (blob) {
      if (blob.size > maxMB * 1024 * 1024) {
        throw new Error((isPdf(file) ? '这份 PDF 有 ' : '压缩后仍有 ') +
          (blob.size / 1048576).toFixed(1) + 'MB，超过 ' + maxMB + 'MB 上限。');
      }
      return blobToB64(blob).then(function (b64) {
        return api('/api/upload-pages', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_base64: b64,
            mime: blob.type || file.type || 'image/png',
            filename: file.name || '',
            role: role || 'student'
          })
        });
      });
    });
  }

  function onTeacherFiles(ev) {
    var files = Array.prototype.slice.call(ev.target.files || []);
    ev.target.value = '';
    if (!files.length) return;
    buildBank(files);
  }

  function buildBank(files) {
    var box = $('#teacher-status');
    box.innerHTML = spinner('正在读答案页并建题库');

    // 逐份上传拆页，把所有页的 page_id 汇总成一套题库。
    // 一份作业的答案常常横跨两页，分成两套题库会让学生页只能对上一半。
    var pageIds = [];
    var chain = Promise.resolve();
    files.forEach(function (f) {
      chain = chain.then(function () {
        return uploadPages(f, 'teacher').then(function (res) {
          res.pages.forEach(function (p) { pageIds.push(p.page_id); });
        });
      });
    });

    chain.then(function () {
      box.innerHTML = spinner('已上传 ' + pageIds.length + ' 页，正在逐页读标准答案');
      return api('/api/bank/build', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_ids: pageIds,
          name: files.map(function (f) {
            return (f.name || '').replace(/\.[^.]+$/, '');
          }).join(' + ').slice(0, 40)
        })
      });
    }).then(function (res) {
      ACTIVE_BANK = res.bank_id;
      box.innerHTML =
        '<div class="note note--green"><span class="note__ico">' +
        I('circleCheck', { size: 18 }) + '</span><div>题库<b>' + esc(res.name) +
        '</b>已建立：' + res.question_count + ' 道题，总分 ' + res.total_score + ' 分。' +
        (res.guessed_score_count
          ? '其中 <b>' + res.guessed_score_count + ' 道题卷面没印分值</b>，' +
            '已按题型推定，可在下方逐题修改。'
          : '全部分值取自卷面。') +
        '<br>现在上传学生作业，就会按这套标准答案逐题比对判分。</div></div>';
      return loadBanks();
    }).catch(function (e) {
      box.innerHTML =
        '<div class="note note--red"><span class="note__ico">' + I('alert', { size: 18 }) +
        '</span><div><b>建题库失败。</b><br>' + esc(e.message) + '</div></div>';
    });
  }

  function loadBanks() {
    return api('/api/bank/list').then(function (d) {
      BANKS = d.banks || [];
      if (ACTIVE_BANK && !BANKS.some(function (b) { return b.bank_id === ACTIVE_BANK; })) {
        ACTIVE_BANK = '';
      }
      renderBanks();
    }).catch(function () { /* 题库是可选项，列不出来不该打断主动线 */ });
  }

  function renderBanks() {
    var box = $('#bank-box');
    if (!box) return;
    if (!BANKS.length) { box.innerHTML = ''; closeBankDetail(); return; }

    box.innerHTML =
      '<div class="toolbar"><h3 class="block-title" style="margin:0;">本次已建题库</h3></div>' +
      '<div class="bank-list">' + BANKS.map(function (b) {
        var on = (b.bank_id === ACTIVE_BANK);
        return '<div class="bank-row' + (on ? ' is-on' : '') + '">' +
          '<label class="bank-row__pick">' +
            '<input type="radio" name="bank-pick" value="' + esc(b.bank_id) + '"' +
              (on ? ' checked' : '') + '>' +
            '<span><b>' + esc(b.name) + '</b>' +
            '<span class="hint-inline"> · ' + esc(b.subject || '学科未定') + ' · ' +
              b.question_count + ' 题 · 满分 ' + b.total_score + '</span></span>' +
          '</label>' +
          '<button class="btn btn--ghost btn--sm" type="button" data-bank-open="' +
            esc(b.bank_id) + '">查看/改分值</button>' +
          '<button class="btn btn--ghost btn--sm" type="button" data-bank-del="' +
            esc(b.bank_id) + '" aria-label="删除题库">&times;</button>' +
        '</div>';
      }).join('') + '</div>' +
      '<label class="bank-row bank-row--none' + (ACTIVE_BANK ? '' : ' is-on') + '">' +
        '<input type="radio" name="bank-pick" value=""' + (ACTIVE_BANK ? '' : ' checked') + '>' +
        '<span>不用题库（由大模型自行判分，「答案匹配度」一维留空）</span>' +
      '</label>';

    box.querySelectorAll('input[name="bank-pick"]').forEach(function (rb) {
      rb.addEventListener('change', function () {
        ACTIVE_BANK = rb.value;
        renderBanks();
      });
    });
    box.querySelectorAll('[data-bank-del]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        api('/api/bank/' + encodeURIComponent(btn.dataset.bankDel), { method: 'DELETE' })
          .then(loadBanks)
          .catch(function (e) { toast('删除失败', e.message); });
      });
    });
    box.querySelectorAll('[data-bank-open]').forEach(function (btn) {
      btn.addEventListener('click', function () { openBank(btn.dataset.bankOpen); });
    });
    // 题库被删掉时，它那份展开着的详情也要跟着关——否则页面上留着的是一套
    // 已经不存在的题目，改它的分值只会拿到 404。
    var open = openBankId();
    if (open && !BANKS.some(function (b) { return b.bank_id === open; })) closeBankDetail();
    else syncBankOpenState();
    Icons.hydrate(box);
  }

  /* 题库详情：逐题列出标准答案与分值，分值可改。
     「系统推定」的分值必须能改——不能改的话，推定就成了教师无法反驳的判断。

     展开态记在 #bank-detail 的 dataset 上，而不是模块变量里：详情的 DOM 与状态
     同生共死，任何一方被清掉都不会留下「按钮说展开着、下面却是空的」的残影。 */
  function openBankId() {
    var box = $('#bank-detail');
    return (box && box.dataset.openBank) || '';
  }

  function closeBankDetail() {
    var box = $('#bank-detail');
    if (!box) return;
    delete box.dataset.openBank;
    box.innerHTML = '';
    syncBankOpenState();
  }

  /* 按钮既是入口也是收起开关，文案与 aria 必须跟着展开态走——
     不然展开后它还写着「查看/改分值」，等于告诉用户再点一次会再展开一遍。 */
  function syncBankOpenState() {
    var open = openBankId();
    $$('[data-bank-open]').forEach(function (btn) {
      var on = btn.dataset.bankOpen === open;
      btn.textContent = on ? '收起' : '查看/改分值';
      btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
  }

  function openBank(bankId) {
    var box = $('#bank-detail');
    if (!box) return;
    if (openBankId() === bankId) { closeBankDetail(); return; }
    box.dataset.openBank = bankId;
    syncBankOpenState();
    box.innerHTML = spinner('读取题库');
    api('/api/bank/' + encodeURIComponent(bankId)).then(function (bk) {
      if (openBankId() !== bankId) return;   // 请求飞在路上时用户点了收起 / 换了一套
      box.innerHTML =
        '<div class="bank-detail">' +
        '<p class="hint">标注「系统推定」的分值是按题型推的（卷面没印分值），可直接修改。</p>' +
        bk.questions.map(function (q) {
          return '<div class="bank-q">' +
            '<span class="bank-q__no">' + esc(q.no || q.qid) + '</span>' +
            '<span class="bank-q__body">' +
              '<span class="bank-q__stem">' + esc((q.stem || '').slice(0, 80)) + '</span>' +
              '<span class="bank-q__ans">标准答案：' +
                esc((q.standard_answer || '（答案页此题为空）').slice(0, 80)) + '</span>' +
            '</span>' +
            '<span class="bank-q__type">' + esc(q.qtype) + '</span>' +
            '<input class="input input--sm bank-q__score" type="number" min="0.5" max="150" ' +
              'step="0.5" value="' + q.max_score + '" data-qid="' + esc(q.qid) + '" ' +
              'aria-label="' + esc(q.no || q.qid) + ' 分值">' +
            '<span class="bank-q__src' + (q.score_source === 'default' ? ' is-guess' : '') + '">' +
              (q.score_source === 'printed' ? '卷面印的'
                : q.score_source === 'teacher' ? '教师已改' : '系统推定') +
            '</span>' +
          '</div>';
        }).join('') + '</div>';

      box.querySelectorAll('.bank-q__score').forEach(function (inp) {
        inp.addEventListener('change', function () {
          var val = Number(inp.value);
          if (!(val > 0)) { toast('分值无效', '分值需要大于 0'); return; }
          api('/api/bank/' + encodeURIComponent(bankId) + '/score', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ qid: inp.dataset.qid, max_score: val })
          }).then(function (d) {
            var src = inp.parentNode.querySelector('.bank-q__src');
            if (src) { src.textContent = '教师已改'; src.classList.remove('is-guess'); }
            toast('分值已更新', '这套题库现在满分 ' + d.total_score + ' 分');
            loadBanks();
          }).catch(function (e) { toast('改分值失败', e.message); });
        });
      });
    }).catch(function (e) {
      if (openBankId() !== bankId) return;
      box.innerHTML = '<p class="hint">题库读取失败：' + esc(e.message) + '</p>';
    });
  }

  // ——————————————————————————————————————————————————————————————
  // 暂存区：文件选好后先进暂存，用户改名后再手动触发识别/批改。
  // 这样文件名就是学生姓名，不再统一显示「上传作业」。
  // ——————————————————————————————————————————————————————————————
  var STAGED_FILES = [];  // [{file, name, blobUrl}]
  var NAME_MAX = 20;      // 与后端 _clean_name 的截断长度一致

  function stageFiles(files) {
    files.forEach(function (f) {
      // 去扩展名当默认名，并截到后端会保留的长度。不截的话，微信 / 相机的
      // 机器文件名（32 位十六进制之类）会让用户看到一个自己没打过的长名字，
      // 而真正用上的只有前 20 个字符。
      var raw = f.name.replace(/\.[^.]+$/, '').slice(0, NAME_MAX);
      STAGED_FILES.push({
        file: f, name: raw, pdf: isPdf(f),
        // PDF 没法用 <img> 直接显示缩略图，列表里改用一个文件角标
        blobUrl: isPdf(f) ? '' : URL.createObjectURL(f)
      });
    });
    renderStaging();
  }

  /* 「全部增强批改」按钮：配了强模型才给——按钮在而功能不在，比没有按钮更糟。
     单独抽出来是因为 config 是异步回来的，可能晚于用户选文件；那时暂存区已经
     渲染过一轮，只补这个按钮即可，重跑整个 renderStaging 会把页面滚回暂存区。 */
  function syncStageStrongBtn() {
    var sBtn = $('#stage-grade-strong-btn');
    var sLbl = $('#stage-grade-strong-label');
    if (!sBtn) return;
    var cfg = CONFIG.strong_recognize || {};
    sBtn.style.display = cfg.available ? '' : 'none';
    sBtn.title = '每份都用 ' + (cfg.model || '强模型') +
      ' 识别：比常规准，但每份约 1 分钟，' + STAGED_FILES.length + ' 份要等更久';
    if (sLbl) sLbl.textContent = '全部增强批改（' + STAGED_FILES.length + ' 份）';
  }

  function renderStaging() {
    var box   = $('#stage-box');
    var list  = $('#stage-list');
    var title = $('#stage-title');
    var gBtn  = $('#stage-grade-btn');
    var gLbl  = $('#stage-grade-label');
    var sBtn  = $('#stage-grade-strong-btn');
    var sLbl  = $('#stage-grade-strong-label');
    var cBtn  = $('#stage-clear-btn');
    if (!box) return;

    if (!STAGED_FILES.length) { box.style.display = 'none'; return; }
    box.style.display = '';
    if (title) title.textContent = '待批清单 · 共 ' + STAGED_FILES.length + ' 份';
    if (gLbl)  gLbl.textContent  = '全部批改（' + STAGED_FILES.length + ' 份）';
    syncStageStrongBtn();

    // 渲染每一份的缩略图 + 改名输入框 + 单独批改/移除按钮
    list.innerHTML = STAGED_FILES.map(function (it, idx) {
      return '<div class="stage-item">' +
        (it.pdf
          ? '<span class="stage-item__thumb stage-item__thumb--pdf" ' +
              'data-icon="fileText" data-icon-size="20" title="PDF · 批改时按页拆开"></span>'
          : '<img class="stage-item__thumb" src="' + esc(it.blobUrl) + '" alt="">') +
        '<input class="input input--sm stage-item__name" type="text" ' +
          'placeholder="学生姓名" value="' + esc(it.name) + '" maxlength="' + NAME_MAX + '" ' +
          'data-idx="' + idx + '" aria-label="学生姓名">' +
        '<button class="btn btn--primary btn--sm" type="button" data-grade-idx="' + idx + '">' +
          '批改</button>' +
        '<button class="btn btn--ghost btn--sm stage-item__rm" type="button" ' +
          'data-rm-idx="' + idx + '" aria-label="移除">&times;</button>' +
      '</div>';
    }).join('');
    Icons.hydrate(box);

    // 同步改名到数组
    var nameInputs = list.querySelectorAll('.stage-item__name');
    nameInputs.forEach(function (inp) {
      inp.addEventListener('input', function () {
        var i = Number(inp.dataset.idx);
        if (STAGED_FILES[i]) STAGED_FILES[i].name = inp.value;
      });
    });
    // 移除单份
    list.querySelectorAll('[data-rm-idx]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var i = Number(btn.dataset.rmIdx);
        if (STAGED_FILES[i]) URL.revokeObjectURL(STAGED_FILES[i].blobUrl);
        STAGED_FILES.splice(i, 1);
        renderStaging();
      });
    });
    // 单份批改
    list.querySelectorAll('[data-grade-idx]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var i = Number(btn.dataset.gradeIdx);
        var it = STAGED_FILES[i];
        if (!it) return;
        URL.revokeObjectURL(it.blobUrl);
        STAGED_FILES.splice(i, 1);
        renderStaging();
        // 名字清空就不回退到原始文件名——机器文件名不是姓名，
        // 让后端用默认的「上传作业」比显示一串十六进制强
        handleFileWithName(it.file, it.name);
      });
    });

    // 全部批改（只绑一次，通过 onclick 覆写避免重复绑定）
    if (gBtn) gBtn.onclick = function () { gradeAllStaged(false); };
    if (sBtn) sBtn.onclick = function () { gradeAllStaged(true); };
    // 清空
    if (cBtn) cBtn.onclick = function () {
      STAGED_FILES.forEach(function (it) { URL.revokeObjectURL(it.blobUrl); });
      STAGED_FILES = [];
      renderStaging();
    };

    box.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'nearest' });
  }

  /* 清空暂存区，把这一批送去批改。strong=true 时整批改用强模型识别。

     为什么批量需要单独的入口：单份路径上的「字迹潦草？换强模型重读」是批完之后
     的补救，一叠字迹都潦草的作业按那条路走，得等常规批改跑完再一份份点回去重读，
     且第一遍的错判已经进了工作台。所以在送批前给一次选择。 */
  function gradeAllStaged(strong) {
    var copy = STAGED_FILES.slice();
    if (!copy.length) return;
    STAGED_FILES.forEach(function (it) { URL.revokeObjectURL(it.blobUrl); });
    STAGED_FILES = [];
    renderStaging();
    if (copy.length === 1) {
      handleFileWithName(copy[0].file, copy[0].name, strong);
    } else {
      var renamed = copy.map(function (it) {
        it.file._stageName = it.name;
        return it.file;
      });
      runBatch(renamed, strong);
    }
  }

  // 带自定义学生名的单文件识别入口
  function handleFileWithName(file, studentName, strong) {
    var sheet = $('#recog-sheet');
    sheet.style.display = '';
    $('#recog-result').style.display = 'none';
    $('#recog-status').innerHTML = spinner(isPdf(file) ? '正在拆页' : '正在处理图片');
    sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });

    // 照片和 PDF 走同一条路：都先交给服务端留底并拆页。
    // 照片其实只有一页，但仍然要留底——批改痕迹要画回学生自己那张纸，
    // 服务端手里得有原图。以前照片是直接把 base64 送去识别、不留底的，
    // 于是批完就没法在原图上留痕。
    uploadPages(file, 'student').then(function (res) {
      if (!res.pages.length) { showRecogError('这份文件没有可处理的页'); return; }
      if (res.pages.length === 1) {
        recognizePage(res.pages[0],
                      res.kind === 'pdf' ? 'PDF 已拆页（共 1 页）' : '', studentName, strong);
      } else {
        openPdfPicker(file.name || '学生作业', res, studentName, strong);
      }
    }).catch(function (e) { showRecogError(e.message); });
  }

  /* PDF 拆出多页时让用户先选页。也给「全部批改」——一本作业册逐页点，
     每点一次等一轮，是把一次批改变成排队。全批走批量链路，每页一份结果。 */
  function openPdfPicker(name, res, studentName, strong) {
    var allLabel = (strong ? '全部增强批改（' : '全部批改（') + res.page_total + ' 页）';
    $('#recog-status').innerHTML =
      '<div class="note"><span class="note__ico">' + I('fileText', { size: 18 }) +
      '</span><div><b>' + esc(name) + '</b> 共 ' + res.page_total + ' 页，已自动拆页。' +
      '<br>可整本全批，也可先点选其中一页。</div></div>' +
      '<div class="toolbar" style="margin:12px 0 0;">' +
      '<button class="btn btn--accent btn--sm" type="button" id="pdf-all-btn">' +
      I('layers', { size: 14 }) + '<span>' + esc(allLabel) + '</span></button>' +
      '</div>' +
      '<div class="pdf-pages">' +
      res.pages.map(function (p, i) {
        return '<button class="pdf-page" type="button" data-pdf-i="' + i + '">' +
          '<img src="' + esc(p.url) + '" alt="第 ' + p.page_no + ' 页">' +
          '<span>第 ' + p.page_no + ' 页</span></button>';
      }).join('') + '</div>';

    var status = $('#recog-status');
    var allBtn = $('#pdf-all-btn');
    if (allBtn) {
      allBtn.addEventListener('click', function () {
        status.innerHTML = '';
        runBatchPages(res.pages, studentName || name, strong);
      });
    }
    status.querySelectorAll('[data-pdf-i]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var page = res.pages[Number(btn.dataset.pdfI)];
        status.innerHTML = '';
        recognizePage(page, '第 ' + page.page_no + ' / ' + res.page_total + ' 页',
                      studentName, strong);
      });
    });
    Icons.hydrate(status);
  }

  function onFileChosen(ev) {
    var files = Array.prototype.slice.call(ev.target.files || []);
    ev.target.value = '';
    if (!files.length) return;
    stageFiles(files);   // 先进暂存，不自动识别
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

  function runBatch(files, strong) {
    if (files.length > BATCH_MAX) {
      alert('单次最多 ' + BATCH_MAX + ' 份，已选 ' + files.length +
        ' 份。请拆成多次上传。');
      return;
    }
    var list = files.map(function (f) {
      // 未经暂存区的直传（拖入）也要截，f.name 这里是带扩展名的原始文件名
      return { file: f, name: (f._stageName || f.name).slice(0, NAME_MAX),
               status: 'queued', text: '待处理' };
    });
    startBatch(list, strong);
  }

  /* PDF 全批：拆好的每一页各算一份，送进同一条批量链路。
     入口在拆页选择界面上——那里本来只能挑一页批，一份 12 页的作业册
     要点 12 次、每次等一轮。 */
  function runBatchPages(pages, baseName, strong) {
    if (pages.length > BATCH_MAX) {
      alert('单次最多 ' + BATCH_MAX + ' 页，这份共 ' + pages.length +
        ' 页。请逐页批，或拆分 PDF 后再传。');
      return;
    }
    var list = pages.map(function (p) {
      return { page: p, name: pageItemName(baseName, p),
               status: 'queued', text: '待处理' };
    });
    startBatch(list, strong);
  }

  /* 页名 = 文件名 + 页号。先给页号留位再截文件名：直接截整串的话，
     名字长一点的作业每页都会截成同一个前缀，工作台里十几份同名，分不出谁是谁。 */
  function pageItemName(baseName, page) {
    var suffix = ' · 第 ' + page.page_no + ' 页';
    var base = (baseName || '作业').slice(0, Math.max(1, NAME_MAX - suffix.length));
    return base + suffix;
  }

  function startBatch(list, strong) {
    var token = ++BATCH_TOKEN;
    var sheet = $('#batch-sheet');
    sheet.style.display = '';
    $('#recog-sheet').style.display = 'none';
    $('#batch-done').style.display = 'none';
    sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });
    renderBatch(list);
    processBatch(list, 0, token, strong);
  }

  function renderBatch(list) {
    var done = list.filter(function (it) { return it.status === 'ok' || it.status === 'fail'; }).length;
    var pct = Math.round(done / list.length * 100);
    /* 「可以走开」必须写在界面上，否则教师只会盯着进度条等。
       超时预算已经按离开式批改放宽（识别 300s / 强模型 900s / 批改 300s），
       一份作业走完整条链路是分钟级的——不说清楚，慢就会被当成卡死。
       只在还没批完时提示；推送那半句仅在真配了 webhook 时说，
       否则就是承诺一个不存在的通知。 */
    var waitHint = done < list.length
      ? '<span class="hint"> · 每份通常 1-2 分钟，可以离开页面'
        + (CONFIG && CONFIG.feishu_webhook_configured
           ? '，全部批完会自动推送飞书审核卡片' : '')
        + '</span>'
      : '';
    $('#batch-progress').innerHTML =
      '已处理 <b class="mono">' + done + ' / ' + list.length + '</b> 份' + waitHint;
    $('#batch-bar-fill').style.width = pct + '%';
    $('#batch-list').innerHTML = list.map(function (it, i) {
      var cls = 'batch-item';
      if (it.status === 'ok') cls += ' is-done';
      if (it.status === 'fail') cls += ' is-fail';
      if (it.status === 'working') cls += ' is-busy';
      var detail = it.html
        ? '<span class="batch-item__detail">' + it.html + '</span>'
        : '<span class="batch-item__detail">' + esc(it.text) + '</span>';
      return '<div class="' + cls + '">' +
        '<span class="mono">' + (i + 1) + '</span>' +
        '<span class="batch-item__name">' + esc(it.name) + '</span>' +
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
      '<div><b>批量完成</b>：成功 ' + ok + ' 份' +
      (fail ? '，失败 ' + fail + ' 份' : '') +
      '。已进入当前文件夹与教师工作台，按红黄绿置信度分流——<b>优先审红、黄桶</b>。</div>' +
      '</div>' +
      '<div class="toolbar" style="margin:14px 0 0;">' +
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
        '<label for="batch-result-select">切换查看</label>' +
        '<select id="batch-result-select" class="select"' +
          ' aria-label="切换查看本批次的批改结果">' + opts + '</select>' +
        '<span class="hint-inline">本批共 ' + okItems.length + ' 份</span>' +
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

  /* 送批载荷：识别阶段产出的元信号必须跟着题目一起回传。

     这里以前在 processBatch 与 gradePaper 里各手写了一份字段清单，识别侧新增的
     字段没人记得同步两处——qtype / overflow / attribution_confidence 加上之后，
     服务端 _clean_page_questions 建好了接收位、批改端写好了消费逻辑，前端却始终
     没传，于是三阶段识别标出来的越界作答与归属把握在批改层恒为缺省值，等于没做。
     抽成一处，以后识别侧加字段只改这里。 */
  function gradeItem(q, answerOverride) {
    return {
      no: q.no || '', subject: q.subject || '', stem: q.stem || '',
      answer: answerOverride !== undefined ? answerOverride : (q.answer || ''),
      printed_max_score: q.printed_max_score || null,
      bbox: q.bbox || null,
      // 学生作答的位置：批改痕迹的勾叉画在这里，不传就只能画在页边
      answer_box: q.answer_box || null,
      // 题型决定这道题该不该看解题步骤，客观题只核对答案
      qtype: q.qtype || '',
      // 越界作答与归属把握：这道题的转写可不可信
      overflow: !!q.overflow,
      attribution_confidence: typeof q.attribution_confidence === 'number'
        ? q.attribution_confidence : null,
      // 定向复识留痕：改写过的题，以及模型自己都说看不清的题
      refined: !!q.refined,
      refine_changed: !!q.refine_changed,
      refine_reason: q.refine_reason || '',
      legible_hint: q.legible_hint === false ? false : null
    };
  }


  function processBatch(list, idx, token, strong) {
    if (idx >= list.length) return;
    var it = list[idx];
    it.status = 'working';
    it.text = strong ? '强模型识别中（1-2 分钟）' : '识别中';
    renderBatch(list);

    function next() {
      if (token === BATCH_TOKEN) processBatch(list, idx + 1, token, strong);
    }

    // 与单份路径同一条链路：先留底拆页，再识别，再整页批改。
    // 批量以前走的是「一份 = 一道题」的 grade-image，于是批量上传的作业
    // 既没有批改痕迹、也用不上题库——同一个动作在两条入口里结果不一样。
    //
    // it.page 已经是拆好的页（PDF 全批入口、或上一项展开时插进来的），不必再传一次。
    var ready = it.page
      ? Promise.resolve(it.page)
      : uploadPages(it.file, 'student').then(function (up) {
          if (token !== BATCH_TOKEN) return null;
          var pages = up.pages || [];
          if (!pages.length) throw new Error('没有可处理的页');
          if (pages.length > 1) expandPdfPages(list, idx, it, pages);
          return pages[0];
        });

    ready.then(function (page) {
      if (token !== BATCH_TOKEN || !page) return;
      return api('/api/recognize-image', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page_id: page.page_id, strong: !!strong })
      }).then(function (r) {
        if (token !== BATCH_TOKEN) return;
        if (r.engine === 'none') {
          it.status = 'fail';
          it.text = '未能识别：离线演示模式只识别下方内置样例';
          it.html = RECOG_GUIDE[r.reason] || RECOG_GUIDE.no_match;
          renderBatch(list);
          next();
          return;
        }
        var qs = r.questions || [];
        if (!qs.length) {
          it.status = 'fail';
          it.text = '未能提取题面';
          renderBatch(list);
          next();
          return;
        }
        it.text = '批改中（' + qs.length + ' 道题）';
        renderBatch(list);
        return api('/api/grade-page', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            page_id: page.page_id,
            questions: qs.map(function (q) { return gradeItem(q); }),
            subject: r.subject || undefined,
            clarity: r.clarity,
            bank_id: ACTIVE_BANK || undefined,
            student_name: it.name || undefined,
            engine: r.engine,
            folder_id: ACTIVE_FOLDER || undefined
          })
        }).then(function (res) {
          if (token !== BATCH_TOKEN) return;
          it.status = 'ok';
          it.result = res;   // 保留该份完整批改结果，供批量卡片就地展开证据链
          it.text = res.total_score + ' / ' + res.max_score + ' 分 · 置信度 ' +
            Math.round(res.confidence) + ' · ' + esc(STATUS_TEXT[res.status] || res.status);
          // 批量是降级最容易发生的地方（连打几十次，网关一抖就掉到单次路径），
          // 也是最难发现的地方——列表里只有分数，没人会去点开看识别详情。
          // 这一份的题库匹配大概率偏差，标出来让教师知道该重跑哪几份。
          if (r.staged === false && r.staged_error) {
            it.text += ' · ⚠ 已降级识别';
          }
          if (idx === list.length - 1 && token === BATCH_TOKEN) CURRENT = res;
          renderBatch(list);
          next();
        });
      });
    }).catch(function (e) {
      it.status = 'fail';
      it.text = '失败：' + e.message;
      renderBatch(list);
      next();
    });
  }

  /* 一份多页 PDF = 多份作业：第 2 页起就地插到当前项后面，逐页批完。
     以前批量只批第 1 页并写一句「本次只批第 1 页」，一本 12 页的作业册
     批完只有 1 页有结果，其余 11 页既没进工作台也无从补批。
     超出单次上限的部分如实说明，不静默丢。 */
  function expandPdfPages(list, idx, it, pages) {
    var base = it.name;
    it.name = pageItemName(base, pages[0]);
    it.page = pages[0];
    var rest = pages.slice(1);
    var room = Math.max(0, BATCH_MAX - list.length);
    var dropped = Math.max(0, rest.length - room);
    if (dropped) rest = rest.slice(0, room);
    var extra = rest.map(function (p) {
      return { page: p, name: pageItemName(base, p), status: 'queued', text: '待处理' };
    });
    list.splice.apply(list, [idx + 1, 0].concat(extra));
    // 立刻刷新清单：不刷的话，新增的页要等当前这页批完（十几秒起）才冒出来，
    // 进度条会先显示 0/1 再跳成 0/12，看着像卡住了又自己变了。
    renderBatch(list);
    if (dropped) {
      toast('本次少批 ' + dropped + ' 页',
            base + ' 共 ' + pages.length + ' 页，单次上限 ' + BATCH_MAX +
            ' 份，后 ' + dropped + ' 页没有批改，请另起一批。');
    }
  }

  /* 识别不了是公开体验里最常见的一步：必须给出路，而不是甩一句「失败」。
     内置样例现在也是真实作业原件，同样要联网识别，所以这里没有「换成样例
     就能跑」的兜底可以给——只能如实说清楚是哪一环出的问题、还能做什么。 */
  var RECOG_GUIDE = {
    vlm_not_configured:
      '服务端还没有配置多模态识别密钥（<code>ZHIPI_VLM_API_KEY</code>）。' +
      '识别与批改都要联网调用大模型，内置的三科真实作业也不例外。<br>' +
      '自行部署时在环境变量里配好密钥后重启即可；' +
      '②③④ 三屏用的是内置基线数据，现在就能看。',
    vlm_unavailable:
      '真实识别暂时不可用：公开体验的每日额度已用尽。<br>' +
      '可以明天再来，或自行部署配置自己的密钥；' +
      '②③④ 三屏的工作台、班级看板与错因画像不受影响，现在就能看。',
    vlm_failed:
      '多模态识别调用失败，可能是网络或上游服务波动。<br>' +
      '可稍后重试；若这张照片字迹特别潦草或拍得很斜，' +
      '换一份内置样例先跑通链路，再回头调这张。'
  };

  function recognize(b64, mime, note, studentName) {
    return recognizeWith({ image_base64: b64, mime: mime }, note, studentName);
  }

  /* 按已上传的页图识别（PDF 拆页后的走法）。
     服务端手里留着那一页的原图，批改完才画得回去。 */
  /* 「字迹潦草？换强模型重读」按钮。只在服务端确实配了强模型时出现——
     按钮在而功能不在，比没有按钮更糟。已经是强模型的结果就不再提供。 */
  function renderStrongButton(r) {
    var box = $('#strong-recog-box');
    if (!box) return;
    var cfg = CONFIG.strong_recognize || {};
    var show = cfg.available && r && r.engine === 'vlm' && !r.strong;
    box.style.display = show ? '' : 'none';
    if (!show) return;
    box.innerHTML =
      '<button class="btn btn--sm" type="button" id="strong-recog-btn">' +
        I('scan', { size: 14 }) + '<span>字迹潦草？换强模型重读</span></button>' +
      '<span class="hint" style="margin-left:8px">' +
        esc(cfg.model || '强模型') + '，约 1 分钟，比常规识别准但慢</span>';
    Icons.hydrate(box);
    var btn = $('#strong-recog-btn');
    if (btn) btn.addEventListener('click', strongRecognize);
  }


  function recognizePage(page, note, studentName) {
    var img = $('#chosen-img');
    if (img) img.src = page.url;
    return recognizeWith({ page_id: page.page_id }, note, studentName);
  }

  /* 强模型重识别：把最近一次识别用过的那份入参原样再跑一遍，只多带 strong。

     为什么做成按钮而不是自动：强模型一次调用 60 秒上下，是常规档的十倍，
     整体默认打开会把演示拖垮。而「这份字迹潦草」恰恰是教师一眼就能判断、
     系统很难自知的事（模型读错时的自报置信度与读对时一样高），所以把开关
     交给人——看着不对就点一下重读。 */
  var LAST_RECOG_BODY = null;

  function strongRecognize() {
    if (!LAST_RECOG_BODY) return;
    var btn = $('#strong-recog-btn');
    if (btn && btn.disabled) return;
    if (btn) btn.disabled = true;
    var body = {};
    Object.keys(LAST_RECOG_BODY).forEach(function (k) { body[k] = LAST_RECOG_BODY[k]; });
    body.strong = true;
    recognizeWith(body, '强模型重识别', RECOG && RECOG.student_name)
      .catch(function () { })
      .then(function () { if (btn) btn.disabled = false; });
  }

  function recognizeWith(body, note, studentName) {
    RECOG = null;
    LAST_RECOG_BODY = body;
    var strong = !!body.strong;
    var sheet = $('#recog-sheet');
    sheet.style.display = '';
    $('#recog-result').style.display = 'none';
    $('#recog-status').innerHTML = spinner(strong
      ? '正在用强模型重新识别（实测整页 1-2 分钟，可以先做别的）'
      : '正在识别手写内容（整页三阶段，通常 1 分钟出头）');
    sheet.scrollIntoView({ behavior: M.reduced ? 'auto' : 'smooth', block: 'start' });

    return api('/api/recognize-image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (r.engine === 'none') {
        $('#recog-status').innerHTML =
          '<div class="note note--amber"><span class="note__ico">' + I('alert', { size: 18 }) +
          '</span><div><b>未能识别这张照片。</b><br>' +
          (RECOG_GUIDE[r.reason] || RECOG_GUIDE.vlm_failed) +
          '<div class="mt-4"><button class="btn btn--sm" type="button" data-goto="teacher">' +
          I('table', { size: 14 }) + '<span>先去看教师工作台</span>' +
          I('arrowRight', { size: 14 }) + '</button></div>' +
          (r.error ? '<p class="hint mt-4">技术详情：' + esc(r.error) + '</p>' : '') +
          '</div></div>';
        return;
      }
      RECOG = r;
      if (studentName) RECOG.student_name = studentName;
      $('#recog-status').innerHTML = '';
      $('#recog-result').style.display = '';

      var badges = [];
      badges.push('<span class="stamp stamp--quiet">' + I('scan', { size: 13 }) + ' ' +
        esc(ENGINE_TEXT[r.engine] || r.engine) + '</span>');
      var clarityCls = r.clarity >= 85 ? 'factor--ok' : (r.clarity >= 60 ? 'factor--warn' : 'factor--bad');
      badges.push('<span class="factor ' + clarityCls + '">卷面清晰度 <b>' + r.clarity + '</b></span>');
      if (r.question_id) {
        badges.push('<span class="factor factor--ok">自动判题 <b>' +
          esc(r.subject || '') + ' · ' + esc(r.question_title || '') + '</b></span>');
      } else if (r.subject) {
        // 题库外作业：学科由模型在 K12 白名单内归类。以前这里不显示任何东西，
        // 老师看不到「系统认为这是哪一科」，判错了也无从发现。
        badges.push('<span class="factor factor--ok">学科归类 <b>' +
          esc(r.subject) + '</b></span>');
        badges.push('<span class="factor">判分口径 <b>体系判别分 15</b></span>');
        if (r.printed_max_score) {
          badges.push('<span class="factor">卷面分值 <b>' + r.printed_max_score + '</b></span>');
        }
      }
      // 一张图多道题：如实告知只批了第一道，不要让另外几道悄悄消失
      if (r.question_count > 1) {
        badges.push('<span class="factor factor--warn">检测到 <b>' + r.question_count +
          '</b> 道题</span>');
      }
      if (note) badges.push('<span class="factor">' + esc(note) + '</span>');
      if (r.quota_note) badges.push('<span class="factor factor--warn">' + esc(r.quota_note) + '</span>');
      // 三阶段识别挂了、降级走了单次这条备用路。以前这件事只存在于返回值里
      // （staged_error），日志不打、界面不显示，于是「这一页为什么大量题对不上
      // 题库」在任何一处都查不到——教师以为是题库没建好。降级出来的转写确实
      // 更差：实测英语完形填空会把相邻几道题的题干读成同一段，只有一道能配上。
      if (r.staged === false && r.staged_error) {
        badges.push('<span class="factor factor--warn">已降级为备用识别</span>');
      }
      if (r.strong) {
        badges.push('<span class="factor factor--ok">强模型识别 <b>' +
          esc(r.model_tier || '') + '</b></span>');
      }
      if (r.strong_error) {
        // 强模型挂了会静默回落常规链路。不说的话，教师以为自己点的那一下生效了，
        // 看到的却还是同一份转写。
        badges.push('<span class="factor factor--warn">强模型未成功，已回落常规识别</span>');
      }
      $('#recog-badges').innerHTML = badges.join('');
      renderStrongButton(r);

      renderPaperList(r);

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

  /* 一图多题 / 整页作业：把认出来的每道题都列出来，并给出「批改整页」入口。
     单题时整块隐藏，界面与原来完全一致——绝大多数单张照片只有一道题，
     不该为了整页场景给所有人多加一块东西。 */
  function renderPaperList(r) {
    var box = $('#paper-sheet');
    var btn = $('#paper-grade-btn');
    var qs = (r && r.questions) || [];
    // 有 page_id 就能整页批改，哪怕只认出一道题（整页批改的口径也更好）
    var canPage = !!(r && r.page_id) && qs.length >= 1;
    if (!box || qs.length < 2) {
      if (box) { box.style.display = 'none'; box.innerHTML = ''; }
      if (btn) btn.style.display = canPage && qs.length === 1 ? '' : 'none';
      if (canPage && qs.length === 1) {
        $('#paper-grade-label').textContent = '批改整页（1 道）';
      }
      return;
    }

    // 记号的落点：优先贴着学生作答，其次贴着题目框，与 marks._anchor 同口径
    var onAnswer = qs.filter(function (q) { return !!q.answer_box; }).length;
    var located = qs.filter(function (q) { return !!(q.answer_box || q.bbox); }).length;
    var rows = qs.map(function (q) {
      var no = '<span class="paper-q__no">' + esc(q.no || q.index) + '</span>';
      var score = q.printed_max_score
        ? '<span class="paper-q__pm">卷面 ' + q.printed_max_score + ' 分</span>' : '';
      return '<label class="paper-q">' +
        no +
        '<span class="paper-q__body">' +
          '<span class="paper-q__t">' + esc(q.subject || '') + ' · ' +
            esc(q.title || ('第 ' + q.index + ' 题')) + '</span>' +
          '<span class="paper-q__s">' + esc((q.answer || '').slice(0, 60) || '（未识别到作答）') +
          '</span>' +
        '</span>' + score +
        '</label>';
    }).join('');

    box.style.display = '';
    box.innerHTML =
      '<div class="paper-head">' +
        '<b>这一页认出 ' + qs.length + ' 道题</b>' +
        '<span class="hint-inline">「批改整页」一次批完全部题目，勾叉直接画在学生作答旁；' +
        '下方转写框只对应第 1 题</span>' +
      '</div>' +
      // 定位不到位置的题，痕迹会画在页边并标上题号。说清楚，教师才知道
      // 那几个记号为什么不贴着题目——而不是以为系统认错了题。
      (located < qs.length
        ? '<p class="hint">其中 ' + (qs.length - located) + ' 道题未能定位到页面位置，' +
          '批改痕迹将画在页边并标注题号。</p>'
        : (onAnswer < qs.length
          ? '<p class="hint">其中 ' + (qs.length - onAnswer) + ' 道题只定位到题目、' +
            '未定位到作答位置，这几处记号会画在题目右侧而不是答案旁。</p>'
          : '')) +
      '<div class="paper-list">' + rows + '</div>';

    if (btn) {
      btn.style.display = canPage ? '' : 'none';
      $('#paper-grade-label').textContent = '批改整页（' + qs.length + ' 道）';
    }
    Icons.hydrate(box);
  }

  /* 整页批改：一次调用批完这一页的全部题目，并在学生原图上留痕。

     这里以前是「一道题一次请求」的串行循环，还配了一个「本次最多批 8 道」的
     上限。改成整页一次调用之后，成本与等待不再随题数线性膨胀，模型也终于
     能看到同一页上各题之间的关系（第 (2) 问用第 (1) 问的结论、一段短文挂
     五个小题），于是上限和循环一起去掉了。 */
  function gradePaper() {
    if (!RECOG || !RECOG.page_id) return;
    var qs = RECOG.questions || [];
    if (!qs.length) return;
    var btn = $('#paper-grade-btn');
    if (btn && btn.disabled) return;  // 防双击重入
    if (btn) btn.disabled = true;

    var hint = $('#grade-hint');
    hint.innerHTML = '<span class="spin">' + I('loader', { size: 14 }) + '</span> 整页批改中（' +
      qs.length + ' 道题一次批完）';

    // 教师若在转写框里改过第 1 题，改动要真实生效——这是「教师可控」的落点
    var edited = $('#ocr-text').value;
    var payload = qs.map(function (q, i) {
      // 只有「这页就一道题」时，转写框里的编辑才对得上第 1 题
      return gradeItem(q, (i === 0 && edited && qs.length === 1) ? edited : undefined);
    });

    api('/api/grade-page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page_id: RECOG.page_id,
        questions: payload,
        subject: RECOG.subject || undefined,
        clarity: RECOG.clarity,
        bank_id: ACTIVE_BANK || undefined,
        student_name: RECOG.student_name || undefined,
        engine: RECOG.engine,
        folder_id: ACTIVE_FOLDER || undefined
      })
    }).then(function (res) {
      BATCH_LIST = null;
      CURRENT = res;
      hint.textContent = '';
      if (btn) btn.disabled = false;
      renderResult();
      switchTab('result');
      window.scrollTo({ top: 0, behavior: M.reduced ? 'auto' : 'smooth' });
      var meta = activeFolderMeta();
      autoPushFeishu((meta ? meta.name + ' · ' : '') +
        (res.student_name || '上传作业') + ' 整页批改完成');
      loadFolders();
      loadTeacher();
    }).catch(function (e) {
      hint.textContent = '整页批改失败：' + e.message;
      if (btn) btn.disabled = false;
    });
  }

  function submitImageGrade() {
    if (!RECOG) return;
    var hint = $('#grade-hint');
    hint.innerHTML = '<span class="spin">' + I('loader', { size: 14 }) + '</span> 批改中';
    var body;
    if (RECOG.engine === 'vlm') {
      // 一律按「当前转写文本」批改，而不是识别时的原始输出——
      // 否则教师对转写的修正就成了摆设（教师可控原则）
      body = {
        ocr_text: $('#ocr-text').value,
        ocr_clarity: RECOG.clarity,
        engine: RECOG.engine,
        folder_id: ACTIVE_FOLDER || undefined
      };
      if (RECOG.question_id) {
        body.question_id = RECOG.question_id;
      } else if (RECOG.question_text) {
        // 题库外的真实作业：拿题面 + 学科走五维度判别分体系。
        // 这里以前要求必须命中题库，命中不了就报「未能判定题目」——
        // 而删掉题库强行匹配后 question_id 恒为 null，等于整条上传动线断死。
        body.stem = RECOG.question_text;
        body.subject = RECOG.subject || undefined;
        body.printed_max_score = RECOG.printed_max_score || undefined;
      } else {
        hint.textContent = '未能提取题面，无法批改：请确认照片里包含印刷题目';
        return;
      }
      if (RECOG.student_name) body.student_name = RECOG.student_name;
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
      // 上传件批改完成后自动飞书提醒
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
    if (score >= max) return ['correct', 'is-ok', '全部得分'];
    if (score <= 0) return ['wrong', 'is-bad', '未得分'];
    return ['partial', 'is-warn', '部分得分'];
  }

  /* 整页总分的口径说明。满分 = 各题分值之和，而分值有三个来源：
     题库（教师传的答案页）、卷面印的数字、系统按题型推定的。
     推定占了多少必须写出来——不写，老师会把这个总分当卷面分抄进成绩册。 */
  function pageBasisNote(r) {
    var total = r.question_count || (r.questions || []).length;
    var printed = r.printed_score_count || 0;
    if (r.bank_name) {
      var m = r.matched_count || 0;
      return '按题库《' + esc(r.bank_name) + '》逐题比对，' + m + ' / ' + total +
        ' 题对上题库分值' +
        (m < total ? '；其余 ' + (total - m) + ' 题题库中无对应题，分值按题型推定，教师可复核'
                   : '');
    }
    return '无题库：' + (printed ? printed + ' 题用卷面印的分值，' : '') +
      (total - printed) + ' 题分值按题型推定。' +
      '<b>先传教师答案页可按标准答案逐题比对</b>，「答案匹配度」一维也才计分。';
  }

  /* 带批改痕迹的学生原图。整页批改才有。
     这块放在结果页最前面：老师看一份作业的第一反应是「卷子上批成什么样」，
     而不是先读五个置信度因子。 */
  function pageMarkHtml(r) {
    if (!r.marked_url) return '';
    var acc = r.mark_stats || {};
    return '<div class="sheet">' +
      '<div class="toolbar">' +
        '<h3>批改痕迹（学生原件）</h3>' +
        '<span class="spacer"></span>' +
        '<a class="btn btn--ghost btn--sm" href="' + esc(r.marked_url) + '" download>' +
          '<span data-icon="arrowDown" data-icon-size="14"></span><span>下载批改件</span></a>' +
      '</div>' +
      '<div class="marked">' +
        '<img class="marked__img" src="' + esc(r.marked_url) + '" ' +
          'alt="批改后的学生作业，勾叉画在每题的学生作答旁">' +
        // 坐标准不准要如实报，别让老师以为记号一定贴着答案
        (acc.located != null
          ? '<div class="marked__note"><b>' + acc.located + ' / ' + acc.total +
            '</b> 道题的记号按识别到的位置落笔' +
            (acc.on_answer != null && acc.on_answer < acc.located
              ? '（其中 ' + acc.on_answer + ' 道贴着学生作答，' +
                (acc.located - acc.on_answer) + ' 道只定位到题目、画在题目右侧）'
              : '') +
            (acc.total > acc.located
              ? '，其余 ' + (acc.total - acc.located) + ' 道未定位到坐标，记号画在页边并标注题号'
              : '') + '。批改件与原件分开保存，学生原图不会被改写。</div>'
          : '') +
      '</div>' +
      (r.mark_error
        ? '<div class="note note--amber"><span class="note__ico">' +
          I('alert', { size: 18 }) + '</span><div>' + esc(r.mark_error) + '</div></div>'
        : '') +
      '</div>';
  }

  /* 整页批改的逐题总览表。步骤链在下面另有一块，这里只回答
     「哪几道题扣了分」——一页十几道题时，那才是老师第一眼要找的。 */
  function pageQuestionsHtml(r) {
    var qs = r.questions || [];
    if (!qs.length) return '';
    return '<div class="sheet">' +
      '<div class="toolbar">' +
        '<h3>逐题得分（共 ' + qs.length + ' 道）</h3>' +
        '<span class="spacer"></span>' +
        '<span class="hint-inline">' + (r.bank_name
          ? '按题库《' + esc(r.bank_name) + '》的标准答案逐题比对'
          : '无题库，由大模型自行判分') + '</span>' +
      '</div>' +
      '<div class="pq-list">' + qs.map(function (q) {
        var st = stepState(q.score, q.max_score);
        return '<div class="pq ' + st[1] + '">' +
          '<span class="pq__no">' + esc(q.no || q.index) + '</span>' +
          '<span class="pq__mark">' + I(st[0], { size: 16 }) + '</span>' +
          '<span class="pq__body">' +
            '<span class="pq__stem">' + esc((q.stem || '').slice(0, 70)) + '</span>' +
            '<span class="pq__ans">作答：' +
              esc((q.student_answer || '（未作答）').slice(0, 70)) + '</span>' +
            (q.error_tag ? '<span class="pq__tags"><span class="tag tag--sm">' +
              esc(q.error_tag) + '</span></span>' : '') +
            // 没对上题库的题要说出来：它的分是模型自定口径给的，
            // 也没有计入答案匹配度，教师有权知道这条差别。
            (r.bank_name && !q.matched
              ? '<span class="pq__tags"><span class="tag tag--sm tag--quiet">' +
                '题库中无此题</span></span>' : '') +
            // 教师页把小问拆成了多条、学生页是一整块时，这道题的满分是
            // 几条小问加出来的。不写出来，教师看到「8 分」无从对账。
            (q.absorbed && q.absorbed.length
              ? '<span class="pq__tags"><span class="tag tag--sm tag--quiet">' +
                '含题库 ' + q.absorbed.map(function (a) {
                  return esc(a.no || a.qid);
                }).join('、') + '</span></span>' : '') +
          '</span>' +
          '<span class="pq__score">' + q.score + '<span class="pq__max">/' +
            q.max_score + '</span></span>' +
        '</div>';
      }).join('') + '</div></div>';
  }

  // 步骤状态 → 分流章的色位。语义不同（这是「这一步对不对」，不是「这份要不要人审」），
  // 但三色阶一致，复用同一套 stamp 类。
  var STEP_STAMP = { 'is-ok': 'g', 'is-warn': 'y', 'is-bad': 'r' };

  /* 待复核题清单：识别层自己报出「这道没读准」的题，点名摆在教师备注上方。

     为什么要单独占一块而不是只写进备注文字：这是唯一会让一份**分数好看**的
     作业停下来的信号——置信度是整页一个数，一道题读不准会被其它因子稀释，
     教师扫一眼分数就过了。注意它只覆盖「系统知道自己没把握」的题；模型自信
     读错的题这里不会出现（见 pagegrader._review_flags 顶部实测记录），那类
     只能靠教师看转写发现。 */
  function reviewBlock(r) {
    var flags = r.review_flags || [];
    if (!flags.length) return '';
    return '<h3 class="block-title">待人工复核</h3>' +
      '<div class="muted" style="margin-bottom:8px">' +
        '以下题目系统未能可靠读取，判分仅供参考，请对照原图确认：</div>' +
      '<div class="factor-wrap">' +
        flags.map(function (f) {
          return '<span class="stamp stamp--y">第 ' + esc(String(f.no || f.index)) +
                 ' 题 · ' + esc(f.reason) + '</span>';
        }).join('') +
      '</div>';
  }


  function renderResult() {
    var r = CURRENT;
    if (!r) return;

    var steps = stepChainHtml(r.step_analysis);

    // 因子区只放五因子本身。二次批改一致性的分数已经是其中一枚 chip，
    // 再挂一个「复核分 15」的绿标签是同一件事说两遍。
    var factors = factorChipsHtml(r);
    // 交叉验证不占权重，所以在五因子里看不出来；它一票否决转红时必须有交代，
    // 否则教师看到的是「置信度 88 却判了红」——像个 bug。
    var crossNote = '';
    if (r.cross_check && r.cross_check.escalated) {
      crossNote = '<p class="hint">大模型二独立复核判 <b>' +
        r.cross_check.model2_score + '</b> 分，与本次判分相差 ' +
        r.cross_check.gap + ' 分（超过满分 15%），两个模型结论分歧显著，' +
        '已不按置信度自动放行，<b>强制转人工批改</b>。</p>';
    }

    var tags = r.error_tags.length
      ? r.error_tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('')
      : '<span class="muted">无</span>';

    // 知识点块：题库外走维度体系时，知识点就是那五个维度名，和上面的
    // 逐批改节点一字不差，再列一遍纯属占地方——那种情况整块不出。
    var kps = r.knowledge_points || [];
    var stepNames = (r.step_analysis || []).map(function (s) { return String(s.step || '').trim(); });
    var kpDup = kps.length && kps.every(function (k) { return stepNames.indexOf(String(k).trim()) >= 0; });
    var kpBlock = (!kps.length || kpDup) ? '' :
      '<h3 class="block-title">知识点</h3><div class="factor-wrap">' +
        kps.map(function (k) { return '<span class="tag tag--kp">' + esc(k) + '</span>'; }).join('') +
      '</div>';

    var mine = (r.source === 'upload')
      ? '<div class="note note--green"><span class="note__ico">' + I('circleCheck', { size: 18 }) +
        '</span><div>这份是<b>你刚上传的作业</b>，已编号 <b>' + esc(r.submission_id || '') +
        '</b> 并进入「03 教师工作台」与「04 班级看板」，可以继续走完终审动线。</div></div>' : '';

    var sk = STATUS_STAMP[r.status] || 'y';
    // score_basis === 'system'：题库外作业，分数是五维度体系判别分而非试卷实际分值
    var sysBasis = (r.score_basis === 'system');
    var isPage = (r.grade_scope === 'page');

    $('#result-body').innerHTML =
      mine +
      pageMarkHtml(r) +
      '<div class="sheet">' +
        '<div class="toolbar">' +
          '<div>' +
            '<h3>' + esc(r.student_name) + ' · ' + esc(r.subject) + ' · ' + esc(r.question_title) + '</h3>' +
            '<p class="sub" style="margin:4px 0 0;">Step 02 · 过程级批改与证据链</p>' +
          '</div>' +
          '<span class="spacer"></span>' +
          '<span class="stamp stamp--' + sk + '">' + I(r.status, { size: 13 }) + ' ' +
            STATUS_TEXT[r.status] + '</span>' +
        '</div>' +

        '<div class="score-row">' +
          '<div class="score-card">' +
            '<div class="lab">' +
              (sysBasis ? '体系判别分 Discriminant'
                : isPage ? '整页总分 Page Score' : '总分 Score') + '</div>' +
            '<div class="val"><span id="ro-score">0</span><small> / ' + r.max_score + '</small></div>' +
            // 题库外作业：15 分是本体系的判别口径，不是试卷上那道题的分值。
            // 不写清楚，老师会直接把它当成实际得分抄进成绩册。
            (sysBasis
              ? '<div class="score-card__note">按五维度判别，非试卷分值' +
                (r.printed_max_score
                  ? '；卷面标注 <b>' + r.printed_max_score + '</b> 分，可按比例折算'
                  : '') + '</div>'
              // 整页满分 = 各题分值之和。有几道的分值是系统按题型推的，必须说出来，
              // 否则老师会把这个总分当成卷面分抄进成绩册。
              : isPage ? '<div class="score-card__note">' + pageBasisNote(r) + '</div>'
              : '') +
          '</div>' +
          '<div class="score-card">' +
            '<div class="lab">综合置信 Confidence</div>' +
            '<div class="val" style="font-size:1.45rem;color:' + STATUS_VAR[r.status] + '">' +
              '<span id="ro-conf">0</span></div>' +
            '<div class="meter"><span id="ro-meter" style="background:' + STATUS_VAR[r.status] + '"></span></div>' +
            '<div class="meter__ticks"><span>0</span><span>60</span><span>85</span><span>100</span></div>' +
          '</div>' +
          '<div class="score-card">' +
            '<div class="lab">分流 Triage</div>' +
            '<div style="margin-top:10px;"><span class="stamp stamp--' + sk + '">' +
              I(r.status, { size: 13 }) + ' ' + STATUS_TEXT[r.status] + '</span></div>' +
          '</div>' +
          '<div class="score-card">' +
            '<div class="lab">链路 Pipeline</div>' +
            '<div class="factor-wrap" style="margin-top:8px;">' +
            (r.recognition_engine ? '<span class="factor">识别 <b>' +
              esc(ENGINE_TEXT[r.recognition_engine] || r.recognition_engine) + '</b></span>' : '') +
            '<span class="factor">批改 <b>' + (r.mode === 'llm' ? '真实 LLM' : 'Mock 规则引擎') + '</b></span>' +
            '</div>' +
          '</div>' +
        '</div>' +

        (r.note ? '<p class="hint">' + esc(r.note) + '</p>' : '') +
        '<h3 class="block-title">置信度五因子</h3>' +
        '<div class="factor-wrap">' + factors + '</div>' +
        crossNote +
      '</div>' +

      (isPage ? '' :
      '<div class="sheet">' +
        '<div class="toolbar"><h3>题目与作答</h3></div>' +
        '<div class="kv">' +
          '<div class="kv__k">题目</div><div class="kv__v">' + esc(r.question_text) + '</div>' +
          // 题库外作业没有人工标准答案，判分基准是模型自己解出来的。
          // 必须如实标注来源：基准一错就会把正确作答判成错，而证据链看起来毫无异样。
          (sysBasis
            ? '<div class="kv__k">判分基准</div><div class="kv__v pre">' +
              (r.reference_answer
                ? esc(r.reference_answer) +
                  '<span class="basis-warn">模型自解，未经人工确认，请先核对基准再看判分</span>'
                : '<span class="muted">模型未给出自解，本次判分缺少可核对的基准</span>') +
              '</div>'
            : '<div class="kv__k">标准答案</div><div class="kv__v muted">' +
              esc(r.standard_answer) + '</div>') +
          '<div class="kv__k">学生作答</div><div class="kv__v pre">' + esc(r.ocr_text) + '</div>' +
        '</div>' +
      '</div>') +

      (isPage ? pageQuestionsHtml(r) : '') +

      '<div class="sheet">' +
        '<div class="toolbar">' +
          '<h3>' + (isPage ? '逐题判分与证据链' : '逐批改节点与证据链') + '</h3>' +
          '<span class="spacer"></span>' +
          '<span class="hint-inline" style="max-width:300px;text-align:right;">' +
            '每一步判分都引用学生作答原文作为依据，老师是在「审」而不是在「信」。</span>' +
        '</div>' +
        steps +
        kpBlock +
        '<h3 class="block-title">错因标签</h3><div class="factor-wrap">' + tags + '</div>' +
      '</div>' +

      '<div class="sheet">' +
        '<div class="toolbar"><h3>个性化评语</h3></div>' +
        '<p class="quote">' + esc(r.student_feedback) + '</p>' +
        reviewBlock(r) +
        '<h3 class="block-title">教师备注</h3><div class="muted">' + esc(r.teacher_note) + '</div>' +
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
      '<div class="filters teacher-filters">' +
      '<span class="hint-inline">筛选</span>' +
      '<select class="select select--sm" data-f="status" aria-label="分流状态">' + statusOpts + '</select>' +
      '<select class="select select--sm" data-f="subject" aria-label="题目">' + subjOpts + '</select>' +
      '<select class="select select--sm" data-f="tag" aria-label="错因">' + tagOpts + '</select>' +
      '<span class="hint-inline">共 <b class="mono">' + rows.length + '</b> 份</span>' +
      '</div>';

    var trs = rows.map(function (r) {
      var actions;
      if (r.reviewed) {
        var label = r.teacher_action === 'modified' ? '已修改' : '已确认';
        var revTags = (r.final_error_tags && r.final_error_tags.length)
          ? '<div class="meta-line">错因修订：' + r.final_error_tags.map(esc).join('、') + '</div>' : '';
        actions = '<span class="reviewed-label">' +
          I('check', { size: 14, stroke: 2.4 }) + label + ' ' + r.final_score + ' / ' + r.max_score +
          '</span>' + revTags +
          '<div class="row-actions"><button class="btn btn--sm btn--ghost" type="button" data-review="' +
          esc(r.submission_id) + '">' + I('eye', { size: 13 }) + '查看/改判</button></div>';
      } else {
        actions = '<div class="row-actions">' +
          '<button class="btn btn--sm ' + (r.status === 'green' ? 'btn--ghost' : 'btn--primary') +
          '" type="button" data-confirm="' + esc(r.submission_id) + '">' +
          (r.status === 'green' ? '抽查通过' : '确认') + '</button>' +
          '<button class="btn btn--sm btn--ghost" type="button" data-review="' + esc(r.submission_id) + '">' +
          I('penLine', { size: 13 }) + '终审修改</button>' +
          '<button class="btn btn--sm btn--ghost" type="button" data-detail="' +
          esc(r.submission_id) + '">' + I('eye', { size: 13 }) + '查看批改</button></div>';
      }
      var tags = r.error_tags.length
        ? r.error_tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('')
        : '<span class="muted">&#8212;</span>';
      var mine = (r.source === 'upload') ? ' <span class="tag tag--mine">我的上传</span>' : '';
      // 判别分与题库题的实际分值同列展示，必须有区分标记，否则两种口径混在
      // 一张表里没法比较。卷面分值已知时一并给出，方便老师折算。
      var basisMark = (r.score_basis === 'system')
        ? '<span class="basis-mark" title="体系判别分（五维度），非试卷分值' +
          (r.printed_max_score ? '；卷面标注 ' + r.printed_max_score + ' 分' : '') +
          '">判别</span>'
        : '';
      // 同一份作业拆出的多行要能看出同源，否则「张三」在表里出现 8 次
      // 像是交了 8 份作业。两种来源：单题批改按 paper_id 聚，整页批改按页码。
      var paperMark = '';
      if (r.paper_id && r.paper_total > 1) {
        paperMark = '<span class="paper-mark" title="同一份作业第 ' + r.paper_index +
          ' 题（共 ' + r.paper_total + ' 题）·分组编号 ' + esc(r.paper_id) + '">题 ' +
          r.paper_index + '/' + r.paper_total + '</span>';
      } else if (r.page_total > 1) {
        paperMark = '<span class="paper-mark" title="多页作业的第 ' + r.page_no +
          ' 页（共 ' + r.page_total + ' 页）">页 ' + r.page_no + '/' + r.page_total +
          '</span>';
      }
      return '<tr>' +
        '<td><b>' + esc(r.student_name) + '</b>' + mine + '</td>' +
        '<td>' + esc(r.subject) + ' · ' + esc(r.question_title) + paperMark + '</td>' +
        '<td class="num"><b>' + r.ai_score + '</b> / ' + r.max_score + basisMark + '</td>' +
        '<td class="num">' + r.confidence.toFixed(1) + '</td>' +
        '<td><span class="stamp stamp--' + (STATUS_STAMP[r.status] || 'y') + '">' +
          I(r.status, { size: 12 }) + ' ' + STATUS_TEXT[r.status] + '</span></td>' +
        '<td>' + tags + '</td>' +
        '<td>' + actions + '</td></tr>';
    }).join('');

    var hasBasis = rows.some(function (r) { return r.score_basis === 'system'; });
    body.innerHTML = filterBar +
      '<div class="table-wrap teacher-table-wrap"><table class="teacher-table">' +
      '<thead><tr><th>学生</th><th>题目</th><th>AI 分</th><th>置信度</th>' +
      '<th>分流</th><th>错因</th><th>操作</th></tr></thead>' +
      '<tbody>' + trs + '</tbody></table></div>' +
      (hasBasis ? '<p class="hint">标注「判别」的是题库外作业，' +
        '分数为五维度体系判别分（满分 15），代表本体系的判别口径而非试卷分值，' +
        '可按卷面分值比例折算。</p>' : '');
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
      I('shieldCheck', { size: 18 }) + '</span><div>终审已记录。该题教师通过率因子回灌为 <b class="mono">' +
      resp.question_pass_rate + '</b>，同题未终审作答的置信度已按新因子重新计算。</div></div>';
    Icons.hydrate(el);
    setTimeout(function () { el.innerHTML = ''; }, 6000);
  }

  function quickReview(id) {
    api('/api/teacher/review', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ submission_id: id, teacher_action: 'confirmed' })
    }).then(function (resp) {
      toast('已确认', '已记入终审，通过率因子已回灌');
      showTeacherNote(resp);
      loadTeacher();
    }).catch(function (e) { alert('提交失败：' + e.message); });
  }

  // 逐步证据链 HTML（结果页 / 审卷面板共用）
  function stepChainHtml(step_analysis) {
    if (!step_analysis || !step_analysis.length) return '';
    return '<div class="evidence">' + step_analysis.map(function (s, i) {
      var st = stepState(s.score, s.max_score);
      var tag = s.error_tag ? '<span class="tag">' + esc(s.error_tag) + '</span>' : '';
      var evid = s.evidence
        ? '<div class="step__quote">' + I('quote', { size: 14 }) +
          '<span>' + esc(s.evidence) + '</span></div>' : '';
      var illegible = (s.legible === false)
        ? '<div class="step__quote step__quote--warn">' + I('alert', { size: 14 }) +
          '<span>该步字迹难以辨认，建议教师人工复核</span></div>' : '';
      // 模型没返回这一评分点：0 分但不是「判定为 0 分」。两者在界面上原本
      // 长得一模一样，教师会把判分缺失读成判分结论。
      var missing = s.missing
        ? '<div class="step__quote step__quote--warn">' + I('alert', { size: 14 }) +
          '<span>模型未返回该评分点的判定，此处 0 分为缺省值而非判分结论，' +
          '请人工补判</span></div>' : '';
      // 知识点与步骤名相同就不再挂一次。题库外走维度体系时，知识点就是维度名、
      // 也就是步骤名，两个一模一样的标签并排显示像是渲染出了 bug。
      var kp = String(s.knowledge_point || '').trim();
      var kpTag = (kp && kp !== String(s.step || '').trim())
        ? '<span class="tag tag--kp">' + esc(kp) + '</span>' : '';
      // 右侧那枚章：颜色 + 图标 + 读屏文本三重编码，色弱与读屏都能分辨得分状态
      return '<div class="step ' + st[1] + '" data-reveal="' + (i * 45) + '">' +
        '<span class="idx">' + (i + 1) + '</span>' +
        '<div>' +
          '<div class="t">' + esc(s.step) + ' ' + tag + kpTag + '</div>' +
          '<div class="d">' + esc(s.reason) + '</div>' +
          evid + illegible + missing +
        '</div>' +
        '<span class="stamp stamp--' + STEP_STAMP[st[1]] + '" title="' + st[2] + '">' +
          I(st[0], { size: 13, stroke: 2.2 }) +
          '<span class="mono">' + s.score + ' / ' + s.max_score + '</span>' +
          '<span class="sr-only">（' + st[2] + '）</span>' +
        '</span>' +
        '</div>';
    }).join('') + '</div>';
  }

  // 置信度因子 HTML（结果页 / 审卷面板共用）
  function factorChipsHtml(r) {
    var f = r.confidence_factors;
    if (!f) return '';
    return FACTOR_ORDER.map(function (k) {
      if (!(k in f)) return '';
      var w = FACTOR_WEIGHT[k];
      var v = f[k];
      var wHtml = w ? ' <em class="factor__w">&#215;' + w.toFixed(2) + '</em>' : '';
      // 测不出的一维要摆在明面上：静默略过会让教师以为公式只有四项，
      // 而实际是这一项的权重被重归一化摊给了其余四项。
      if (v === null || v === undefined) {
        var why = (k === 'answer_match') ? '无题库 · 权重已重归一化'
                                         : '未触发 · 权重已重归一化';
        return '<span class="factor factor--na">' + esc(FACTOR_LABEL[k] || k) +
          wHtml + ' <b>' + why + '</b></span>';
      }
      return '<span class="factor">' + esc(FACTOR_LABEL[k] || k) + wHtml +
        ' <b>' + v + '</b></span>';
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
      ? '<p class="pre-sm">' + esc(r.ocr_text) + '</p>'
      : '<p class="hint">（内置样例 · 转写见结果页）</p>';

    // 维度体系作业：逐维改分。只有总分的话，回灌只能得出「这题判错了」，
    // 得不出「运算执行这一维偏严」，而后者才是能指导下一份批改的信息。
    // 预填 AI 的逐维得分，教师只改错的那一维；总分随之自动求和。
    var dims = (r.step_analysis || []).filter(function (s) { return s.dimension; });
    var dimBase = (r.final_dimension_scores && Object.keys(r.final_dimension_scores).length)
      ? r.final_dimension_scores : null;
    var dimBlock = dims.length
      ? '<h3 class="block-title">逐维度终审改分</h3>' +
        '<div class="dim-grid" id="rv-dims">' +
        dims.map(function (s) {
          var v = (dimBase && dimBase[s.dimension] != null) ? dimBase[s.dimension] : s.score;
          return '<label class="dim-cell">' +
            '<span class="dim-cell__k">' + esc(s.step) + '</span>' +
            '<span class="dim-cell__in">' +
            '<input class="input input--sm" type="number" data-dim="' + esc(s.dimension) +
              '" data-ai="' + s.score + '" min="0" max="' + s.max_score +
              '" step="0.5" value="' + v + '"' +
              ' aria-label="' + esc(s.step) + ' 得分，满分 ' + s.max_score + '">' +
            '<em>/ ' + s.max_score + '</em></span>' +
            '<span class="dim-cell__ai">AI ' + s.score + '</span>' +
            '</label>';
        }).join('') +
        '</div>' +
        '<p class="hint">改动任一维度，终审总分自动按各维之和更新。' +
        '逐维修正量会按「学科 × 维度」累积，作为后续同类作业的判分提示。</p>'
      : '';

    $('#modal-root').innerHTML =
      '<div class="mask" id="mask"><div class="dialog dialog--wide" role="dialog" aria-modal="true"' +
      ' aria-labelledby="rv-title">' +
      '<div class="dialog__head">' +
        '<div><div class="kicker">Final Review</div>' +
        '<h2 class="dialog__title" id="rv-title">教师终审 · ' + esc(r.student_name) + '</h2></div>' +
        '<button class="dialog__close" type="button" data-close="1" aria-label="关闭">' +
        I('x', { size: 18 }) + '</button>' +
      '</div>' +
      '<p class="sub" style="margin:0;">' + esc(r.subject) + ' · ' + esc(r.question_title) +
        ' ｜ AI 判分 <b class="mono">' + r.ai_score + ' / ' + r.max_score +
        '</b>，置信度 <b class="mono">' + r.confidence.toFixed(1) + '</b>' +
        (r.reviewed ? ' ｜ <span class="reviewed-label">已终审</span>' : '') +
        '</p>' +
      '<div class="review-body">' +
        '<div>' +
          '<h3 class="block-title" style="margin-top:0;">题目与标准答案</h3>' +
          '<p class="hint">' + esc(r.question_text) + '</p>' +
          '<p class="hint" style="color:var(--st-green);font-weight:600;">标准答案：' +
            esc(r.standard_answer) + '</p>' +
          '<h3 class="block-title">识别转写</h3>' + ocrBlock +
          '<h3 class="block-title">置信度因子</h3>' +
          '<div class="factor-wrap">' + factorChipsHtml(r) + '</div>' +
        '</div>' +
        '<div>' +
          '<h3 class="block-title" style="margin-top:0;">AI 逐步证据链</h3>' +
          stepChainHtml(r.step_analysis) +
        '</div>' +
      '</div>' +
      '<div class="review-foot">' +
        dimBlock +
        '<div class="review-form-grid">' +
          '<div class="field"><label for="rv-score">终审分数' +
            (dims.length ? ' <span class="hint-inline">（各维之和）</span>' : '') + '</label>' +
          '<input class="input" type="number" id="rv-score" min="0" max="' + r.max_score +
            '" step="0.5" value="' + baseScore + '"' +
            (dims.length ? ' readonly' : '') + '></div>' +
          '<div class="field"><label>错因标签改判</label>' +
          '<div class="tagpick" id="rv-tags">' + tags + '</div></div>' +
        '</div>' +
        '<div class="field" style="margin-top:12px;">' +
          '<label for="rv-comment">评语修订 · 留空则沿用 AI 评语</label>' +
          '<textarea class="textarea" id="rv-comment" rows="3" placeholder="' +
            esc((r.ai_feedback || '').slice(0, 50)) + '…">' + esc(baseComment) + '</textarea>' +
        '</div>' +
        '<div class="detail-actions">' +
          '<button class="btn btn--ghost" type="button" data-close="1">取消</button>' +
          '<button class="btn btn--primary" type="button" id="rv-submit" data-id="' + esc(id) +
          '" data-max="' + r.max_score + '">' + I('check', { size: 15 }) + '提交终审</button>' +
        '</div>' +
      '</div></div></div>';
    Icons.hydrate($('#modal-root'));
    var input = $('#rv-score');

    if (dims.length) {
      var dimInputs = $$('#rv-dims input[data-dim]');
      var syncTotal = function () {
        var sum = 0;
        dimInputs.forEach(function (el) {
          var n = Number(el.value);
          var max = Number(el.max);
          // 越界值不参与求和，也不静默改写老师输入的数字：
          // 标红提示、由老师自己改，比替他做决定安全。
          var bad = isNaN(n) || n < 0 || n > max;
          el.classList.toggle('is-bad', bad);
          if (!bad) sum += n;
        });
        input.value = Math.round(sum * 100) / 100;
      };
      dimInputs.forEach(function (el) {
        el.addEventListener('input', syncTotal);
      });
      // 打开即对齐一次：AI 总分与逐维之和理论上相等，但终审记录里可能
      // 只存过总分（旧记录），此时以逐维之和为准，避免提交出一个自相矛盾的单子。
      syncTotal();
      if (dimInputs.length) dimInputs[0].focus();
    } else if (input) {
      input.focus();
    }
  }

  function closeModal() { $('#modal-root').innerHTML = ''; }

  function submitReview(id, maxScore) {
    // 逐维改分：先校验每一维，越界就地报错，不让它悄悄不参与总分求和
    var dimEls = $$('#rv-dims input[data-dim]');
    var dimScores = null, dimChanged = false;
    if (dimEls.length) {
      dimScores = {};
      for (var i = 0; i < dimEls.length; i++) {
        var el = dimEls[i];
        var n = Number(el.value);
        var mx = Number(el.max);
        if (el.value === '' || isNaN(n) || n < 0 || n > mx) {
          alert('「' + (el.getAttribute('aria-label') || '').split(' ')[0] +
            '」得分须在 0 ~ ' + mx + ' 之间');
          el.focus();
          return;
        }
        dimScores[el.dataset.dim] = n;
        if (n !== Number(el.dataset.ai)) dimChanged = true;
      }
    }

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
    // dimChanged 必须参与判定：运算执行 -1、步骤完整 +1 时总分不变，但这是
    // 两个方向相反的真实偏差信号，按「确认」提交会把它整个丢掉。
    var untouched = row && score === baseScore && tagsSame && !comment && !dimChanged;
    var payload = untouched
      ? { submission_id: id, teacher_action: 'confirmed' }
      : { submission_id: id, teacher_action: 'modified', final_score: score,
          final_error_tags: tags, final_comment: comment || null };
    // 维度分随「修改」一起提交：即使总分没变，逐维之间的挪动也是有效回灌信号
    // （比如运算执行 -1、步骤完整 +1，总分不动但两维的偏差方向相反）。
    if (dimScores && !untouched) payload.final_dimension_scores = dimScores;

    api('/api/teacher/review', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      closeModal();
      toast('已终审', untouched ? '按「确认」记录' : '分数 / 错因 / 评语已更新');
      showTeacherNote(resp);
      loadTeacher();
    }).catch(function (e) { alert('提交失败：' + e.message); });
  }

  /* ======================================================================
     8. Step 04：班级看板
     ====================================================================== */

  function severity(rate) {
    if (rate >= 60) return 'var(--st-red)';
    if (rate >= 40) return 'var(--st-yellow)';
    return 'var(--st-green)';
  }

  function loadBoard() {
    var body = $('#board-body');
    api('/api/analytics/class').then(function (d) {
      var dist = d.distribution;

      var kpBars = d.weak_knowledge_points.map(function (w, i) {
        return '<div class="bar-row"><span>' + esc(w.name) + '</span>' +
          '<span class="bar-track"><i data-grow="' + w.error_rate +
          '" data-delay="' + (i * 60) + '" style="background:' + severity(w.error_rate) + '"></i></span>' +
          '<b>' + w.wrong + '/' + w.total + ' · ' + w.error_rate + '%</b></div>';
      }).join('');

      var maxCount = Math.max.apply(null, [1].concat(d.error_tag_distribution.map(function (t) { return t.count; })));
      var tagBars = d.error_tag_distribution.map(function (t, i) {
        return '<div class="bar-row"><span>' + esc(t.name) + '</span>' +
          '<span class="bar-track"><i data-grow="' +
          Math.round(t.count / maxCount * 100) + '" data-delay="' + (i * 55) + '"></i></span>' +
          '<b>' + t.count + ' 次</b></div>';
      }).join('');

      var SEG_CLS = { green: 'dist__g', yellow: 'dist__y', red: 'dist__r' };
      var seg = function (k) {
        if (!dist[k]) return '';
        return '<span class="dist__seg ' + SEG_CLS[k] + '" data-grow="' + dist[k + '_pct'] +
          '">' + STATUS_TEXT[k] + ' ' + dist[k] + '</span>';
      };

      var uploadNote = d.upload_count
        ? '<div class="note note--green"><span class="note__ico">' + I('circleCheck', { size: 18 }) +
          '</span><div>本看板含<b>你自己上传的 ' + d.upload_count + ' 份作业</b>' +
          '（内置班级作答 ' + d.builtin_count + ' 份）。这些数据只属于你这次体验。</div></div>' : '';

      body.innerHTML = uploadNote +
        '<div class="sheet">' +
          '<div class="toolbar">' +
            '<h3>班级学情 · ' + esc(d.class_name) + '</h3>' +
            '<span class="spacer"></span>' +
            '<span class="hint-inline">Step 04 · 根据本次批改结果实时聚合</span>' +
          '</div>' +
          // 内置作答在 llm 模式下惰性批改：没批完就打开看板时，各项指标都只是
          // 「按已批的那几份算出来的」，必须说明，不能把半成品当结论展示
          (d.graded_total && d.graded_count < d.graded_total
            ? '<div class="note note--amber"><span class="note__ico">' +
              I('alert', { size: 18 }) + '</span><div>内置作答尚在批改中（<b>' +
              d.graded_count + ' / ' + d.graded_total +
              '</b> 份已完成）。下列指标只按已批完的部分聚合，稍后刷新本页可看到完整学情。' +
              '</div></div>'
            : '') +
          '<div class="kpi-row">' +
            '<div class="kpi"><div class="lab">参与作答</div><div class="val" data-count="' +
              d.student_count + '" data-suffix=" 人">0</div>' +
              // 整份试卷拆题后份数与人数不再相等，只报一个数说不清在统计什么
              (d.result_count != null && d.result_count !== d.student_count
                ? '<div class="kpi__sub">共 ' + d.result_count + ' 份批改</div>' : '') +
            '</div>' +
            '<div class="kpi"><div class="lab">平均得分率</div><div class="val" data-count="' + d.average_score_pct + '" data-digits="1" data-suffix="%">0</div></div>' +
            '<div class="kpi"><div class="lab">薄弱知识点</div><div class="val" data-count="' + d.weak_knowledge_points.length + '">0</div></div>' +
            '<div class="kpi"><div class="lab">批改模式</div><div class="val is-text">' + (d.mode === 'llm' ? '真实 LLM' : 'Mock 规则引擎') + '</div></div>' +
          '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="toolbar"><h3>红黄绿分流占比</h3></div>' +
          '<div class="dist">' + seg('green') + seg('yellow') + seg('red') + '</div>' +
          '<div class="legend">' +
            '<span><i class="swatch-i" style="background:var(--st-green)"></i>自动通过 ' + dist.green + ' 人（' + dist.green_pct + '%）</span>' +
            '<span><i class="swatch-i" style="background:var(--st-yellow)"></i>教师确认 ' + dist.yellow + ' 人（' + dist.yellow_pct + '%）</span>' +
            '<span><i class="swatch-i" style="background:var(--st-red)"></i>转人工 ' + dist.red + ' 人（' + dist.red_pct + '%）</span>' +
          '</div>' +
        '</div>' +

        '<div class="two-col">' +
          '<div class="sheet">' +
            '<div class="toolbar"><h3>知识点错误率</h3></div>' +
            '<div class="bars">' + (kpBars || emptyBox('暂无数据')) + '</div>' +
            '<div class="legend">' +
              '<span><i class="swatch-i" style="background:var(--st-green)"></i>&lt; 40%</span>' +
              '<span><i class="swatch-i" style="background:var(--st-yellow)"></i>40% &#8211; 60%</span>' +
              '<span><i class="swatch-i" style="background:var(--st-red)"></i>&#8805; 60%</span>' +
            '</div>' +
          '</div>' +
          '<div class="sheet">' +
            '<div class="toolbar"><h3>错因分布</h3></div>' +
            '<div class="bars">' + (tagBars || emptyBox('暂无数据')) + '</div>' +
          '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="toolbar"><h3>下节课讲评建议</h3></div>' +
          '<ul class="sugg">' + d.teaching_suggestions.map(function (s) {
            return '<li>' + I('lightbulb', { size: 16 }) + '<span>' + esc(s) + '</span></li>';
          }).join('') + '</ul>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="toolbar">' +
            '<h3>讲评课件大纲</h3>' +
            '<span class="spacer"></span>' +
            '<button class="btn btn--primary btn--sm" type="button" id="gen-outline">' +
              I('bookOpen', { size: 15 }) + '生成讲评大纲</button>' +
            '<button class="btn btn--ghost btn--sm" type="button" id="copy-outline" style="display:none;">' +
            I('copy', { size: 15 }) + '<span>复制 Markdown</span></button>' +
          '</div>' +
          '<p class="hint">基于本次批改数据自动生成结构化大纲（共性错因 + 典型错例证据 + 分层任务 + 复测建议），' +
          '可一键复制为讲评课件底稿，粘贴至希沃白板、飞书文档等备课环境。</p>' +
          '<div id="outline-body" class="mt-4"></div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="toolbar">' +
            '<h3>学生个人错因画像</h3>' +
            '<span class="spacer"></span>' +
            '<select class="select select--sm" id="stu-select" style="max-width:200px;" aria-label="选择学生"></select>' +
          '</div>' +
          '<p class="hint">跨题聚合本次批改 + 历史错因时间线（历史为<b>模拟数据</b>，用于演示画像形态）。</p>' +
          '<div id="profile-body" class="mt-4">' + emptyBox('选择学生查看画像', 'users') + '</div>' +
        '</div>' +

        '<div class="sheet">' +
          '<div class="toolbar">' +
            '<div>' +
              '<h3>飞书协同</h3>' +
              '<p class="sub" style="margin:4px 0 0;">审核提醒推送到群；学情写入多维表格，表内 AI 字段生成摘要与建议</p>' +
            '</div>' +
          '</div>' +
          '<p class="hint">机器人互动卡片提醒教师审核，多维表格沉淀学情台账并由 AI 字段捷径' +
          '自动生成错因摘要与学习建议。</p>' +
          '<div class="toolbar" style="margin:14px 0 0;">' +
            '<button class="btn btn--primary" type="button" id="feishu-push">' + I('send', { size: 15 }) + '推送审核提醒卡片</button>' +
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
        return '<div class="bar-row"><span>' + esc(t.name) + '</span>' +
          '<span class="bar-track"><i data-grow="' +
          Math.round(t.count / fmax * 100) + '" data-delay="' + (i * 55) + '"></i></span>' +
          '<b>' + t.count + ' 次</b></div>';
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
          score + chip + '<div class="factor-wrap" style="margin-top:5px;">' + tags + '</div></li>';
      }).join('');

      body.innerHTML =
        '<div class="kpi-row" style="grid-template-columns:repeat(3,1fr);">' +
          '<div class="kpi"><div class="lab">本次作答</div><div class="val">' + p.submission_count + '</div></div>' +
          '<div class="kpi"><div class="lab">平均得分率</div><div class="val">' + p.average_score_pct + '%</div></div>' +
          '<div class="kpi"><div class="lab">薄弱知识点</div><div class="val">' + (p.weak_knowledge_points || []).length + '</div></div>' +
        '</div>' +
        '<div class="profile-grid" style="margin-top:14px;">' +
          '<div class="panel"><h4>错因频次 · 本次作业</h4><div class="bars">' + freqBars + '</div></div>' +
          '<div class="panel"><h4>趋势判断</h4><p class="quote" style="border:0;background:none;padding:0;">' +
            esc(p.trend_summary || '') + '</p></div>' +
        '</div>' +
        '<h3 class="block-title">错因演变时间线</h3><ul class="timeline">' + tl + '</ul>';
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
      body.innerHTML = '<div class="outline">' + esc(OUTLINE_MD) + '</div>';
      $('#copy-outline').style.display = '';
      toast('已生成', '讲评大纲已更新');
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
      toast('已复制', '可直接粘贴到备课文档');
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

  /* 飞书结果区是两栏常驻：左栏机器人卡片（推送产出），右栏多维表格台账（同步产出）。
     两个按钮各写各的一栏，都点过之后就是设计稿里那张「飞书第二现场」全景图；
     只点一个时空栏用 :empty 收掉，不会留半幅空白。 */
  function feishuSlot(which) {
    var box = $('#feishu-result');
    if (!box) return null;
    if (!box.classList.contains('feishu-grid')) {
      box.className = 'feishu-grid';
      box.innerHTML = '<div class="feishu-col feishu-col--card"></div>' +
                      '<div class="feishu-col feishu-col--base"></div>';
    }
    return box.querySelector(which === 'card' ? '.feishu-col--card' : '.feishu-col--base');
  }

  /* 接口原文收进折叠块。评委要验「这是真调用不是画的」时一点即开，
     平时不把一屏 JSON 糊在演示界面上。 */
  function rawJson(label, d) {
    return '<details class="raw"><summary>' + esc(label) + '</summary>' +
      '<div class="pre">' + esc(JSON.stringify(d, null, 2)) + '</div></details>';
  }

  function pushFeishu() {
    var box = feishuSlot('card');
    if (!box) return;
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
          if (el.tag === 'div' && el.text) return '<p>' + larkMd(el.text.content) + '</p>';
          if (el.tag === 'hr') return '<hr>';
          if (el.tag === 'action' && el.actions) {
            var a = el.actions[0];
            return '<div class="mt-4"><span class="larkcard__cta" style="pointer-events:none;">' +
              esc(a.text.content) + '</span></div>';
          }
          if (el.tag === 'note' && el.elements) return '<p class="larkcard__note">' + esc(el.elements[0].content) + '</p>';
          return '';
        }).join('');
        cardHtml = '<div class="larkcard"><div class="larkcard__head">' + esc(header) + '</div>' +
          '<div class="larkcard__body">' + parts + '</div></div>';
      }
      box.innerHTML =
        '<h3 class="block-title" style="margin-top:0;">机器人审核提醒卡片</h3>' +
        banner(d.mode, '演示模式：未配置飞书凭据，以下为将推送的卡片', '已真实推送到飞书群') +
        cardHtml +
        rawJson('接口返回 · msg_type=interactive', d);
      Icons.hydrate(box);
      toast(d.mode === 'demo' ? '已生成卡片' : '已推送', d.mode === 'demo'
        ? '未配置飞书凭据，展示的是将推送的内容' : '审核提醒卡片已发送到群');
    }).catch(function (e) {
      box.innerHTML = emptyBox('推送失败：' + e.message, 'alert');
      Icons.hydrate(box);
    });
  }

  function syncFeishu() {
    var box = feishuSlot('base');
    if (!box) return;
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
          return '<tr>' + cols.map(function (c) {
            var v = esc(f[c]);
            // 分流那列在台账里也该是章，不是一个灰字
            if (c === '分流状态') {
              var k = { '自动通过': 'g', '教师确认': 'y', '转人工': 'r' }[f[c]];
              if (k) v = '<span class="stamp stamp--' + k + '">' + v + '</span>';
            }
            return '<td>' + v + '</td>';
          }).join('') + '</tr>';
        }).join('');
        tableHtml =
          '<div class="table-wrap"><table class="base-table"><thead><tr>' + head + '</tr></thead>' +
          '<tbody>' + rows + '</tbody></table></div>' +
          '<p class="larkcard__note">写入后可对「错因标签」「得分 / 满分」等列配置飞书 <b>AI 字段捷径</b>，' +
          '逐行自动生成「一句话错因摘要」与「个性化学习建议」。</p>';
      } else if (d.record_count) {
        // 兜底：拿到了条数但没拿到明细（老版本服务端 live 模式不返回 records）。
        // 不能什么都不显示——那看着像同步失败了。
        tableHtml = '<p class="hint">已写入 <b class="mono">' + d.record_count +
          '</b> 条记录（本次未返回明细，可展开下方接口返回核对）。</p>';
      } else {
        tableHtml = emptyBox('本次没有可同步的批改记录。先批改几份作业再同步台账。', 'table');
      }
      box.innerHTML =
        '<h3 class="block-title" style="margin-top:0;">多维表格《学情台账》 · ' + records.length + ' 条</h3>' +
        banner(d.mode, '演示模式：未配置飞书凭据，以下为将写入的记录', '已真实写入飞书多维表格《学情台账》') +
        tableHtml +
        rawJson('接口返回 · bitable records', d);
      Icons.hydrate(box);
      toast(d.mode === 'demo' ? '已生成台账' : '已同步', d.mode === 'demo'
        ? '未配置飞书凭据，展示的是将写入的记录' : '学情台账已写入多维表格');
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
    var tIn = $('#teacher-file-input');
    if (tIn) tIn.addEventListener('change', onTeacherFiles);
    $('#grade-btn').addEventListener('click', submitImageGrade);
    $('#paper-grade-btn').addEventListener('click', gradePaper);

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

    // 拖拽上传：桌面端把作业直接拖进来
    var dz = $('#dropzone');
    var tdz = $('#teacher-dropzone');
    [[dz, stageFiles], [tdz, buildBank]].forEach(function (pair) {
      var zone = pair[0], handler = pair[1];
      if (!zone) return;
      ['dragenter', 'dragover'].forEach(function (ev) {
        zone.addEventListener(ev, function (e) {
          e.preventDefault(); zone.classList.add('is-over');
        });
      });
      ['dragleave', 'drop'].forEach(function (ev) {
        zone.addEventListener(ev, function (e) {
          e.preventDefault(); zone.classList.remove('is-over');
        });
      });
      zone.addEventListener('drop', function (e) {
        var fs = e.dataTransfer && e.dataTransfer.files;
        if (!fs || !fs.length) return;
        handler(Array.prototype.slice.call(fs));
      });
    });

    // 题库夹的合并勾选：走 change 而不是 click——点 label 文字也会切换选中，
    // 那时 click 的 target 是 label，closest('[data-bank-pick]') 找不到。
    document.addEventListener('change', function (e) {
      var cb = e.target;
      if (!cb || !cb.dataset || !cb.dataset.bankPick) return;
      if (cb.checked) BANK_PICKS[cb.dataset.bankPick] = 1;
      else delete BANK_PICKS[cb.dataset.bankPick];
      syncBankPickBar();
    });

    // 全局委托：动态生成的按钮都在这里接
    document.addEventListener('click', function (e) {
      var t = e.target;
      var fcard = t.closest && t.closest('[data-folder]');
      if (fcard && fcard.classList.contains('folder')) {
        selectFolder(fcard.dataset.folder);
        return;
      }
      // 内置样例入口：学生页进待批清单，教师答案页建题库
      var stage = t.closest && t.closest('[data-demo-stage]');
      if (stage) { stageDemoItems([stage.dataset.demoStage]); return; }
      var bank = t.closest && t.closest('[data-demo-bank]');
      if (bank) { bankFromDemoItems([bank.dataset.demoBank]); return; }
      if (t.closest && t.closest('#bank-merge-btn')) {
        bankFromDemoItems(Object.keys(BANK_PICKS));
        return;
      }
      if (t.closest && t.closest('#bank-pick-clear')) {
        BANK_PICKS = {};
        $$('[data-bank-pick]').forEach(function (cb) { cb.checked = false; });
        syncBankPickBar();
        return;
      }

      var goto = t.closest && t.closest('[data-goto]');
      if (goto) { switchTab(goto.dataset.goto); return; }


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
      // 「全部增强批改」按钮的显隐取自这份配置。config 是异步回来的，
      // 万一用户在它到达前就选好了文件，暂存区已经渲染过一轮、按钮不会自己出现。
      renderStaging();
      Icons.hydrate();
    }).catch(function () { /* 配置拿不到不阻断主流程 */ });

    loadFolders();
    loadBanks();

    api('/api/demo-pages').then(function (data) {
      renderGallery(data);
      Icons.hydrate();
    }).catch(function (e) {
      // 内置样例清单拿不到时，把话说在识别引擎那一行——#gallery 容器已经撤掉，
      // 往它上面写会直接抛 null。夹内清单自己会显示各自的加载失败。
      var hint = $('#engine-hint');
      if (hint) hint.innerHTML = '内置样例清单加载失败：' + esc(e.message);
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
