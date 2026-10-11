/**
 * 反检测 + 保活注入脚本
 *
 * 设计目标：让页面「以为」视频在被真人正常观看。
 * 所有注入均为幂等操作，重复注入不会出错。
 *
 * 覆盖四类常见反作弊手段：
 *   1. 失焦检测 —— visibilitychange / document.hidden / blur / mouseleave
 *   2. 鼠标静止检测 —— 长时间无 mousemove 判定为挂机
 *   3. 播放器元素劫持 —— video.pause() 被外部调用强行暂停
 *   4. 倍速被重置 —— 平台定时把 playbackRate 打回 1.0
 */

(function () {
  if (window.__acp_installed) return;
  window.__acp_installed = true;

  const CFG = window.__acp_config || {};
  const JITTER = CFG.mouseJitter !== false;
  const FORCE_RATE = CFG.playbackRate || 1.0;

  // ---------------------------------------------------------------
  // 1. 失焦检测拦截
  // ---------------------------------------------------------------
  try {
    Object.defineProperty(document, 'hidden', {
      get: () => false,
      configurable: true,
    });
    Object.defineProperty(document, 'visibilityState', {
      get: () => 'visible',
      configurable: true,
    });
    Object.defineProperty(document, 'webkitHidden', {
      get: () => false,
      configurable: true,
    });
  } catch (e) {
    /* 某些浏览器不允许重定义，忽略 */
  }

  // 吞掉所有可见性 / 焦点相关事件，阻止它们冒泡到平台监听器
  const SWALLOW = [
    'visibilitychange',
    'webkitvisibilitychange',
    'blur',
    'mozvisibilitychange',
    'pagehide',
    'freeze',
  ];
  SWALLOW.forEach((evt) => {
    document.addEventListener(
      evt,
      (e) => {
        e.stopImmediatePropagation();
        e.preventDefault();
      },
      true
    );
    window.addEventListener(
      evt,
      (e) => {
        e.stopImmediatePropagation();
        e.preventDefault();
      },
      true
    );
  });

  // 屏蔽 onblur / onfocus 属性式监听
  try {
    window.onblur = null;
    window.onfocus = null;
    document.onvisibilitychange = null;
  } catch (e) {}

  // ---------------------------------------------------------------
  // 2. 鼠标活动模拟（对抗"静止即挂机"策略）
  // ---------------------------------------------------------------
  function dispatchMouseMove() {
    const x = Math.floor(Math.random() * 600) + 100;
    const y = Math.floor(Math.random() * 400) + 100;
    const evt = new MouseEvent('mousemove', {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
      screenX: x,
      screenY: y,
      movementX: Math.floor(Math.random() * 7) - 3,
      movementY: Math.floor(Math.random() * 7) - 3,
    });
    document.dispatchEvent(evt);

    // 周期性补一次微滚动，绕过"滚动惰性"检测
    if (Math.random() < 0.15) {
      window.scrollBy(0, Math.random() < 0.5 ? 1 : -1);
    }
  }

  if (JITTER) {
    const tick = () => {
      dispatchMouseMove();
      // 20~45 秒随机间隔，避免固定周期的机器特征
      setTimeout(tick, 20000 + Math.random() * 25000);
    };
    setTimeout(tick, 3000);
  }

  // ---------------------------------------------------------------
  // 3. 播放器劫持防护
  // ---------------------------------------------------------------
  const MIN_RATE = CFG.minRate || FORCE_RATE;

  function hardenVideo(v) {
    if (!v || v.__acp_hardened) return;
    v.__acp_hardened = true;

    // 拦截 pause：外部调用一律拒绝，脚本自身通过 __acp_allowPause 放行
    const rawPause = v.pause.bind(v);
    v.pause = function () {
      if (window.__acp_allowPause) return rawPause();
      return undefined;
    };
    v.__acp_rawPause = rawPause;

    // 维持倍速，抵抗平台的重置行为
    v.addEventListener('ratechange', () => {
      if (Math.abs(v.playbackRate - MIN_RATE) > 0.01) {
        v.playbackRate = MIN_RATE;
      }
    });

    // 被暂停则立刻恢复
    v.addEventListener('pause', () => {
      if (window.__acp_running && !window.__acp_allowPause) {
        setTimeout(() => {
          const p = v.play();
          if (p && p.catch) p.catch(() => {});
        }, 120);
      }
    });

    // 自动恢复播放
    if (v.paused) {
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    }
  }

  function scanVideos() {
    document.querySelectorAll('video').forEach(hardenVideo);
  }

  scanVideos();

  // 动态插入的播放器（SPA 切课常会重建 video 节点）
  const mo = new MutationObserver(() => scanVideos());
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // ---------------------------------------------------------------
  // 4. 状态查询接口，供 Python 侧轮询
  // ---------------------------------------------------------------
  window.__acp_status = function () {
    const vids = Array.from(document.querySelectorAll('video'));
    if (!vids.length) {
      return { hasVideo: false, finished: false };
    }
    // 选时长最长的那个，避开广告 / 预览片
    const v = vids.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
    return {
      hasVideo: true,
      paused: v.paused,
      ended: v.ended,
      muted: v.muted,
      rate: v.playbackRate,
      currentTime: v.currentTime,
      duration: v.duration || 0,
      progress: v.duration ? v.currentTime / v.duration : 0,
      // 视频自身播完 + 距结尾 2 秒内，视为本节完成
      finished: v.ended || (v.duration > 0 && v.duration - v.currentTime < 2),
      videoCount: vids.length,
    };
  };

  window.__acp_running = true;
})();
