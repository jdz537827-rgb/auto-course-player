/**
 * chaoxing.js —— 超星学习通（泛雅 MOOC2-ANS）专用保活脚本
 *
 * 设计原则（重要）：
 *   本脚本 **不伪造心跳包、不重算 enc 签名**。
 *   它只做一件事：让超星自己的播放器 JS 以为「真人正在正常观看」，
 *   从而由播放器自己发出合法的 multimedia/log 心跳。
 *   这样既不吃 enc 算法变更的风险，也不构成对服务端接口的直接攻击。
 *
 * 针对超星的具体反作弊点：
 *   1. 切后台/失焦 → 视频自动暂停，且心跳停发
 *   2. 鼠标长时间静止 → isdrag 判定异常，进度不计
 *   3. 拖动进度条 → isdrag=1，该段进度作废（所以要"匀速前进"）
 *   4. 倍速过高 → 服务端校验 playingTime 增速，超阈值不计数
 *   5. 播放器会周期性重置 playbackRate
 */

(function () {
  if (window.__cx_installed) return;
  window.__cx_installed = true;

  const CFG = window.__cx_config || {};
  const RATE = CFG.rate || 1.5;          // 超星对倍速敏感，默认保守 1.5
  const JITTER = CFG.mouseJitter !== false;
  const KEEP_FOCUS = CFG.keepFocus !== false;
  const DEBUG = CFG.debug !== false;

  const log = (...a) => DEBUG && console.log('%c[CX]', 'color:#0a0', ...a);
  log('保活脚本注入，rate=' + RATE);

  // =================================================================
  // 1. 焦点 / 可见性伪造
  //    超星用 visibilitychange + blur + 定期 document.hidden 轮询三重检测
  // =================================================================
  try {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    Object.defineProperty(document, 'webkitHidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'webkitVisibilityState', { get: () => 'visible', configurable: true });
  } catch (e) { log('属性重定义受限', e.message); }

  // 吞掉事件（capture 阶段拦截，阻止到达超星监听器）
  ['visibilitychange', 'webkitvisibilitychange', 'blur', 'pagehide', 'freeze']
    .forEach((evt) => {
      const swallow = (e) => { e.stopImmediatePropagation(); };
      document.addEventListener(evt, swallow, true);
      window.addEventListener(evt, swallow, true);
    });

  try { window.onblur = null; window.onfocus = null; } catch (e) {}

  // 周期性地主动派发 focus，抵消超星可能有的事件式检测
  if (KEEP_FOCUS) {
    setInterval(() => {
      try {
        window.dispatchEvent(new Event('focus'));
        document.dispatchEvent(new Event('focus'));
      } catch (e) {}
    }, 15000);
  }

  // =================================================================
  // 2. 鼠标活动模拟
  //    超星靠 mousemove 判定"人在电脑前"。间隔随机化，避免机器特征。
  // =================================================================
  if (JITTER) {
    const rnd = (n) => Math.floor(Math.random() * n);
    const jitter = () => {
      const x = rnd(900) + 100, y = rnd(600) + 100;
      document.dispatchEvent(new MouseEvent('mousemove', {
        bubbles: true, cancelable: true,
        clientX: x, clientY: y, screenX: x, screenY: y,
        movementX: rnd(9) - 4, movementY: rnd(9) - 4,
      }));
      // 极轻微滚动，模拟"人眼扫读"，幅度限制在 1px 内不触发懒加载
      if (Math.random() < 0.2) window.scrollBy(0, Math.random() < 0.5 ? 1 : -1);
      setTimeout(jitter, 15000 + rnd(30000));   // 15~45s 随机
    };
    setTimeout(jitter, 2000 + rnd(3000));
  }

  // =================================================================
  // 3. 播放器加固
  // =================================================================
  function harden(v) {
    if (!v || v.__cx_hardened) return v ? v : null;
    v.__cx_hardened = true;

    // --- 拦截 pause ---
    const rawPause = v.pause.bind(v);
    v.__cx_rawPause = rawPause;
    v.pause = function () {
      if (window.__cx_force) return rawPause();
      log('拦截了一次外部 pause');
      return undefined;
    };

    // --- 倍速维持：超星会定时把 rate 打回 1.0 ---
    v.addEventListener('ratechange', () => {
      const target = window.__cx_rate || RATE;
      if (Math.abs(v.playbackRate - target) > 0.01) {
        v.playbackRate = target;
      }
    });

    // --- 被暂停即刻恢复 ---
    v.addEventListener('pause', () => {
      if (!window.__cx_running || window.__cx_force) return;
      setTimeout(() => {
        try { v.playbackRate = window.__cx_rate || RATE; } catch (e) {}
        const p = v.play();
        if (p && p.catch) p.catch(() => {});
      }, 200);
    });

    // --- 静音自动播放（超星的 autoplay 策略要求） ---
    v.muted = true;
    try { v.playbackRate = window.__cx_rate || RATE; } catch (e) {}

    if (v.paused) {
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    }
    return v;
  }

  function scan() {
    const list = Array.from(document.querySelectorAll('video'));
    // 取时长最长的，避开超星页面上的广告/预览小窗
    if (list.length) {
      const main = list.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
      harden(main);
      list.forEach(harden);
    }
  }

  scan();
  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });

  // 超星是 iframe 嵌套结构，同样处理子框架
  try {
    document.querySelectorAll('iframe').forEach((f) => {
      f.addEventListener('load', () => { try { scan(); } catch (e) {} });
    });
  } catch (e) {}

  // =================================================================
  // 4. 心跳监控：记录超星自己发出的 multimedia/log 请求
  //    仅用于观测与诊断，不篡改、不伪造。
  // =================================================================
  window.__cx_heartbeats = [];
  const HB_RE = /multimedia\/log|\/log\/a\//i;

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (...args) {
      const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
      const p = origFetch.apply(this, args);
      if (HB_RE.test(url)) {
        p.then((r) => r.clone().text().then((t) => {
          const rec = { url: url.slice(0, 300), body: String(t).slice(0, 200), at: Date.now() };
          window.__cx_heartbeats.push(rec);
          if (window.__cx_heartbeats.length > 300) window.__cx_heartbeats.shift();
          log('心跳', rec.body);
        }).catch(() => {})).catch(() => {});
      }
      return p;
    };
  }

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__cx_url = url;
    if (HB_RE.test(String(url))) {
      this.addEventListener('load', () => {
        window.__cx_heartbeats.push({
          url: String(url).slice(0, 300),
          body: String(this.responseText || '').slice(0, 200),
          at: Date.now(),
        });
        if (window.__cx_heartbeats.length > 300) window.__cx_heartbeats.shift();
      });
    }
    return origOpen.call(this, method, url, ...rest);
  };

  // =================================================================
  // 5. 状态接口，供 Python 侧轮询
  // =================================================================
  window.__cx_status = function () {
    const vids = Array.from(document.querySelectorAll('video'));
    if (!vids.length) {
      // 超星未播放时可能只渲染成 div 占位，需要点一下播放按钮
      const playBtn = document.querySelector(
        '.vjs-big-play-button, .ans-video-PlayBtn, .videoArea .play, [class*="playBtn"]'
      );
      return { hasVideo: false, needsClick: !!playBtn };
    }
    const v = vids.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
    const hbs = window.__cx_heartbeats || [];
    const last = hbs.length ? hbs[hbs.length - 1] : null;
    return {
      hasVideo: true,
      paused: v.paused,
      ended: v.ended,
      muted: v.muted,
      rate: v.playbackRate,
      currentTime: v.currentTime,
      duration: v.duration || 0,
      progress: v.duration ? v.currentTime / v.duration : 0,
      finished: v.ended || (v.duration > 0 && v.duration - v.currentTime < 2),
      heartbeatCount: hbs.length,
      lastHeartbeat: last,
      // 心跳停摆超过 100 秒说明保活失效（超星本身约 60s 发一次 multimedia/log，
      // 阈值必须大于其间隔，否则会误报）
      heartbeatStalled: last ? (Date.now() - last.at > 100000) : false,
    };
  };

  window.__cx_rate = RATE;
  window.__cx_running = true;
})();
