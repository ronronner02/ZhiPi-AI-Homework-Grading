/*!
 * 智批π · 动效引擎
 *
 * 不引任何动画库（GSAP / Motion 都要走 CDN，与本项目「断网可演示」的硬约束冲突）。
 * 这里手写三样东西：弹簧积分器、进场观察器、滚动视差。
 * 全部尊重 prefers-reduced-motion —— 该开关打开时直接落到终值，不做过渡。
 */
(function (global) {
  'use strict';

  var REDUCED = global.matchMedia &&
    global.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------------------
     弹簧：半隐式欧拉积分
     比 CSS 三次贝塞尔更像真实物体——有过冲、有阻尼回正，且中途改目标不会突跳。
     ------------------------------------------------------------------------ */
  function spring(opts) {
    var from = opts.from || 0;
    var to = (opts.to === undefined) ? 1 : opts.to;
    var stiffness = opts.stiffness || 170;
    var damping = opts.damping || 24;
    var mass = opts.mass || 1;
    var precision = opts.precision || 0.004;
    var onUpdate = opts.onUpdate;
    var onDone = opts.onDone;

    if (REDUCED) {
      onUpdate && onUpdate(to);
      onDone && onDone();
      return function () {};
    }

    var x = from, v = 0, raf = 0, last = 0;

    function frame(t) {
      if (!last) last = t;
      // 夹住 dt：标签页切走再切回时 t 会跳很大，不夹住会一帧走完整段动画
      var dt = Math.min((t - last) / 1000, 1 / 30);
      last = t;
      // 固定子步长积分，高刚度下才稳定
      var steps = Math.max(1, Math.ceil(dt * 240));
      var h = dt / steps;
      for (var i = 0; i < steps; i++) {
        var f = -stiffness * (x - to) - damping * v;
        v += (f / mass) * h;
        x += v * h;
      }
      onUpdate && onUpdate(x);
      if (Math.abs(x - to) < precision && Math.abs(v) < precision) {
        onUpdate && onUpdate(to);
        onDone && onDone();
        return;
      }
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
    return function () { cancelAnimationFrame(raf); };
  }

  /* ------------------------------------------------------------------------
     数字滚动：分数、置信度、统计值都用它，弹簧收尾让数字「落定」而不是硬停
     ------------------------------------------------------------------------ */
  function countTo(el, to, opts) {
    opts = opts || {};
    var digits = opts.digits === undefined ? 0 : opts.digits;
    var from = opts.from === undefined ? 0 : opts.from;
    var suffix = opts.suffix || '';
    if (!el) return;
    return spring({
      from: from, to: to, stiffness: opts.stiffness || 120, damping: opts.damping || 26,
      precision: Math.pow(10, -(digits + 2)),
      onUpdate: function (v) { el.textContent = v.toFixed(digits) + suffix; }
    });
  }

  /* ------------------------------------------------------------------------
     进场：元素滚入视口时逐个浮起，同组内按顺序错开
     ------------------------------------------------------------------------ */
  var io = null;
  function observeReveals(root) {
    var nodes = (root || document).querySelectorAll('[data-reveal]:not([data-revealed])');
    if (!nodes.length) return;
    if (REDUCED) {
      nodes.forEach(function (n) { n.dataset.revealed = '1'; n.style.opacity = ''; });
      return;
    }
    if (!io) {
      io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (!e.isIntersecting) return;
          var el = e.target;
          var delay = Number(el.dataset.reveal) || 0;
          el.style.transition =
            'opacity 620ms cubic-bezier(.16,.84,.28,1) ' + delay + 'ms,' +
            'transform 620ms cubic-bezier(.16,.84,.28,1) ' + delay + 'ms';
          el.style.opacity = '1';
          el.style.transform = 'none';
          el.dataset.revealed = '1';
          io.unobserve(el);
        });
      }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    }
    nodes.forEach(function (n) {
      n.style.opacity = '0';
      n.style.transform = 'translateY(18px)';
      io.observe(n);
    });
  }

  /* ------------------------------------------------------------------------
     滚动视差：首屏拼贴的三张作业照片按不同速率位移，制造纸张分层的景深
     ------------------------------------------------------------------------ */
  var parallaxNodes = [];
  var ticking = false;

  function registerParallax(root) {
    parallaxNodes = Array.prototype.slice.call(
      (root || document).querySelectorAll('[data-parallax]'));
    if (parallaxNodes.length && !REDUCED) onScroll();
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var y = global.scrollY || global.pageYOffset || 0;
      parallaxNodes.forEach(function (el) {
        var k = Number(el.dataset.parallax) || 0;
        var rot = Number(el.dataset.parallaxRotate) || 0;
        el.style.transform =
          'translate3d(0,' + (y * k).toFixed(2) + 'px,0) rotate(' + rot + 'deg)';
      });
      ticking = false;
    });
  }
  if (!REDUCED) global.addEventListener('scroll', onScroll, { passive: true });

  /* ------------------------------------------------------------------------
     翻页：上一张纸带着轻微上移淡出，下一张从下方托起来落定。
     刻意不做满屏色幕——一整块高饱和专色糊住视野，既割裂又显廉价；
     这套视觉的隐喻是「翻过一页」，不是「刷一层漆」。
     swap 在两段动画之间执行，负责真正的 DOM 显隐切换。
     ------------------------------------------------------------------------ */
  function pageTurn(outEl, inEl, swap) {
    if (REDUCED || !outEl || !inEl) { swap && swap(); return; }

    // 先清掉这两个元素上一轮遗留的动画，否则反复进出会不断堆叠，
     // 且旧动画的 fill 状态会和新动画抢同一个属性。
    // 先摘掉回调再 cancel，避免旧动画的 oncancel 反过来触发本轮的 commit。
    [outEl, inEl].forEach(function (el) {
      el.style.willChange = '';             // 上一轮可能留了合成层提示，先清掉
      if (!el.getAnimations) return;
      el.getAnimations().forEach(function (a) {
        a.onfinish = a.oncancel = null;
        a.cancel();
      });
    });

    var done = false;
    function commit() {                    // 动画被打断也要保证 DOM 状态落定
      if (done) return;
      done = true;
      swap && swap();

      // 进场刻意用 fill:'none'：动画之外元素回到自身的自然样式，也就是「可见」。
      // 若用 fill:'both'，一旦这条动画因故没跑完（标签页被挂起、onfinish 没派发），
      // 反向填充会把元素永久钉在 opacity:0 上——整屏空白，比没有转场严重得多。
      inEl.style.willChange = 'transform, opacity';
      var a = inEl.animate(
        [{ opacity: 0, transform: 'translateY(14px)' },
         { opacity: 1, transform: 'translateY(0)' }],
        { duration: 380, easing: 'cubic-bezier(.16,.84,.28,1)', fill: 'none' }
      );
      a.onfinish = a.oncancel = function () { inEl.style.willChange = ''; };
    }

    // 退场保留 forwards：淡出后要一直停在透明，直到 swap 把它 display:none，
    // 否则最后一帧会闪回不透明。它下次出场前会被开头那段 cancel 清掉。
    outEl.style.willChange = 'transform, opacity';
    var o = outEl.animate(
      [{ opacity: 1, transform: 'translateY(0)' },
       { opacity: 0, transform: 'translateY(-18px)' }],
      { duration: 260, easing: 'cubic-bezier(.62,.02,.2,1)', fill: 'forwards' }
    );
    o.onfinish = o.oncancel = function () {
      outEl.style.willChange = '';
      commit();
    };
    setTimeout(commit, 420);               // onfinish 没来也不能卡在半路
  }

  /* ------------------------------------------------------------------------
     宽度动画：条形图 / 分流占比条统一走这里，避免各处手写 transition
     ------------------------------------------------------------------------ */
  function growTo(el, pct, delay) {
    if (!el) return;
    if (REDUCED) { el.style.width = pct + '%'; return; }
    el.style.width = '0%';
    setTimeout(function () { el.style.width = pct + '%'; }, delay || 40);
  }

  global.Motion = {
    reduced: REDUCED,
    spring: spring,
    countTo: countTo,
    reveal: observeReveals,
    parallax: registerParallax,
    pageTurn: pageTurn,
    growTo: growTo
  };
})(window);
