#!/usr/bin/env python3
"""
chaoxing_player.py —— 超星学习通（泛雅 MOOC2-ANS）自动播放器

作者: zkk    日期: 2026.10.11    版本: v1.0

与通用 player.py 的区别：
  - 处理超星特有的 iframe 嵌套（播放器在子框架里）
  - 自动遍历左侧章节树，逐节点播放
  - 监控超星自身发出的 multimedia/log 心跳，判断保活是否生效
  - 倍速默认保守（1.5x），因为超星服务端会校验 playingTime 增速

前置条件：
  1. 手动登录一次，登录态持久化到 profile 目录
  2. 用 --login 参数首次运行以完成登录

用法：
    # 首次：弹出浏览器手动登录
    python3 chaoxing_player.py --config configs/chaoxing.json --login

    # 之后：直接跑
    python3 chaoxing_player.py --config configs/chaoxing.json

    # 只播当前页
    python3 chaoxing_player.py --config configs/chaoxing.json --single
"""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

# ---- 版本信息（启动时打印，更新版本改这里即可）--------------------
__VERSION__ = "v1.0"
__AUTHOR__ = "zkk"
__BUILD_DATE__ = "2026.10.11"

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write("缺少 playwright：sudo pip3 install playwright\n")
    sys.exit(2)

HERE = Path(__file__).resolve().parent
CX_JS = (HERE / "chaoxing.js").read_text(encoding="utf-8")


class Log:
    def __init__(self, logfile=None):
        self.logfile = Path(logfile) if logfile else None
        if self.logfile:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg, level="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level:5}] {msg}"
        print(line, flush=True)
        if self.logfile:
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
def _is_disabled_browser(path):
    """路径里含 disabled / .broken 的视为禁用/损坏内核，不选用。"""
    low = str(path).lower()
    return "disabled" in low or ".broken" in low


def _chromium_version(path):
    """从路径里提取 chromium-<数字> 的版本号，用于排序（越大越新）。"""
    m = re.search(r"chromium-(\d+)", str(path).lower())
    return int(m.group(1)) if m else 0


def find_browser(preferred=None):
    """
    跨平台查找可用的 Chromium 内核浏览器。

    查找顺序：
      0. 配置里显式指定的 browserPath（最优先，用户说了算）
      1. Playwright 自带的内核（最可靠，playwright install chromium 装的就是它）
      2. 系统已安装的 Chrome / Chromium / Edge
    Windows 与 macOS 的安装路径都已覆盖。

    注意：会跳过名字里带 disabled / .broken 的 Playwright 内核目录，
    这类目录通常是被人为禁用或安装损坏的残留（缺 SxS 清单，启动会报
    WinError 14001 并行配置错误），不能选。
    """
    import glob

    def _usable(p):
        return bool(p) and os.path.exists(p) and not _is_disabled_browser(p)

    # --- 0. 配置显式指定 -------------------------------------------
    if preferred:
        p = os.path.expanduser(str(preferred).strip().strip('"'))
        if _usable(p):
            return p
        # 配置写了但不可用 → 打印到 stderr，继续走自动探测
        sys.stderr.write(f"[warn] browserPath 不可用，忽略：{preferred}\n")

    # --- 1. Playwright 自带内核 -------------------------------------
    candidates = []
    for base in (
        os.path.expanduser("~/.cache/ms-playwright"),
        os.path.expanduser("~/Library/Caches/ms-playwright"),          # macOS
        os.path.expanduser("~/AppData/Local/ms-playwright"),           # Windows
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    ):
        if base and os.path.isdir(base):
            candidates += glob.glob(os.path.join(base, "chromium-*", "chrome-*", "chrome"))
            candidates += glob.glob(os.path.join(base, "chromium-*", "chrome-*", "chrome.exe"))
            candidates += glob.glob(os.path.join(base, "chromium-*", "chrome-*", "Chromium.app",
                                                 "Contents", "MacOS", "Chromium"))
    # 过滤掉禁用/损坏的残留目录，并优先选版本号更高的（新装的更可靠）
    candidates = [c for c in candidates if not _is_disabled_browser(c)]
    candidates.sort(key=_chromium_version, reverse=True)
    for c in candidates:
        if os.path.exists(c):
            return c

    # --- 2. 系统已安装的浏览器 --------------------------------------
    system_paths = [
        # Linux
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome", "/usr/local/bin/chromium",
        "/opt/google/chrome/google-chrome",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in system_paths:
        if os.path.exists(p):
            return p

    # --- 3. 从 PATH 里找 -------------------------------------------
    import shutil
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome",
                 "msedge", "Microsoft Edge"):
        found = shutil.which(name)
        if found:
            return found

    return None


def safe_goto(page, url, timeout=90000, settle_sec=3.0):
    """
    健壮导航：容忍「被其它导航中断」。

    超星这类 SPA 在页面载入后会用 JS 立刻做一次重定向
    （例如从 mooc2-ans/mycourse/stu 跳到 mooc1/mycourse/studentstudy）。
    此时 Playwright 对 *同一次* goto 调用会抛出：
        Error: Page.goto: Navigation to "..." is interrupted by another navigation
    这不是错误，恰恰说明页面按预期跳走了。因此这里：
      1. 用 wait_until="commit" 发起（只要求导航被接收，不做严格等待）
      2. 捕获 "interrupted by another navigation" 并静默忽略
      3. 之后轮询等 URL 稳定下来，再返回
    返回最终 URL。
    """
    try:
        page.goto(url, wait_until="commit", timeout=timeout)
    except PWTimeout:
        # 超时也先放行，后面靠 settle 轮询判断
        pass
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "interrupted by another navigation" in msg or "net::ERR_ABORTED" in msg:
            pass  # 页面自己跳走了，正常现象
        else:
            raise

    # 等 URL 稳定：连续 3 次采样相同即认为跳转结束（更抗抖动）
    last = None
    same = 0
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            cur = page.url
        except Exception:
            break
        if cur == last:
            same += 1
            if same >= 2:      # 加上本次共 3 次一致
                break
        else:
            same = 0
        last = cur
        time.sleep(0.4)

    # 再给页面一点渲染时间（超星骨架屏 → 真实内容）
    if settle_sec:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=int(settle_sec * 1000))
        except Exception:
            pass
    return last or url


def port_alive(port):
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_logged_in(page):
    """
    粗略判断当前是否"看起来已登录"（仅作提示用，不再作为自动判断依据）。
    规则简单：URL 落在登录页（passport/login）→ 未登录；否则视为已登录。
    注意：不再检测页面里的登录表单元素 —— 超星业务页内置隐藏的登录弹层/二维码
    容器，会导致"已登录却报未登录"的误判（历史 bug）。
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "passport" in url or "/login" in url or "login." in url:
        return False
    return True


def wait_login_manual(page, log, hint="请在浏览器窗口中完成登录（扫码或账密）"):
    """
    等待用户在浏览器里完成登录：打印提示后，等待用户按回车继续。
    （用户明确要求用"按回车"方式，比自动检测更直观、不会误判。）
    """
    log("=" * 56)
    log(hint)
    log("登录成功后，回到这里按回车继续…")
    log("=" * 56)
    try:
        input()
    except EOFError:
        pass
    log("继续执行")
    return True


def has_display():
    """
    判断是否存在**可用**的图形显示。

    注意：容器环境常设置 DISPLAY=:0 但实际没有 X server 在跑。
    仅检查环境变量会导致误判，进而以有头模式启动浏览器并立即崩溃
    （报错：Missing X server or $DISPLAY）。
    因此这里做实测：尝试连接 X11 socket / 查询 xdpyinfo。

    Windows / macOS 没有 DISPLAY 的概念，GUI 原生可用，直接返回 True。
    """
    if sys.platform in ("win32", "darwin"):
        return True

    disp = os.environ.get("DISPLAY")
    if not disp:
        return False

    # UNIX socket 形式，如 :0 -> /tmp/.X11-unix/X0
    m = re.match(r"^:(\d+)", disp)
    if m:
        sock = f"/tmp/.X11-unix/X{m.group(1)}"
        if not os.path.exists(sock):
            return False

    # 变量存在且 socket 存在，再用 xdpyinfo 做最终确认
    try:
        r = subprocess.run(
            ["xdpyinfo"], capture_output=True, timeout=3,
            env={**os.environ, "DISPLAY": disp},
        )
        return r.returncode == 0
    except Exception:
        # xdpyinfo 不可用时，退回 socket 存在性判断
        return bool(m)


class CXPlayer:
    # ---- 任务点处理器登记表 ---------------------------------------
    # 后续要支持新的任务点类型（如问卷、投票、签到…）只需两步：
    #   ① 在本类里写一个方法 try_xxx(self, page, label="")，返回
    #        "done"   已成功处理（视为本节点完成）
    #        "failed" 识别到该类型但处理失败（记 failed，下次重试）
    #        "none"   不是这类节点（继续尝试下一个处理器）
    #   ② 在下面的列表加一行：(配置 features 里的开关名, 方法名)
    # 主流程会自动按列表顺序调用，run() 不用改。
    # 也可以只改 config 的 features，把某类临时关掉（设为 false）。
    TASK_HANDLERS = [
        ("quiz", "try_quiz"),
        ("document", "try_document"),     # 文档 + 音频任务点
        ("discussion", "try_discussion"), # 讨论/课后讨论任务点
    ]

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.behavior = cfg.get("behavior", {})
        self.stats = {"done": 0, "empty": 0, "failed": 0, "stuck": 0, "drifted": 0}
        # 含 video 的 frame 缓存：超星页面常有 20+ 个 iframe，
        # 每轮 poll 全量遍历 evaluate 开销大。命中后记下来，之后直查。
        self._vframe = None
        # 连续停滞计数（play_current 每次进入时归零）
        self._stall_n = 0
        # AI 配置：apiKey 允许用环境变量 CX_AI_KEY 覆盖，
        # 避免把明文 key 长期留在配置文件里
        self.ai = dict(cfg.get("ai", {}) or {})
        env_key = os.environ.get("CX_AI_KEY")
        if env_key:
            self.ai["apiKey"] = env_key

    # 播放器/章节内容相关 frame 的 URL 特征
    _FRAME_KW = ("ananas", "/video", "knowledge", "mooc", "course", "study")

    # ---- 注入 -----------------------------------------------------
    def inject(self, page, ctx, full=False):
        """
        超星播放器可能在主框架或 iframe。
        默认只注入 主文档 + 内容相关 frame + 已知 video frame
        （超星页面常有 20+ 个 iframe，全量注入几十 KB 的 JS 开销不小）；
        full=True 时全量注入（保活失效时的兜底）。
        """
        payload = {
            "rate": self.behavior.get("playbackRate", 1.5),
            "mouseJitter": self.behavior.get("mouseJitter", True),
            "keepFocus": True,
            "debug": self.behavior.get("verbose", False),
        }
        targets = [page]
        for f in page.frames:
            if f == page.main_frame:
                continue
            furl = (f.url or "").lower()
            if full or not furl or any(k in furl for k in self._FRAME_KW):
                targets.append(f)
        if self._vframe is not None and self._vframe not in targets:
            targets.append(self._vframe)
        for target in targets:
            try:
                target.evaluate("(c) => { window.__cx_config = c; }", payload)
                target.evaluate(CX_JS)
            except Exception:
                pass

    def inject_all_frames(self, page):
        for f in page.frames:
            try:
                f.evaluate("() => { window.__cx_running = true; }")
            except Exception:
                pass

    # ---- 处理"点击播放"占位 --------------------------------------
    def try_start_video(self, page, ctx):
        """
        超星初始是封面图 + 播放按钮，需要点一下才挂载 video。
        播放器在 ananas/modules/video/ 这个 iframe 里，所以遍历所有 frame。
        关键：必须用**真实鼠标轨迹**（move+down+up），el.click() 对超星无效。

        安全约束（防止"播放一会跳转到别的页面"）：
        - 只点明确的「播放按钮」类选择器，**绝不点 video 元素本身**
          （点 video 中心会误触播放/暂停切换，甚至触发页面上的跳转覆盖层）；
        - 只在 ananas/video 这类播放器 frame 里操作，避免误点主文档里的课程链接。
        """
        candidates = [
            ".vjs-big-play-button",
            ".ans-video-PlayBtn",
            ".videoArea .play",
            "[class*='playBtn']",
            "[class*='play-btn']",
            ".vjs-play-control.vjs-paused",
            ".vjs-play-control[title*='播放']",
        ]
        for f in page.frames:
            # 只处理播放器相关 frame，避免点到主文档的课程/章节链接
            furl = (f.url or "").lower()
            if not any(k in furl for k in ("ananas", "/video", "modules/video")):
                continue
            for sel in candidates:
                try:
                    el = f.query_selector(sel)
                    if el and el.is_visible():
                        el.scroll_into_view_if_needed(timeout=2500)
                        time.sleep(0.3)
                        box = el.bounding_box()
                        if box:
                            cx = box["x"] + box["width"] / 2
                            cy = box["y"] + box["height"] / 2
                            page.mouse.move(cx - 20, cy - 8)
                            time.sleep(0.15)
                            page.mouse.move(cx, cy, steps=6)
                            time.sleep(0.2)
                            page.mouse.down()
                            time.sleep(0.07)
                            page.mouse.up()
                        else:
                            el.click(timeout=2500)
                        self.log(f"点击播放控件: {sel} (frame={f.url[:50]})")
                        time.sleep(2)
                        return True
                except Exception:
                    continue
        return False

    # ---- 用 JS 直接播放（不点击，最安全） -------------------------
    def js_play(self, page):
        """
        直接对所有 frame 里的 video 调 play() 并设 muted，
        完全不做鼠标点击 —— 用于 video 已挂载但暂停的场景，
        避免 try_start_video 的点击误触页面跳转。
        """
        rate = self.behavior.get("playbackRate", 1.5)
        hit = False
        for f in page.frames:
            try:
                ok = f.evaluate(
                    f"""() => {{
                        const v = document.querySelector('video');
                        if (!v) return false;
                        try {{ v.muted = true; }} catch (e) {{}}
                        if (Math.abs(v.playbackRate - {rate}) > 0.01) v.playbackRate = {rate};
                        if (v.paused) {{ const p = v.play(); if (p && p.catch) p.catch(()=>{{}}); }}
                        return true;
                    }}"""
                )
                if ok:
                    hit = True
            except Exception:
                continue
        return hit

    # ---- 读状态（穿透 iframe） -----------------------------------
    def status(self, page):
        """先查缓存的 video frame，失效再全量扫描。"""
        # 1) 缓存命中：一次 evaluate 直接拿状态
        if self._vframe is not None:
            try:
                st = self._vframe.evaluate(
                    "() => (window.__cx_status ? window.__cx_status() : null)")
                if st and st.get("hasVideo"):
                    return st
            except Exception:
                pass
            self._vframe = None   # 缓存失效（切章/重载），进入全量扫描
        # 2) 全量扫描，命中后写缓存
        for f in page.frames:
            try:
                st = f.evaluate("() => (window.__cx_status ? window.__cx_status() : null)")
                if st and st.get("hasVideo"):
                    self._vframe = f
                    return st
            except Exception:
                continue
        # 退化：直接查 video
        for f in page.frames:
            try:
                st = f.evaluate(
                    """() => {
                        const vs = Array.from(document.querySelectorAll('video'));
                        if (!vs.length) return null;
                        const v = vs.sort((a,b)=>(b.duration||0)-(a.duration||0))[0];
                        return {
                            hasVideo: true, paused: v.paused, ended: v.ended,
                            rate: v.playbackRate, currentTime: v.currentTime,
                            duration: v.duration||0,
                            progress: v.duration ? v.currentTime/v.duration : 0,
                            finished: v.ended || (v.duration>0 && v.duration-v.currentTime<2),
                            heartbeatCount: 0, heartbeatStalled: false,
                        };
                    }"""
                )
                if st:
                    self._vframe = f
                    return st
            except Exception:
                continue
        return {"hasVideo": False, "needsClick": True}

    # ---- 播放单节 -------------------------------------------------
    # ---- 任务点级完成检测（播放页内的绿勾「任务点已完成」） ----------
    def task_point_finished(self, page, debug=False):
        """
        检测当前章节的任务点是否已完成（截图证据：播放页视频卡片上方
        显示绿勾「任务点已完成」）。已完成则无需再播放。

        超星的完成标记可能出现在多种位置，逐一探测（任一命中即算完成）：
          1) 可见文本含「任务点已完成」（排除「未完成/待完成」）
          2) 元素 title / aria-label 属性含「已完成」
          3) 元素 class 含完成语义（icon_yiwanc / icon_Completed / done / complete）
          4) 视频卡片的完成角标（图片 alt / 伪元素 content）

        debug=True 时返回详细探测结果 dict，便于线上定位真实结构。
        """
        js = r"""() => {
            const out = { text:false, attr:false, cls:false, videoDone:false,
                          hits: [], texts: [], unfinished:false, url: location.href };
            // 排除「未完成」干扰
            const bt = (document.body && document.body.innerText) || '';
            // 归一化空白（去掉全角/半角空格、换行），兼容「任务点 已完成」等变体
            const btNorm = bt.replace(/[\s\u3000]+/g, '');
            out.unfinished = btNorm.indexOf('任务点未完成') >= 0
                          || btNorm.indexOf('任务点待完成') >= 0
                          || btNorm.indexOf('未完成任务点') >= 0;
            // ---- 1) 正文文本 ----
            if (btNorm.indexOf('任务点已完成') >= 0
                || btNorm.indexOf('已完成任务点') >= 0) {
                out.text = true; out.hits.push('text:任务点已完成');
            }
            // 把含「完成」的行收集起来（调试用，最多 12 行）
            try {
                bt.split('\n').forEach(l => {
                    l = l.trim();
                    if (l && l.indexOf('完成') >= 0 && out.texts.length < 12)
                        out.texts.push(l.slice(0, 60));
                });
            } catch(e) {}
            // ---- 2) 属性 title / aria-label ----
            try {
                document.querySelectorAll('[title],[aria-label],[alt]').forEach(el => {
                    const v = (el.getAttribute('title')||'') + '|' +
                              (el.getAttribute('aria-label')||'') + '|' +
                              (el.getAttribute('alt')||'');
                    if (v.indexOf('已完成') >= 0 || v.indexOf('已完成') >= 0) {
                        out.attr = true;
                        if (out.hits.length < 20) out.hits.push('attr:' + v.slice(0,60));
                    }
                });
            } catch(e) {}
            // ---- 3) class 完成语义 ----
            try {
                const doneCls = /(icon_yiwanc|icon_Completed|icon_done|yiwanc|is-?done|is-?complete|finished|completed)/i;
                document.querySelectorAll('*').forEach(el => {
                    const c = el.className;
                    if (typeof c === 'string' && doneCls.test(c)) {
                        out.cls = true;
                        if (out.hits.length < 24) out.hits.push('cls:' + c.slice(0,60));
                    }
                });
            } catch(e) {}
            // ---- 4) 视频卡片完成角标 ----
            try {
                document.querySelectorAll('.taskPoint, .ans-job-icon, .jobFinish, .icon_yiwanc, [class*="finish"], [class*="complete"]').forEach(el => {
                    const t = (el.textContent||'').trim();
                    const c = (el.className||'') + '';
                    if (t.indexOf('已完成') >= 0 || /yiwanc|completed|finished|done/i.test(c)) {
                        out.videoDone = true;
                        if (out.hits.length < 28) out.hits.push('card:' + c.slice(0,50) + '#' + t.slice(0,20));
                    }
                });
            } catch(e) {}
            // ---- 5) 是否仍有未完成的媒体任务点（视频/音频）----
            // 关键：章节可能含多个任务点（如 简答+音频）。只因某个任务点的绿勾
            // 就判定整章完成会漏掉其余任务点。存在"未播完媒体"时视为未完成。
            // 注意：必须先排除「占位/提示音」元素——无 src、或不可见且时长
            // ≤10s 且不在任务点容器里的 <audio> 一律不算待完成媒体，
            // 否则一个隐藏提示音会让整章永远无法判完成。
            try {
                const inTaskArea = (el) => {
                    let n = el;
                    for (let i = 0; i < 6 && n; i++) {
                        const idc = String(n.className || '') + ' ' + String(n.id || '');
                        if (/ans-|job|taskpoint|task-point|attach|knowledge/i.test(idc)) return true;
                        n = n.parentElement;
                    }
                    return false;
                };
                const looksReal = (m) => {
                    if (!m) return false;
                    const hasSrc = !!(m.currentSrc || m.src);
                    if (!hasSrc) return false;                       // 无源占位元素
                    const r = m.getBoundingClientRect();
                    const visible = !!(r.width || r.height);
                    const d = (isFinite(m.duration) ? m.duration : 0);
                    if (m.tagName === 'AUDIO') {
                        // 真实课程音频以分钟计；提示音只有 1~2 秒且通常不可见
                        return d > 10 || visible || inTaskArea(m);
                    }
                    return visible || d > 10 || inTaskArea(m);
                };
                const isPending = (m) => {
                    if (!looksReal(m)) return false;
                    const d = m.duration || 0;
                    const ct = m.currentTime || 0;
                    // ended 或 已接近结尾 → 不算 pending
                    if (m.ended) return false;
                    if (d > 0 && (d - ct) < 2) return false;
                    return true;   // 未 ended 且未接近结尾 → 视为待完成
                };
                document.querySelectorAll('video, audio').forEach(m => {
                    if (isPending(m)) out.pendingMedia = true;
                });
            } catch(e) {}
            return out;
        }"""
        res = {"finished": False, "unfinished": False, "hits": [], "detail": [],
               "url": ""}
        for f in page.frames:
            try:
                r = f.evaluate(js)
            except Exception:
                continue
            if not r:
                continue
            if r.get("unfinished"):
                res["unfinished"] = True
            hit_here = bool(r.get("text") or r.get("attr") or r.get("cls")
                            or r.get("videoDone"))
            if hit_here:
                res["finished"] = True
                res["url"] = r.get("url", "")
                for h in (r.get("hits") or [])[:8]:
                    res["hits"].append(h)
            # 仍有关联的未播完媒体（视频/音频）任务点 → 整章视为未完成，
            # 不能因为某个已完成任务点的绿勾就跳走
            if r.get("pendingMedia"):
                res["pendingMedia"] = True
            if debug and (r.get("texts") or r.get("hits")):
                res["detail"].append({
                    "url": (r.get("url") or "")[:90],
                    "texts": r.get("texts") or [],
                    "hits": (r.get("hits") or [])[:8],
                })
        # 有明确「未完成」文案时，不因其他模糊信号误判
        if res["unfinished"]:
            res["finished"] = False
        # 仍有未播完的媒体任务点 → 整章未完成（绿勾只是某个子任务点的）
        if res.get("pendingMedia") and not res["unfinished"]:
            res["finished"] = False
            res["hits"].append("pendingMedia:仍有未播完视频/音频，不跳过")
        return res

    def play_current(self, page, idx, title=""):
        label = f"[{idx}] {title[:34]}" if title else f"[{idx}]"
        self._rate_fix_logged = False
        self._last_pct = -1
        self._green_skipped = False
        self._vframe = None     # 切章后 video frame 缓存作废
        self._stall_n = 0
        self.inject(page, page.context)

        max_min = self.behavior.get("maxMinutesPerLesson", 120)
        poll = self.behavior.get("pollIntervalSec", 5)
        stall = self.behavior.get("stallToleranceSec", 120)
        rate = self.behavior.get("playbackRate", 1.5)

        t0 = time.time()
        last_prog = -1.0
        last_change = time.time()
        announced = False
        clicked = False

        # ---- 任务点级完成检测：绿勾「任务点已完成」→ 直接跳过，不播 ----
        # 任务点标记可能随知识卡片 iframe 异步渲染，多轮探测（共 3 轮，间隔递增）
        dbg = bool(self.behavior.get("debugTaskPoint") or self.cfg.get("debug"))
        _waits = [0, 2, 3]
        for attempt, w in enumerate(_waits):
            if w:
                time.sleep(w)
            try:
                info = self.task_point_finished(page, debug=dbg)
                if info.get("finished"):
                    self.log(f"{label} 任务点已完成（绿勾标记），跳过播放")
                    for h in info.get("hits", [])[:4]:
                        self.log(f"    完成标记: {h}")
                    self._green_skipped = True
                    return "done"
                if dbg and attempt == len(_waits) - 1:
                    self.log(f"{label} [调试] 未检测到任务点完成标记；命中详情：", "WARN")
                    self.log(f"    unfinished={info.get('unfinished')} "
                             f"hits={info.get('hits')}", "WARN")
                    for d in (info.get("detail") or [])[:6]:
                        self.log(f"    frame: {d.get('url')}", "WARN")
                        self.log(f"      texts={d.get('texts')}", "WARN")
                        self.log(f"      hits={d.get('hits')}", "WARN")
            except Exception as e:
                if dbg:
                    self.log(f"{label} [调试] 任务点检测异常: {e}", "WARN")

        while True:
            el = time.time() - t0
            if el > max_min * 60:
                self.log(f"{label} 超过单节上限 {max_min} 分钟，跳过", "WARN")
                return "timeout"

            # ---- 页面漂移检测：播放中若被跳离播放页，记录并尝试回退 ----
            try:
                cur_url = page.url or ""
            except Exception:
                cur_url = ""
            if cur_url and "studentstudy" not in cur_url and "studentcourse" not in cur_url:
                # 不在播放页了（被自动跳转到课程列表/其他页）
                self.log(f"{label} 页面已被跳离播放页（{cur_url[:70]}），停止本节", "WARN")
                self.stats.setdefault("drifted", 0)
                self.stats["drifted"] += 1
                return "drift"

            st = self.status(page)

            if not st.get("hasVideo"):
                # 超星视频是懒加载：优先用 JS 直接播放（最安全，不点击），
                # 不行再点明确的播放按钮；空等几轮后判定"无视频节点"。
                if not clicked:
                    clicked = True
                    if self.js_play(page):
                        time.sleep(3)
                        continue
                    if self.try_start_video(page, page.context):
                        time.sleep(3)
                        continue
                # 首次出现"无 video"时，多等几轮（部分节点加载慢）
                if not hasattr(self, "_novideo_wait"):
                    self._novideo_wait = 0
                self._novideo_wait += 1
                if self._novideo_wait <= 4:
                    self.log(f"{label} 暂未发现 video，等待加载…（{self._novideo_wait}/4）")
                    time.sleep(poll)
                    continue
                self.log(f"{label} 无 video 元素（可能非视频节点）")
                self._novideo_wait = 0
                return "empty"
            else:
                # 已挂载 video 但暂停 → 用 JS 播放（不点击，避免跳转）
                if st.get("paused") and not st.get("ended"):
                    self.js_play(page)

            if st.get("ended") or st.get("finished") or st.get("progress", 0) > 0.985:
                dur = st.get("duration", 0)
                self.log(
                    f"{label} ✓ 完成  {dur/60:.1f} 分钟 用时 {el/60:.1f} 分钟  "
                    f"心跳 {st.get('heartbeatCount', 0)} 次"
                )
                return "done"

            # 倍速校准：超星播放器会把 >2x 的倍速强制打回 1x，
            # 前端 ratechange 监听斗不过它 → Python 侧轮询强制改回。
            # 只挑真正含 video 且倍速不符的 frame 操作，避免遍历 20+ 个空框架拖慢页面
            if st.get("hasVideo"):
                r_now = st.get("rate") or 1
                if abs(r_now - rate) > 0.01:
                    for f in page.frames:
                        try:
                            f.evaluate(
                                f"() => {{ const v = document.querySelector('video');"
                                f" if (v && Math.abs(v.playbackRate - {rate}) > 0.01)"
                                f" v.playbackRate = {rate}; }}")
                        except Exception:
                            continue
                    if not getattr(self, "_rate_fix_logged", False):
                        self._rate_fix_logged = True
                        self.log(f"{label} 倍速被页面重置，已强制校准为 {rate}x")

            if st["progress"] > last_prog + 0.0005:
                last_prog = st["progress"]
                last_change = time.time()
                if not announced:
                    self.log(
                        f"{label} 播放中  {st['duration']/60:.1f} 分钟 @ {st.get('rate', rate)}x"
                    )
                    announced = True
                # 实时进度：每前进 5% 打印一次
                pct = int(st["progress"] * 100)
                if pct - self._last_pct >= 5:
                    self._last_pct = pct
                    self.log(
                        f"{label} 进度 {pct}%  "
                        f"（{st['currentTime']/60:.1f}/{st['duration']/60:.1f} 分钟）"
                    )

            # 心跳停摆 = 保活失效，这是超星刷课失败的最常见原因
            if st.get("heartbeatStalled") and st.get("heartbeatCount", 0) > 0:
                self.log(f"{label} 心跳停摆，重新注入保活", "WARN")
                self.inject(page, page.context, full=True)

            if time.time() - last_change > stall:
                self._stall_n += 1
                self.log(f"{label} 进度停滞 {stall}s（{last_prog*100:.1f}%），"
                         f"尝试恢复（第 {self._stall_n} 次）", "WARN")
                self.stats["stuck"] += 1
                if self._stall_n >= 3:
                    # 连续 3 次停滞：JS 级恢复已无效，直接刷新播放页重来，
                    # 避免空转到 maxMinutesPerLesson 上限
                    self.log(f"{label} 连续停滞 {self._stall_n} 次，刷新播放页…", "WARN")
                    try:
                        safe_goto(page, page.url, timeout=60000)
                        time.sleep(self.behavior.get("pageSettleSec", 4))
                    except Exception:
                        pass
                    self._vframe = None
                    self.inject(page, page.context, full=True)
                    self._stall_n = 0
                    last_change = time.time()
                    continue
                self.inject(page, page.context)
                # video 在 iframe 里，必须穿透所有 frame 播放
                for f in page.frames:
                    try:
                        f.evaluate(
                            """() => {
                                window.__cx_force = true;
                                document.querySelectorAll('video').forEach(v => {
                                    v.muted = true;
                                    v.playbackRate = window.__cx_rate || 1.5;
                                    const p = v.play(); if (p && p.catch) p.catch(()=>{});
                                });
                                window.__cx_force = false;
                            }"""
                        )
                    except Exception:
                        continue
                # 再补一次真实鼠标点播放按钮（有的播放器不认 JS play()）
                try:
                    self.try_start_video(page, page.context)
                except Exception:
                    pass
                last_change = time.time()

            if int(el) > 0 and int(el) % 60 == 0:
                hb = st.get("heartbeatCount", 0)
                self.log(
                    f"{label} {st['progress']*100:5.1f}%  "
                    f"({st['currentTime']/60:.1f}/{st['duration']/60:.1f}min)  心跳 {hb}"
                )

            time.sleep(poll)

    # ---- 抽取章节树 -----------------------------------------------
    def collect_chapters(self, page):
        """
        解析超星左侧目录树，返回 [{url, title}]。
        目录可能在主框架，也可能在 iframe 里，所以遍历所有 frame。
        """
        js = """() => {
            const out = [];
            const seen = new Set();
            const sels = [
                '.chapter_item', '.catalog_points', '.posCatalog_select',
                '[class*="chapter"] a', '[class*="catalog"] a',
                '[class*="Chapter"] a', '.zj_item', '.timeline a',
                'li[class*="node"] a', '.nodeItem a'
            ];
            document.querySelectorAll(sels.join(',')).forEach(a => {
                const href = a.getAttribute('href') || '';
                const oc = a.getAttribute('onclick') || '';
                const t = (a.innerText || '').trim().replace(/\\s+/g, ' ');
                if (!t || t.length > 60) return;
                const looksChapter = /chapterId|nodeId|knowledgeId|jobid|objectId/i.test(href + oc)
                                     || /chapter|node|catalog/i.test(a.className || '');
                if (!looksChapter) return;
                const key = t;
                if (seen.has(key)) return;
                seen.add(key);
                out.push({ title: t, href: href.slice(0, 300) });
            });
            return out;
        }"""
        items = []
        # 主框架 + 所有子框架
        for f in ([page] + [x for x in page.frames if x != page.main_frame]):
            try:
                got = f.evaluate(js)
                if got:
                    items += got
            except Exception:
                continue
        # 去重（同一标题只留一个）
        uniq = {}
        for it in items:
            uniq.setdefault(it["title"], it)
        return list(uniq.values())

    # ---- 诊断：自动进入播放页并 dump 结构 -------------------------
    def diagnose(self, page, out_path=None):
        """
        全自动诊断：多策略尝试进入真实课程页，拿到结构后写 JSON。
        策略A: 真实鼠标点击章节
        策略B: 直接导航到 iframe(frame_content-zj) 的 studentcourse src
               （绕过「壳页 + 懒加载 iframe」——这是点击无效时的关键突破口）
        """
        import json as _json
        result = {"ok": False, "strategy": None, "snapshots": []}
        self.log("=" * 60)
        self.log("自动诊断：多策略进入课程页并分析结构")
        self.log("=" * 60)

        # ---------- 策略 A：真实鼠标点击章节 ----------
        self.log("策略A：真实鼠标点击章节")
        chap_info = self._find_first_chapter(page)
        self.log(f"  章节定位: {chap_info}")
        if chap_info:
            clicked = self._native_click_chapter(page, chap_info)
            self.log(f"  点击结果: {clicked}")
            if clicked and self._wait_loaded(page, result, timeout=15):
                result["ok"] = True
                result["strategy"] = "A:鼠标点击章节"
                self._dump(out_path, result)
                self.log(f"策略A 成功。结果: {out_path}")
                return

        # ---------- 策略 B：直接导航 iframe 的 studentcourse ----------
        self.log("策略B：直接导航到 iframe 的 studentcourse 地址")
        iframe_src = self._get_iframe_course_url(page)
        self.log(f"  iframe src: {iframe_src}")
        if iframe_src:
            safe_goto(page, iframe_src, timeout=90000)
            time.sleep(4)
            result["snapshots"].append(self._snap(page))

            # 关键：在 studentcourse 目录页抓全部章节明细（标题+chapterId）
            chapters = self._extract_chapters(page)
            result["chapters"] = chapters
            self.log(f"  抓取到 {len(chapters)} 个章节")

            # 试导航第一个带 chapterId 的章节，验证能否进播放页
            if chapters:
                first = next((c for c in chapters if c.get("chapterId")), None)
                if first:
                    test_url = self._build_study_url(page.url, first, iframe_src)
                    self.log(f"  试导航首章节: {test_url[:90]}")
                    if test_url:
                        safe_goto(page, test_url, timeout=90000)
                        if self._wait_loaded(page, result, timeout=25):
                            result["ok"] = True
                            result["strategy"] = "B:目录页抓章节+拼URL进播放页"
                            self._dump(out_path, result)
                            self.log(f"策略B 成功。结果: {out_path}")
                            return

            if self._wait_loaded(page, result, timeout=10):
                result["ok"] = True
                result["strategy"] = "B:导航iframe src"
                self._dump(out_path, result)
                self.log(f"策略B(部分)成功。结果: {out_path}")
                return

        # ---------- 都失败：记录最终状态 ----------
        result["snapshots"].append(self._snap(page))
        self._dump(out_path, result)
        self.log("所有策略均未检测到视频/子框架", "WARN")
        self.log(f"结果(失败): {out_path}")

    def _wait_course_tree(self, page, timeout=30):
        """
        等待章节树 #coursetree 渲染出来（studentcourse 页面是异步渲染，
        导航后立即提取会拿到空树）。跨所有 frame 检查 .posCatalog_select。
        超时后输出全标签页/全 frame 诊断，定位目录真实所在。
        """
        probe = """() => ({
            url: location.href,
            tree: document.querySelectorAll('.chapter_item[id^="cur"], #coursetree .posCatalog_select[id^="cur"]').length,
            any: document.querySelectorAll('.chapter_item, .posCatalog_select').length,
            coursetree: !!(document.querySelector('.chapter_item') || document.querySelector('#coursetree'))
        })"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in page.frames:
                try:
                    r = f.evaluate(probe)
                    if r and r.get("tree", 0) > 0:
                        self.log(f"章节树已加载（{r['tree']} 个节点，frame: {r['url'][:70]}）")
                        return True
                except Exception:
                    continue
            time.sleep(1.5)
        # ---------- 超时：输出诊断 ----------
        self.log("等待章节树超时，输出诊断：", "WARN")
        self.log(f"  当前 page.url = {page.url[:100]}", "WARN")
        try:
            for pi, p in enumerate(page.context.pages):
                self.log(f"  标签页[{pi}]: {p.url[:100]}", "WARN")
        except Exception:
            pass
        for fi, f in enumerate(page.frames):
            try:
                r = f.evaluate(probe)
                self.log(f"  frame[{fi}] tree={r['tree']} any={r['any']} "
                         f"coursetree={r['coursetree']} url={r['url'][:80]}", "WARN")
            except Exception as e:
                self.log(f"  frame[{fi}] 评估失败: {e}", "WARN")
        return False

    def _extract_chapters(self, page):
        """
        抓课程目录树。兼容两套标记（以真实 dump 为准）：
          新版（mooc2 studentcourse）：
            - 小节元素：div.chapter_item[id^="cur"]，chapterId = id 去 "cur"
            - 标题 = 元素 title 属性 / .clicktitle 文本；序号 = .catalog_sbar
            - 完成标记 = .icon_yiwanc（兼容 .catalog_state 上的 done 类、
              data-status、title 含"已完成"，以及"任务点已学完"等文案）
          旧版（posCatalog）：div.posCatalog_select[id^="cur"] + .icon_Completed
        返回 [{chapterId, title, done, num}]，按文档顺序。
        """
        js = """() => {
            const out = [];
            // 判断一个章节元素是否"已完成"：多标记 OR 判定
            const isDone = (el) => {
                // 1) 新版/旧版常见完成图标
                if (el.querySelector('.icon_yiwanc, .icon_Completed, .icon_done')) return true;
                // 2) catalog_state 上带完成类名
                const st = el.querySelector('.catalog_state');
                if (st) {
                    const c = (st.className || '').toLowerCase();
                    if (c.indexOf('yiwanc') >= 0 || c.indexOf('done') >= 0
                        || c.indexOf('complete') >= 0 || c.indexOf('finish') >= 0) return true;
                }
                // 3) 任意子元素 class 里含完成语义
                const inner = el.querySelector(
                    '[class*="yiwanc"], [class*="finished"], [class*="complete"], [class*="done"]');
                if (inner) return true;
                // 4) data-status / 属性
                const ds = (el.getAttribute('data-status')
                            || el.getAttribute('data-state') || '').toLowerCase();
                if (ds === 'done' || ds === 'finished' || ds === 'complete' || ds === '1') return true;
                // 5) 文案兜底：元素 title 或内部文本含"已完成/已学完"
                const txt = ((el.getAttribute('title') || '') + ' '
                             + (el.innerText || '')).slice(0, 200);
                if (/已完成|已学完|学习完成|已完成学习/.test(txt)) return true;
                return false;
            };
            let nodes = document.querySelectorAll('.chapter_item[id^="cur"]');
            let mode = 'new';
            if (!nodes.length) {
                const tree = document.querySelector('#coursetree') || document;
                nodes = tree.querySelectorAll('.posCatalog_select[id^="cur"]');
                mode = 'old';
            }
            nodes.forEach(el => {
                const id = el.id || '';
                const cid = id.replace(/^cur/, '');
                if (!/^\\d{6,}$/.test(cid)) return;
                let title = '', num = '';
                if (mode === 'new') {
                    title = (el.getAttribute('title')
                             || (el.querySelector('.clicktitle') || el).innerText)
                            .trim().replace(/\\s+/g, ' ');
                    const sbar = el.querySelector('.catalog_sbar');
                    num = sbar ? sbar.innerText.trim() : '';
                } else {
                    const nameEl = el.querySelector('.posCatalog_name');
                    const numEl = el.querySelector('.posCatalog_sbar');
                    title = (nameEl ? (nameEl.getAttribute('title') || nameEl.innerText) : el.innerText)
                            .trim().replace(/\\s+/g, ' ');
                    num = numEl ? numEl.innerText.trim() : '';
                }
                out.push({ chapterId: cid, title: title, done: isDone(el), num: num });
            });
            return out;
        }"""
        for f in page.frames:
            try:
                got = f.evaluate(js)
                if got:
                    return got
            except Exception:
                continue
        return []

    def _course_params(self, page):
        """
        取 courseId/clazzid/cpi。多来源兜底（按可靠性排序）：
          1. 任意章节的 onclick="getTeacherAjax('course','clazz','chapterId')" —— 三个参数齐全，最可靠
          2. 当前 URL 的 query 参数（courseid=/clazzid=/cpi=）
          3. 页面全局 JS 变量（clazzid 全局其实没有，故放最后）
        """
        # 1) 新版页面：隐藏域 #courseId/#clazzId/#cpi
        for f in page.frames:
            try:
                p = f.evaluate(
                    """() => {
                        const g = id => { const el = document.getElementById(id);
                                          return el ? el.value : null; };
                        const c = g('courseId'), cl = g('clazzId'), cp = g('cpi');
                        if (c && cl) return { courseId: c, clazzid: cl, cpi: cp };
                        return null;
                    }""")
                if p:
                    return p
            except Exception:
                continue

        # 2) 从任一章节 onclick 解析（getTeacherAjax 或 toOld）
        for f in page.frames:
            try:
                p = f.evaluate(
                    """() => {
                        const el = document.querySelector(
                            '[onclick*="getTeacherAjax"], [onclick*="toOld"]');
                        if (!el) return null;
                        const oc = el.getAttribute('onclick') || '';
                        let m = oc.match(/getTeacherAjax\\s*\\(\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]/);
                        if (m) return { courseId: m[1], clazzid: m[2], sampleChapter: m[3] };
                        // toOld('courseid', 'knowledgeId', 'clazzid', 0)
                        m = oc.match(/toOld\\s*\\(\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]/);
                        if (m) return { courseId: m[1], clazzid: m[3], sampleChapter: m[2] };
                        return null;
                    }""")
                if p and p.get("courseId") and p.get("clazzid"):
                    return p
            except Exception:
                continue

        # 2) 从 URL query 解析（遍历主框架 + 所有子框架的 URL，
        #    因为 studentstudy 的完整参数常出现在某个 iframe 的 src 里）
        try:
            from urllib.parse import urlparse, parse_qs
            urls = [page.url] + [f.url for f in page.frames if f.url]
            for u in urls:
                q = parse_qs(urlparse(u).query)
                course = (q.get("courseid") or q.get("courseId") or [None])[0]
                clazz = (q.get("clazzid") or q.get("clazzId") or [None])[0]
                cpi = (q.get("cpi") or [None])[0]
                if course and clazz:
                    return {"courseId": course, "clazzid": clazz, "cpi": cpi}
        except Exception:
            pass

        # 2.5) 从 URL 路径/任意位置用正则兜底（有些页面参数不在 query 里）
        try:
            blob = " ".join([page.url] + [f.url for f in page.frames if f.url])
            mc = re.search(r"courseid=(\d+)", blob, re.I)
            mz = re.search(r"clazzid=(\d+)", blob, re.I)
            if mc and mz:
                return {"courseId": mc.group(1), "clazzid": mz.group(1)}
        except Exception:
            pass

        # 3) 页面全局变量（clazzid 全局通常没有）
        for f in page.frames:
            try:
                p = f.evaluate(
                    """() => ({
                        courseId: (typeof courseid!=='undefined'?courseid:null)
                                  || (typeof courseId!=='undefined'?courseId:null),
                        clazzid: (typeof clazzid!=='undefined'?clazzid:null),
                        cpi: (typeof cpi!=='undefined'?cpi:null)
                    })""")
                if p and p.get("courseId"):
                    return p
            except Exception:
                continue
        return {}

    def _switch_chapter(self, page, chapter_id):
        """
        切换章节。按页面版本多策略：
          策略1（新版）：调用页面自带 toOld(courseId, chapterId, clazzid, 0)，
                   它会 top.location.href 跳转到 studentstudy 播放页 —— 整页跳转。
          策略2（旧版）：真实鼠标点击 .posCatalog_name（自带 getTeacherAjax）。
          策略3（兜底）：拼参数手动调用 toOld / getTeacherAjax。
        返回是否成功。注意：新版 toOld 会触发整页导航，调用方需重新等页面加载。
        """
        # ---------- 策略0：已在播放页 → 直接换 chapterId 导航 ----------
        if "studentstudy" in page.url and "chapterId=" in page.url:
            new_url = re.sub(r"chapterId=\d+", f"chapterId={chapter_id}", page.url)
            if new_url != page.url:
                self.log(f"播放页直切章节: chapterId={chapter_id}")
                safe_goto(page, new_url, timeout=60000)
                return True

        # ---------- 策略1：新版页面自带 toOld 函数 ----------
        params = self._course_params(page)
        cid = params.get("courseId")
        clz = params.get("clazzid")
        # 若读不到参数，尝试直接从目标章节元素的 onclick 里抠出完整参数
        if not (cid and clz):
            for f in page.frames:
                try:
                    got = f.evaluate(
                        """(chapId) => {
                            const sels = ['[id="cur'+chapId+'"]',
                                          '[onclick*="'+chapId+'"]'];
                            for (const s of sels) {
                                const el = document.querySelector(s);
                                if (!el) continue;
                                const oc = el.getAttribute('onclick') || '';
                                let m = oc.match(/getTeacherAjax\\s*\\(\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]/);
                                if (m) return { courseId: m[1], clazzid: m[2] };
                                m = oc.match(/toOld\\s*\\(\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]\\s*,\\s*['"](\\d+)['"]/);
                                if (m) return { courseId: m[1], clazzid: m[3] };
                            }
                            return null;
                        }""", chapter_id)
                    if got:
                        cid, clz = got.get("courseId"), got.get("clazzid")
                        if cid and clz:
                            self.log(f"从章节元素补全参数: courseId={cid} clazzid={clz}")
                            break
                except Exception:
                    continue

        for f in page.frames:
            try:
                has = f.evaluate("() => typeof toOld==='function'")
                if has and cid and clz:
                    f.evaluate("() => { window.__isToOldLink = false; }")
                    f.evaluate(f"() => toOld('{cid}','{chapter_id}','{clz}',0)")
                    self.log(f"已调用 toOld 跳转章节（chapterId={chapter_id}）")
                    return True
            except Exception:
                continue

        # ---------- 策略2：旧版真实鼠标点击章节 span ----------
        sel = f'#cur{chapter_id} .posCatalog_name'
        for f in page.frames:
            try:
                el = f.query_selector(sel)
                if not el:
                    continue
                el.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.4)
                box = el.bounding_box()
                if box:
                    cx = box["x"] + min(box["width"] / 2, 60)
                    cy = box["y"] + box["height"] / 2
                    page.mouse.move(cx - 20, cy - 8)
                    time.sleep(0.15)
                    page.mouse.move(cx, cy, steps=6)
                    time.sleep(0.2)
                    page.mouse.down(); time.sleep(0.07); page.mouse.up()
                else:
                    el.click(timeout=3000)
                self.log(f"已点击章节 span（id=cur{chapter_id}）")
                return True
            except Exception:
                continue

        # ---------- 策略3：拼参数手动调用 ----------
        if not (cid and clz):
            self.log("读不到 courseid/clazzid，无法切换", "WARN")
            return False
        for call in (f"toOld('{cid}','{chapter_id}','{clz}',0)",
                     f"getTeacherAjax('{cid}','{clz}','{chapter_id}')"):
            fn = call.split("(")[0]
            for f in page.frames:
                try:
                    ok = f.evaluate(
                        "() => (typeof %s==='function' ? (%s, true) : false)" % (fn, call))
                    if ok:
                        self.log(f"已调用 {call}")
                        return True
                except Exception:
                    continue
        return False


    def _build_study_url(self, current_url, chapter, ref_url):
        """
        用 chapterId 拼 studentstudy 播放页 URL。
        参考当前 studentstudy 链接的参数（courseId/clazzid/cpi/enc 等），只换 chapterId。
        """
        cid = chapter.get("chapterId")
        if not cid:
            return None
        # 从当前诊断里的 studentstudy 快照借参数最稳；否则用 courseid/clazzid 拼基础版
        base = None
        for s in (current_url, ref_url):
            if s and "studentstudy" in s:
                base = s
                break
        if base:
            # 替换 chapterId
            return re.sub(r"chapterId=\d+", f"chapterId={cid}", base)
        # 没有现成 studentstudy 链接 → 用 courseid/clazzid 拼一个（enc 可能缺失，但值得一试）
        m_course = re.search(r"courseid=(\d+)", ref_url or current_url or "")
        m_clazz = re.search(r"clazzid=(\d+)", ref_url or current_url or "")
        m_cpi = re.search(r"cpi=(\d+)", ref_url or current_url or "")
        if m_course and m_clazz:
            q = f"chapterId={cid}&courseId={m_course.group(1)}&clazzid={m_clazz.group(1)}"
            if m_cpi:
                q += f"&cpi={m_cpi.group(1)}"
            return f"https://mooc1.chaoxing.com/mycourse/studentstudy?{q}&mooc2=1"
        return None

    def ai_choose(self, questions):
        """
        调用 AI（Anthropic 兼容接口）答题。
        questions: [{block, type(single|multi), question, options:[{index,text}]}]
        返回 {block: [选项index,...]}；失败返回 None。
        """
        ai = self.ai
        if not ai.get("enabled") or not ai.get("apiKey"):
            return None
        lines = []
        for qi, q in enumerate(questions, 1):
            t = "多选" if q["type"] == "multi" else "单选/判断"
            opts = "；".join(
                f"{chr(65 + o['index'])}.{o['text'][:80]}" for o in q["options"])
            lines.append(f"第{qi}题（{t}）：{q['question']}\n选项：{opts}")
        prompt = (
            "请回答以下题目。只输出答案，每题一行，格式「题号:字母」，"
            "多选写字母组合（如 2:AC），判断题 A=对 B=错。不要任何解释。\n\n"
            + "\n".join(lines)
        )
        try:
            import urllib.request
            req = urllib.request.Request(
                ai["baseUrl"].rstrip("/") + "/v1/messages",
                data=json.dumps({
                    "model": ai.get("model", "deepseek-v4-flash-vision"),
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode("utf-8"),
                headers={
                    "x-api-key": ai["apiKey"],
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=ai.get("timeoutSec", 60)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = "".join(b.get("text", "") for b in body.get("content", []))
            self.log(f"AI 原始回答: {text[:200]}")
            result = {}
            for m in re.finditer(r"(\d+)\s*[:：.、]\s*([A-E]{1,5})", text.upper()):
                qi = int(m.group(1)) - 1
                if 0 <= qi < len(questions):
                    idxs = [ord(c) - 65 for c in m.group(2)]
                    max_i = len(questions[qi]["options"]) - 1
                    result[questions[qi]["block"]] = [i for i in idxs if 0 <= i <= max_i]
                    self.log(f"  第{qi+1}题 → AI 选择 {m.group(2)}")
            return result or None
        except Exception as e:
            self.log(f"AI 答题失败（退回启发式）: {e}", "WARN")
            return None

    def ai_answer_text(self, questions):
        """
        简答题：把题干发给 AI，返回 {题号index: 答案文本}。失败返回 None。
        """
        ai = self.ai
        if not ai.get("enabled") or not ai.get("apiKey"):
            return None
        lines = [f"第{i+1}题：{q}" for i, q in enumerate(questions)]
        prompt = (
            "请简要回答以下简答题（每题 50-150 字，直接给答案内容，不要解释）。"
            "每题一段，以「题号:」开头，如「1:xxxxx」。\n\n" + "\n".join(lines)
        )
        try:
            import urllib.request
            req = urllib.request.Request(
                ai["baseUrl"].rstrip("/") + "/v1/messages",
                data=json.dumps({
                    "model": ai.get("model", "deepseek-v4-flash-vision"),
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode("utf-8"),
                headers={
                    "x-api-key": ai["apiKey"],
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=ai.get("timeoutSec", 60)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = "".join(b.get("text", "") for b in body.get("content", []))
            result = {}
            # 按「数字:」切分，取每段内容
            parts = re.split(r"(?m)^\s*(\d+)\s*[:：.、]\s*", text)
            # parts = ['', '1', '答案1', '2', '答案2', ...]
            for i in range(1, len(parts) - 1, 2):
                qi = int(parts[i]) - 1
                ans = parts[i + 1].strip()
                if 0 <= qi < len(questions) and ans:
                    result[qi] = ans[:500]
                    self.log(f"  第{qi+1}题 → AI 作答 {len(ans)} 字")
            return result or None
        except Exception as e:
            self.log(f"AI 简答失败: {e}", "WARN")
            return None

    def try_quiz(self, page, label=""):
        """
        章节测验自动作答（启发式，无题库，目的是提交完成任务点）：
          - 只在疑似测验页/框架中动手（URL 含 work/quiz/exam 或存在 .TiMu/.questionLi），
            避免误点目录页的 checkbox。
          - 单选：选第一项；判断：优先「对/true」；多选：全选；填空：跳过。
          - 作答后自动点「提交/交卷」，并屏蔽 confirm 弹窗。
        返回 "done"（作答并提交）/ "none"（没发现题）/ "failed"（结构不识别，已存现场）。
        """
        # 测验 iframe 常是懒加载：滚动页面触发加载后再扫描
        try:
            page.mouse.wheel(0, 3000)
            time.sleep(2)
        except Exception:
            pass
        detect_js = """() => ({
            url: location.href,
            looksQuiz: /work|quiz|exam|test/i.test(location.href)
                       || !!(document.querySelector('.TiMu, .questionLi, .question-item, .questionItem')),
            radios: document.querySelectorAll('input[type=radio]').length,
            checks: document.querySelectorAll('input[type=checkbox]').length,
            subj: document.querySelectorAll(
                '.TiMu iframe, .questionLi iframe, .TiMu textarea, .questionLi textarea').length
        })"""
        answer_js = """() => {
            let answered = 0;
            const groups = {};
            document.querySelectorAll('input[type=radio]').forEach(r => {
                if (r.name) (groups[r.name] = groups[r.name] || []).push(r);
            });
            for (const name in groups) {
                const g = groups[name];
                if (g.some(r => r.checked)) continue;
                let pick = g[0];
                for (const r of g) {  // 判断题优先「对/true/1」
                    const v = (r.value || '').toLowerCase();
                    const lbl = r.parentElement ? r.parentElement.innerText.trim() : '';
                    if (['true','1','对','正确'].includes(v) || lbl.startsWith('对')) { pick = r; break; }
                }
                pick.click(); answered++;
            }
            const cg = {};
            document.querySelectorAll('input[type=checkbox]').forEach(c => {
                if (c.name && c.name !== 'checkbox')
                    (cg[c.name] = cg[c.name] || []).push(c);
            });
            for (const name in cg) {  // 多选：全选
                const g = cg[name];
                if (g.some(c => c.checked)) continue;
                g.forEach(c => c.click()); answered++;
            }
            return answered;
        }"""
        extract_js = """() => {
            const qs = [];
            const blocks = document.querySelectorAll(
                '.TiMu, .questionLi, .question-item, .questionItem');
            blocks.forEach((b, bi) => {
                const inputs = [...b.querySelectorAll(
                    'input[type=radio], input[type=checkbox]')];
                if (!inputs.length) return;
                const tEl = b.querySelector(
                    '.mark_name, .fontLabel, h3, .question-title, .title, .Zy_TItle');
                const qtext = (tEl ? tEl.innerText : b.innerText.slice(0, 200))
                    .trim().replace(/\\s+/g, ' ');
                const opts = inputs.map((inp, ii) => {
                    const li = inp.closest('li') || inp.parentElement;
                    return { index: ii,
                             text: (li ? li.innerText : '').trim().slice(0, 120) };
                });
                qs.push({ block: bi,
                          type: inputs[0].type === 'radio' ? 'single' : 'multi',
                          question: qtext.slice(0, 300), options: opts });
            });
            return qs;
        }"""
        apply_js = """(answers) => {
            const blocks = document.querySelectorAll(
                '.TiMu, .questionLi, .question-item, .questionItem');
            let clicked = 0;
            for (const [bi, idxs] of Object.entries(answers)) {
                const b = blocks[Number(bi)];
                if (!b) continue;
                const inputs = [...b.querySelectorAll(
                    'input[type=radio], input[type=checkbox]')];
                for (const i of idxs) {
                    if (inputs[i] && !inputs[i].checked) { inputs[i].click(); clicked++; }
                }
            }
            return clicked;
        }"""
        # ---------- 简答题：提取题干 / AI 生成 / 填入富文本编辑器 ----------
        subj_extract_js = """() => {
            const qs = [];
            const blocks = document.querySelectorAll(
                '.TiMu, .questionLi, .question-item, .questionItem');
            blocks.forEach((b, bi) => {
                if (b.querySelector('input[type=radio], input[type=checkbox]')) return;
                const ed = b.querySelector('iframe') || b.querySelector('textarea');
                if (!ed) return;
                const tEl = b.querySelector(
                    '.mark_name, .fontLabel, h3, .question-title, .title, .Zy_TItle');
                const qtext = (tEl ? tEl.innerText : b.innerText.slice(0, 200))
                    .trim().replace(/\\s+/g, ' ');
                if (qtext) qs.push({ block: bi, question: qtext.slice(0, 300) });
            });
            return qs;
        }"""
        subj_fill_js = """(answers) => {
            const blocks = document.querySelectorAll(
                '.TiMu, .questionLi, .question-item, .questionItem');
            let filled = 0;
            for (const [bi, text] of Object.entries(answers)) {
                const b = blocks[Number(bi)];
                if (!b) continue;
                const ifr = b.querySelector('iframe');
                if (ifr && ifr.contentDocument && ifr.contentDocument.body) {
                    ifr.contentDocument.body.innerHTML = '<p>' + text + '</p>';
                    filled++;
                }
                const ta = b.querySelector('textarea');
                if (ta) {
                    ta.value = text;
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                    filled++;
                }
            }
            return filled;
        }"""
        found_any = False
        for f in page.frames:
            try:
                d = f.evaluate(detect_js)
                if not d or not d.get("looksQuiz"):
                    continue
                if d.get("radios", 0) + d.get("checks", 0) + d.get("subj", 0) == 0:
                    continue
                found_any = True
                # ---------- 简答题优先处理 ----------
                subj_qs = f.evaluate(subj_extract_js) or []
                if subj_qs:
                    self.log(f"{label} 测验：{len(subj_qs)} 道简答题，请求 AI 作答…")
                    answers = self.ai_answer_text([q["question"] for q in subj_qs])
                    if answers:
                        payload = {str(subj_qs[i]["block"]): t
                                   for i, t in answers.items()}
                        nf = f.evaluate(subj_fill_js, payload) or 0
                        self.log(f"{label} 简答题：已填入 {nf} 处编辑器")
                        time.sleep(1)
                        sres = self._submit_quiz(page, label)
                        if sres == "notfound":
                            # 找不到提交按钮 → 视为未提交成功，不记 done，等待重试
                            self.log(f"{label} 简答题已填但找不到提交按钮，未标记完成", "WARN")
                            return "failed"
                        self.log(f"{label} 测验：已提交 ✓（{sres}）")
                        return "done"
                # ---------- 客观题：结构化提取 → AI 作答 ----------
                questions = f.evaluate(extract_js) or []
                n = 0
                if questions:
                    self.log(f"{label} 测验：提取到 {len(questions)} 题，请求 AI…")
                    answers = self.ai_choose(questions)
                    if answers:
                        n = f.evaluate(apply_js, answers) or 0
                        self.log(f"{label} 测验：AI 作答 {n} 处")
                # ---------- AI 不可用/失败 → 启发式 ----------
                if n == 0:
                    n = f.evaluate(answer_js) or 0
                    if n:
                        self.log(f"{label} 测验：启发式作答 {n} 处")
                if n > 0:
                    time.sleep(1)
                    sres = self._submit_quiz(page, label)
                    if sres == "notfound":
                        # 找不到提交按钮 → 视为未提交成功，不记 done，等待重试
                        self.log(f"{label} 测验：已作答但找不到提交按钮，未标记完成", "WARN")
                        return "failed"
                    self.log(f"{label} 测验：已提交 ✓（{sres}）")
                    return "done"
                # n == 0：可能是重入（答案已在，AI/启发式无可新增）。
                # 只要页面没显示已提交，就仍尝试提交 —— 这是"能输入但不提交"的关键补丁
                bt = ""
                try:
                    bt = f.evaluate("() => (document.body&&document.body.innerText)||''")
                except Exception:
                    pass
                if bt and ("已提交" in bt or "提交成功" in bt or "成绩" in bt or "得分" in bt):
                    self.log(f"{label} 测验：页面显示已提交过，无需再交")
                    return "done"
                sres = self._submit_quiz(page, label)
                if sres == "notfound":
                    # 真结构不识别 → 存现场便于迭代
                    dump = user_dir() / "quiz_dump.html"
                    dump.write_text(f.content(), encoding="utf-8")
                    self.log(f"{label} 测验结构不识别（无答案可填、无按钮可点），已存现场: {dump}", "WARN")
                    return "failed"
                self.log(f"{label} 测验：已提交 ✓（{sres}，重入补交）")
                return "done"
            except Exception:
                continue
        if not found_any:
            self.log(f"{label} 未检测到题目，跳过做题步骤")
        return "none" if not found_any else "failed"

    # ---- 提交测验（多 frame 搜索 + 真实点击 + 二次确认 + 成功校验） ----------
    def _submit_quiz(self, page, label=""):
        """
        在所有 frame 里找「提交/交卷」按钮，用 Playwright 真实鼠标点击
        （新 UI 框架常不响应 JS 合成 click），然后处理二次确认模态框，
        并校验是否真正提交成功。整体最多重试 3 轮。

        返回：
          "submitted"   点到了提交，且检测到「提交成功/交卷成功」等成功标识
                        或提交按钮随后消失（表单已被收走）
          "clicked"     点到了提交按钮，但未确认到成功标识（保守视为已点）
          "notfound"    所有 frame 都找不到可见的提交按钮
        """
        # 0) 所有 frame 覆盖 confirm/alert + 解除 beforeunload 拦截
        for f in page.frames:
            try:
                f.evaluate("() => { try { window.confirm = () => true;"
                           " window.alert = () => {}; window.onbeforeunload = null;"
                           " } catch(e){} }")
            except Exception:
                continue

        # 快速扫描：哪个 frame 里有"可见且文字精确匹配"的提交按钮
        scan_js = """() => {
            function visible(e) {
                const s = getComputedStyle(e);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                return (e.offsetParent !== null || e.getClientRects().length > 0);
            }
            const re = /^(提交|交卷|上交|提交答案|提交作业|确认提交|保存并提交|完成并提交)$/;
            const cands = [...document.querySelectorAll(
                'button, a, input[type=button], input[type=submit], span, div, li, p, [role=button]')];
            const hits = cands.filter(e => {
                if (!visible(e)) return false;
                const t = ((e.innerText || '') + (e.value || '')).trim();
                return t && t.length <= 12 && re.test(t);
            });
            if (!hits.length) return null;
            // 提交按钮一般在页面底部 → 取最靠下的
            hits.sort((x, y) =>
                y.getBoundingClientRect().top - x.getBoundingClientRect().top);
            const el = hits[0];
            el.scrollIntoView({ block: 'center' });
            const rc = el.getBoundingClientRect();
            return { text: ((el.innerText || '') || (el.value || '')).trim(),
                     tag: el.tagName, cls: String(el.className || '').slice(0, 50) };
        }"""
        # JS 兜底点击：完整鼠标事件序列（mousedown/mouseup/click + 原生 click）
        js_click_js = """(txt) => {
            function visible(e) {
                const s = getComputedStyle(e);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                return (e.offsetParent !== null || e.getClientRects().length > 0);
            }
            const re = new RegExp('^(提交|交卷|上交|提交答案|提交作业|确认提交|保存并提交|完成并提交)$');
            const cands = [...document.querySelectorAll(
                'button, a, input[type=button], input[type=submit], span, div, li, p, [role=button]')];
            const hits = cands.filter(e => {
                if (!visible(e)) return false;
                const t = ((e.innerText || '') + (e.value || '')).trim();
                return t && t.length <= 12 && re.test(t);
            });
            if (!hits.length) return false;
            hits.sort((x, y) =>
                y.getBoundingClientRect().top - x.getBoundingClientRect().top);
            const el = hits[0];
            el.scrollIntoView({ block: 'center' });
            const opts = { bubbles: true, cancelable: true, view: window };
            ['mousedown', 'mouseup', 'click'].forEach(t => {
                try { el.dispatchEvent(new MouseEvent(t, opts)); } catch (e) {}
            });
            try { el.click(); } catch (e) {}
            return true;
        }"""
        # 成功/确认检测 + 确认按钮真实点击（模态内定位，避免误点主表单按钮）
        confirm_scan_js = """() => {
            const bt = (document.body && document.body.innerText) || '';
            const SUCC = ['提交成功','交卷成功','答题完成','提交完成','保存成功','已提交'];
            if (SUCC.some(t => bt.indexOf(t) >= 0)) return { ok: 'success' };
            // 清掉上次可能残留的标记
            document.querySelectorAll('[data-cx-confirm]')
                .forEach(e => e.removeAttribute('data-cx-confirm'));
            function visible(e) {
                const s = getComputedStyle(e);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                return (e.offsetParent !== null || e.getClientRects().length > 0);
            }
            // 1) 找可见的模态容器（提示/确认提交弹窗，如「提示: 确认提交? 取消/提交」）
            const sel = '[class*=modal],[class*=Modal],[class*=dialog],[class*=Dialog],'
                      + '[class*=popup],[class*=Popup],[class*=confirm],[class*=Confirm],'
                      + '[class*=window],[class*=Window],.el-dialog,.ant-modal,.layui-layer';
            let modals = [...document.querySelectorAll(sel)].filter(visible);
            // 容器必须含「确认/提示/是否」字样（模态标题或正文），排除主表单误命中
            modals = modals.filter(e =>
                /确认|提示|是否/.test((e.innerText || '')));
            // 面积升序 = 最内层容器优先（避免外层 wrapper 包住整页）
            modals.sort((a, b) => {
                const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                return (ra.width * ra.height) - (rb.width * rb.height);
            });
            const re = /^(提交|确定|确认|是|仍要提交|继续提交|确定提交|确认提交)$/;
            const reNo = /^(取消|关闭|否|返回|暂不|以后再说)$/;
            for (const m of modals) {
                const cands = [...m.querySelectorAll(
                    'button, a, [role=button], input[type=button], span, div')];
                const hits = cands.filter(e => {
                    if (!visible(e)) return false;
                    const t = ((e.innerText || '') + (e.value || '')).trim();
                    return t && t.length <= 8 && re.test(t) && !reNo.test(t);
                });
                if (!hits.length) continue;
                // 确认按钮在模态底部按钮区 → 取最靠下的
                hits.sort((x, y) =>
                    y.getBoundingClientRect().top - x.getBoundingClientRect().top);
                const el = hits[0];
                el.setAttribute('data-cx-confirm', '1');
                const t = ((el.innerText || '') || (el.value || '')).trim();
                return { ok: 'confirm', text: t,
                         cls: String(el.className || '').slice(0, 50) };
            }
            return { ok: null };
        }"""

        for attempt in range(3):
            # 1) 找按钮所在 frame
            target = None
            for f in page.frames:
                try:
                    r = f.evaluate(scan_js)
                except Exception:
                    continue
                if r:
                    target = (f, r)
                    break
            if not target:
                if attempt == 0:
                    return "notfound"
                break  # 首轮点到了但按钮已消失 → 大概率已提交
            f, info = target
            txt = info.get("text") or "提交"
            clicked = False
            # 2a) Playwright 真实点击（可信鼠标事件，新 UI 认这个）
            try:
                loc = f.get_by_text(txt, exact=True)
                loc.last.click(timeout=6000)
                clicked = True
                how = "真实点击"
            except Exception:
                # 2b) JS 事件序列兜底
                try:
                    clicked = bool(f.evaluate(js_click_js, txt))
                    how = "JS兜底"
                except Exception:
                    clicked = False
            if not clicked:
                continue
            self.log(f"{label} 测验：已点击提交按钮 [{info.get('tag')}:{txt}] "
                     f"class={info.get('cls')} ({how})")

            # 3) 二次确认 + 成功校验（最多 5 轮 × 1.5s）
            btn_gone = False
            for _ in range(5):
                time.sleep(1.5)
                # 简答题提交后超星常弹"多终端登录/异常学习"风控页（整页或遮罩），
                # 它会盖住成功标识并阻断后续点击 → 先关掉它
                if self._dismiss_security_notice(page, label):
                    self.log(f"{label} 测验：已关闭提交后的安全提示页")
                    # 关掉后看后台是否已是成功页
                    succ = False
                    for fr in page.frames:
                        try:
                            succ = fr.evaluate("""() => {
                                const bt = (document.body && document.body.innerText) || '';
                                return ['提交成功','交卷成功','答题完成','提交完成',
                                        '保存成功','已提交'].some(t => bt.indexOf(t) >= 0);
                            }""") or False
                        except Exception:
                            succ = False
                        if succ:
                            break
                    if succ:
                        return "submitted"
                    btn_gone = True
                    break
                confirmed = False
                for fr in page.frames:
                    try:
                        r = fr.evaluate(confirm_scan_js)
                    except Exception:
                        continue
                    if not r:
                        continue
                    if r.get("ok") == "success":
                        return "submitted"
                    if r.get("ok") == "confirm" and not confirmed:
                        confirmed = True
                        ctxt = r.get("text") or "确定"
                        try:
                            # 用打标记的元素精确定位真实点击
                            fr.locator('[data-cx-confirm="1"]').first.click(
                                timeout=4000)
                            self.log(f"{label} 测验：真实点击二次确认[{ctxt}] "
                                     f"class={r.get('cls')}")
                        except Exception:
                            for fr2 in page.frames:
                                try:
                                    fr2.evaluate("""() => {
                                        const b = document.querySelector(
                                            '[data-cx-confirm="1"]');
                                        if (!b) return;
                                        b.scrollIntoView({block:'center'});
                                        const o={bubbles:true,cancelable:true,view:window};
                                        ['mousedown','mouseup','click'].forEach(t2=>{
                                            try{b.dispatchEvent(new MouseEvent(t2,o));}catch(e){}
                                        });
                                        try{b.click();}catch(e){}
                                    }""")
                                except Exception:
                                    continue
                            self.log(f"{label} 测验：JS兜底点击二次确认[{ctxt}]", "WARN")
                # 提交按钮是否还在？
                still = None
                for fr in page.frames:
                    try:
                        still = fr.evaluate(scan_js)
                    except Exception:
                        continue
                    if still:
                        break
                if not still:
                    btn_gone = True
                    break
            if btn_gone:
                # 按钮消失 = 表单已被收走（已提交或已跳转），视为成功
                return "submitted"
            # 按钮还在 → 本轮可能没点中，重试
            self.log(f"{label} 测验：提交按钮仍在，重试第 {attempt + 2} 次", "WARN")
        # 收尾再尝试关闭可能延迟弹出的安全提示（提交后才出现的情况）
        for _ in range(3):
            time.sleep(1.5)
            if self._dismiss_security_notice(page, label):
                self.log(f"{label} 测验：收尾关闭延迟弹出的安全提示")
                return "submitted"
        return "clicked"

    def try_document(self, page, label=""):
        """
        文档/音频类任务点处理：
          - 文档（PDF/PPT/图片阅读）：模拟翻阅 —— 滚动 + 点「下一页」+ 停留，
            阅读类任务点一般翻完/停留够时长即记完成。
          - 音频：点击播放并等待播完（带上限）。
        返回 "done"（已处理）/ "none"（不是文档/音频节点）。
        """
        detect_js = """() => ({
            url: location.href,
            isDoc: /pdf|doc|ppt|office|read|ananas/i.test(location.href)
                   || !!document.querySelector(
                       '#viewer, .pdf-viewer, .pdfViewer, [class*="doc"], [class*="ppt"]'),
            hasAudio: !!document.querySelector('audio'),
            hasNextBtn: ![...document.querySelectorAll('button, a, span')].filter(
                e => /下一页|next/i.test(e.innerText || '')).length == 0
        })"""
        is_doc_node = False
        # ---------- 音频：播放并等结束 ----------
        for f in page.frames:
            try:
                d = f.evaluate(detect_js)
            except Exception:
                continue
            if d.get("hasAudio"):
                # 先甄别「真实音频任务点」还是无关的 <audio> 元素：
                # 超星页面常带隐藏占位/提示音 <audio>（即便有 src、readyState 也 >0），
                # 对着它死等会卡住整轮。判定依据：
                #   A. 有源（src 非空 或 readyState>0）
                #   B. 且满足以下任一：
                #      - 时长 > 10s（真实课程音频以分钟计，提示音只有 1~2 秒）
                #      - 元素可见（提示音/占位元素通常 display:none 或 0 尺寸）
                #      - 祖先链上带任务点容器特征（ans- / job / taskpoint / attach）
                try:
                    info = f.evaluate("""() => {
                        const a = document.querySelector('audio');
                        if (!a) return null;
                        const r = a.getBoundingClientRect();
                        let inTask = false, chain = [];
                        let el = a;
                        for (let i = 0; i < 6 && el; i++) {
                            const idc = String(el.className || '') + ' ' + String(el.id || '');
                            chain.push(el.tagName + '.' + idc.trim().slice(0, 50));
                            if (/ans-|job|taskpoint|task-point|attach|knowledge/i.test(idc)) inTask = true;
                            el = el.parentElement;
                        }
                        return {
                            src: (a.currentSrc || a.src || '').slice(0, 120),
                            readyState: a.readyState,
                            dur: (isFinite(a.duration) ? a.duration : 0),
                            visible: !!(r.width || r.height),
                            inTask: inTask,
                            chain: chain,
                        };
                    }""")
                except Exception:
                    continue
                has_src = bool(info and (info.get("src") or info.get("readyState", 0) > 0))
                looks_task = bool(info and (info.get("dur", 0) > 10
                                            or info.get("visible")
                                            or info.get("inTask")))
                if not has_src or not looks_task:
                    self.log(f"{label} 忽略非任务点音频元素: "
                             f"src={bool(info and info.get('src'))} "
                             f"readyState={info and info.get('readyState')} "
                             f"dur={info and info.get('dur')} "
                             f"visible={info and info.get('visible')} "
                             f"chain={info and ' > '.join((info.get('chain') or [])[:3])}")
                    continue
                is_doc_node = True
                try:
                    f.evaluate("""() => {
                        const a = document.querySelector('audio');
                        if (a) { a.muted = true;   // 必须静音，否则浏览器自动播放策略会拦截
                                a.playbackRate = window.__cx_rate || 1.5;
                                const p = a.play(); if (p && p.catch) p.catch(()=>{}); }
                    }""")
                    est = info.get("dur", 0)
                    self.log(f"{label} 检测到音频，播放中…（时长约 {est:.0f}s）")
                    deadline = time.time() + self.behavior.get("maxMinutesPerLesson", 120) * 60
                    last_ct = -1.0
                    last_adv = time.time()
                    last_log = time.time()
                    bad_dur_since = None   # duration 元数据无效的最早时刻
                    while time.time() < deadline:
                        st = f.evaluate("""() => { const a = document.querySelector('audio');
                            return a ? { ended: a.ended, ct: a.currentTime,
                                         dur: (isFinite(a.duration) ? a.duration : 0),
                                         fin: a.duration>0 && a.duration-a.currentTime<2 }
                                     : { ended: true, fin: true, ct: 0, dur: 0 }; }""")
                        if st.get("ended") or st.get("fin"):
                            self.log(f"{label} 音频完成 ✓")
                            return "done"
                        # 进度若长期(90s)不前进：多半是自动播放被拦截或该音频非任务点，
                        # 不再死等，直接判定完成并跳下一章，避免卡死整轮
                        ct = st.get("ct", 0) or 0
                        if ct > last_ct + 0.01:
                            last_ct = ct
                            last_adv = time.time()
                        elif time.time() - last_adv > 90:
                            self.log(f"{label} 音频进度长期未前进，判定已完成并跳下一章", "WARN")
                            return "done"
                        # duration 无效（NaN/0/∞，流式或元数据未加载）时 fin 永不成立，
                        # 若只靠 120min 上限会死等整节课。给 3 分钟兜底。
                        dur = st.get("dur", 0) or 0
                        if dur <= 0:
                            if bad_dur_since is None:
                                bad_dur_since = time.time()
                            elif time.time() - bad_dur_since > 180:
                                self.log(f"{label} 音频时长元数据无效，已等 3 分钟，跳下一章", "WARN")
                                return "done"
                        else:
                            bad_dur_since = None
                        # 每 15s 打一次进度，避免长时间无日志看起来像卡死
                        if time.time() - last_log > 15:
                            if dur > 0:
                                self.log(f"{label} 音频播放中 {ct:.0f}/{dur:.0f}s…")
                            else:
                                self.log(f"{label} 音频播放中 {ct:.0f}s（时长未知）…")
                            last_log = time.time()
                        time.sleep(5)
                    # 走到这里说明触达 maxMinutesPerLesson 上限
                    self.log(f"{label} 音频等待超过单节上限，强制跳下一章", "WARN")
                    return "done"
                except Exception:
                    continue
            if d.get("isDoc"):
                is_doc_node = True
        if not is_doc_node:
            return "none"

        # ---------- 文档：模拟翻阅 ----------
        self.log(f"{label} 文档类任务点，模拟翻阅…")
        next_js = """() => {
            const cands = [...document.querySelectorAll('button, a, span, div')];
            const b = cands.find(e => /^\\s*(下一页|next)\\s*$/i.test(e.innerText || ''));
            if (b) { b.click(); return true; }
            return false;
        }"""
        for round_i in range(10):
            for f in page.frames:
                try:
                    f.evaluate("() => window.scrollBy(0, 800)")
                    f.evaluate(next_js)
                except Exception:
                    continue
            try:
                page.mouse.wheel(0, 800)
            except Exception:
                pass
            time.sleep(3)
        self.log(f"{label} 文档翻阅完成（建议稍后在 App 核对是否记上）✓")
        return "done"

    # ---- 讨论（课后讨论/话题）任务点 -------------------------------
    def try_discussion(self, page, label=""):
        """
        讨论/课后讨论任务点：在讨论 iframe 里自动发一条回复。

        识别：iframe URL 含 bbs / discuss / grouptopic / topic，
              且页面里有回复编辑器（textarea 或 contenteditable）
              或「发布/回复」按钮。
        内容：优先 AI 生成（以讨论主题为题干）；AI 不可用时用主题模板兜底。
        提交：填入编辑器 → 点「发布/回复」按钮 → 等待成功提示。

        返回 "done"（确认发布成功）/ "none"（不是讨论节点或发布未确认）。
        """
        # 1) 定位讨论 frame 并抓主题
        frame = None
        topic = ""
        for f in page.frames:
            furl = (f.url or "").lower()
            if not any(k in furl for k in ("bbs", "discuss", "grouptopic", "/topic")):
                continue
            try:
                info = f.evaluate("""() => {
                    const t = document.querySelector(
                        '.topic-title, .title, h1, h2, .tit, [class*="title"]');
                    const hasEditor = !!document.querySelector(
                        'textarea, [contenteditable="true"], .edui-body-container');
                    const hasBtn = [...document.querySelectorAll('a,button,div,span')]
                        .some(e => /^(发布|回复|发表回帖|提交)$/.test((e.innerText||'').trim()));
                    return {
                        title: t ? (t.innerText || '').trim().slice(0, 120) : '',
                        hasEditor: hasEditor,
                        hasBtn: hasBtn,
                    };
                }""")
            except Exception:
                continue
            if info and (info.get("hasEditor") or info.get("hasBtn")):
                frame = f
                topic = info.get("title") or ""
                break
        if frame is None:
            return "none"

        self.log(f"{label} 检测到讨论任务点，主题: {topic[:50] or '（未知）'}")

        # 2) 生成回复内容：AI 优先，模板兜底
        dcfg = self.cfg.get("discussion", {}) or {}
        reply = None
        if topic:
            try:
                hint = dcfg.get("replyHint") or (
                    "请写一段 80-150 字的讨论式回帖：观点明确、有具体思路或步骤，"
                    "像学生自己在论坛回复，不要客套开场白。"
                )
                r = self.ai_answer_text([
                    f"这是课程讨论区的讨论主题：「{topic}」。{hint}"
                ])
                if r:
                    reply = next(iter(r.values()), None)
            except Exception:
                reply = None
        if not reply and dcfg.get("fallbackReply"):
            reply = dcfg["fallbackReply"]
        if not reply:
            reply = (
                f"关于「{topic[:60] or '本话题'}」：我认为要先把问题的功能要求梳理清楚，"
                "再按「输入条件 → 控制逻辑 → 输出动作」的思路逐步分析，"
                "结合课堂所学内容编写并调试程序加以验证。具体细节我在实操中记录了些心得，"
                "欢迎大家补充不同看法，一起讨论。"
            )
            self.log(f"{label} AI 不可用，使用模板回帖", "WARN")

        # 3) 填入编辑器并点发布
        clicked = False
        try:
            clicked = frame.evaluate("""(txt) => {
                const ta = document.querySelector('textarea');
                const ce = document.querySelector('[contenteditable="true"]');
                if (ta) {
                    ta.value = txt;
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                } else if (ce) {
                    ce.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, txt);
                } else {
                    return false;
                }
                const btn = [...document.querySelectorAll('a,button,div,span')].find(e => {
                    const t = (e.innerText || '').trim();
                    if (!/^(发布|回复|发表回帖|提交)$/.test(t)) return false;
                    return e.offsetParent !== null;   // 可见才点
                });
                if (!btn) return false;
                btn.click();
                return true;
            }""", reply)
        except Exception:
            clicked = False
        if not clicked:
            self.log(f"{label} 讨论编辑器/发布按钮未识别，跳过（请手动处理）", "WARN")
            return "none"

        # 4) 等待成功提示（3s × 3 轮）
        confirmed = False
        for _ in range(3):
            time.sleep(3)
            try:
                confirmed = frame.evaluate(
                    """() => /发布成功|回复成功|发表成功|成功/.test(document.body.innerText || '')"""
                )
            except Exception:
                confirmed = False
            if confirmed:
                break
        if confirmed:
            self.log(f"{label} 讨论回复发布成功 ✓")
            return "done"
        self.log(f"{label} 已尝试发布讨论回复，但未检测到成功提示，请稍后手动确认", "WARN")
        return "none"

    # ---- 任务点处理器链（扩展点）---------------------------------
    def handle_task_points(self, page, label=""):
        """
        按 TASK_HANDLERS 顺序依次尝试处理视频之外的任务点。
        返回 "done"（某处理器成功处理）/ "failed"（失败，待重试）/ "none"（都不匹配）。
        config.features 里对应键设为 false 可跳过该处理器。
        """
        feats = self.cfg.get("features", {}) or {}
        for key, meth in self.TASK_HANDLERS:
            if feats.get(key) is False:
                continue
            fn = getattr(self, meth, None)
            if fn is None:
                continue
            try:
                res = fn(page, label)
            except Exception as e:
                self.log(f"{label} {meth} 处理异常: {e}", "WARN")
                continue
            if res == "done":
                return "done"
            if res == "failed":
                return "failed"
        return "none"


    def _dismiss_security_notice(self, page, label=""):
        """
        关闭超星的安全风控提示页/弹窗（多终端登录提醒、"异常学习"警告、
        修改密码提示等）。这类提示常**覆盖在内容之上或单独成页**，会挡住
        自动化点击（也可能正是之前"播放一会被跳走"的元凶之一），必须关掉。
        仅识别含风控关键词的页面，不会误关正常业务弹窗。
        """
        js = """() => {
            const bt = (document.body && document.body.innerText) || '';
            if (!/(多终端|异常学习|修改密码|安全提醒|风险|账号.*登录|登录.*设备)/.test(bt))
                return null;
            const re = /^(我知道了|知道了|确定|确认|继续|关闭|好的|暂不提醒|忽略|返回学习|返回|稍后|不再提醒)$/;
            function visible(e) {
                const s = getComputedStyle(e);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                return (e.offsetParent !== null || e.getClientRects().length > 0);
            }
            const cands = [...document.querySelectorAll(
                'button, a, [role=button], input[type=button], span, div')];
            const hits = cands.filter(e => {
                if (!visible(e)) return false;
                const t = ((e.innerText || '') + (e.value || '')).trim();
                return t && t.length <= 12 && re.test(t);
            });
            let el = null;
            if (hits.length) {
                hits.sort((x, y) =>
                    y.getBoundingClientRect().top - x.getBoundingClientRect().top);
                el = hits[0];
            } else {
                // 兜底：风控页有但无标准关闭按钮 → 点最底部可见按钮
                const all = cands.filter(e => {
                    if (!visible(e)) return false;
                    const t = ((e.innerText || '') + (e.value || '')).trim();
                    return t && t.length <= 12;
                });
                if (all.length) {
                    all.sort((x, y) =>
                        y.getBoundingClientRect().top - x.getBoundingClientRect().top);
                    el = all[0];
                }
            }
            if (!el) return null;
            el.scrollIntoView({ block: 'center' });
            return { text: ((el.innerText || '') || (el.value || '')).trim(),
                     cls: String(el.className || '').slice(0, 50) };
        }"""
        for f in page.frames:
            try:
                hit = f.evaluate(js)
            except Exception:
                continue
            if not hit:
                continue
            self.log(f"{label} 检测到超星安全提示，尝试关闭: "
                     f"按钮「{hit.get('text')}」class={hit.get('cls')}")
            txt = hit.get("text") or "确定"
            try:
                f.get_by_text(txt, exact=True).last.click(timeout=4000)
            except Exception:
                try:
                    f.evaluate("""(t) => {
                        const re = new RegExp('^(' + t + ')$');
                        const cands = [...document.querySelectorAll(
                            'button, a, [role=button], input[type=button], span, div')];
                        const b = cands.find(e => {
                            const s = getComputedStyle(e);
                            if (s.display === 'none' || s.visibility === 'hidden') return false;
                            const tx = ((e.innerText || '') + (e.value || '')).trim();
                            return tx && tx.length <= 10 && re.test(tx);
                        });
                        if (b) {
                            b.scrollIntoView({ block: 'center' });
                            const o = { bubbles: true, cancelable: true, view: window };
                            ['mousedown', 'mouseup', 'click'].forEach(x => {
                                try { b.dispatchEvent(new MouseEvent(x, o)); } catch (e) {}
                            });
                            try { b.click(); } catch (e) {}
                        }
                    }""", txt)
                except Exception:
                    self.log(f"{label} 关闭安全提示失败（按钮 {txt}）", "WARN")
            time.sleep(1.5)
            return True
        return False

    def auto_click_gateway(self, page, label=""):
        """
        自动点过**中间转场/引导页**的按钮（如「进入课程」「继续学习」）。
        超星 stucoursemiddle 等页面需要点一下才继续，不点会卡住。

        安全约束（防止误点导致页面跳走）：
        - 只在**没有 video 且不在 studentstudy 播放页**的转场页上操作；
        - 匹配词收紧为明确的转场文案，去掉「^进入$」这类过于宽泛的模式；
        - 只点可见的按钮/链接。
        返回是否点了。
        """
        # 已经在播放页 → 不需要（也不应该）点转场按钮
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "studentstudy" in u:
            return False
        js = """() => {
            const pats = [/进入课程/, /继续学习/, /开始学习/, /进入学习/,
                          /继续观看/, /进入学习页/];
            const cands = [...document.querySelectorAll(
                'button, a, input[type=button], .btn, span')];
            for (const e of cands) {
                const t = ((e.innerText || '') + (e.value || '')).trim();
                if (!t || t.length > 12) continue;
                // 排除明显会跳走的导航词
                if (/下一|上一个|返回|首页|退出/.test(t)) continue;
                if (pats.some(p => p.test(t))) {
                    e.click();
                    return t;
                }
            }
            return null;
        }"""
        for f in page.frames:
            try:
                hit = f.evaluate(js)
                if hit:
                    self.log(f"{label} 自动点击转场按钮「{hit}」")
                    time.sleep(3)
                    return True
            except Exception:
                continue
        return False

    def _get_iframe_course_url(self, page):
        """取 iframe(frame_content-zj / 任何指向 studentcourse|study 的 iframe) 的绝对 src。"""
        try:
            src = page.evaluate(
                """() => {
                    const frs = Array.from(document.querySelectorAll('iframe'));
                    for (const f of frs) {
                        const s = f.src || f.getAttribute('data-src') || '';
                        if (/studentcourse|studentstudy|knowledge\/course/i.test(s)) return s;
                    }
                    return frs.length ? (frs[0].src || '') : '';
                }""")
            if src:
                return urljoin(page.url, src)
        except Exception:
            pass
        return None

    def _wait_loaded(self, page, result, timeout=25):
        """
        死等 video / 子框架 / studentstudy 出现。返回是否等到。
        进入播放页后会**主动点一次播放按钮**（穿透所有 frame），
        因为超星 video 是点了播放才挂载的，不点永远 videos=0。
        """
        deadline = time.time() + timeout
        last = None
        clicked_play = False
        while time.time() < deadline:
            snap = self._snap(page)
            if (last is None or snap["frameCount"] != last["frameCount"]
                    or snap["totalVideos"] != last["totalVideos"]
                    or snap["url"] != last["url"]):
                result["snapshots"].append(snap)
                self.log(f"    url={snap['url'][:60]} frames={snap['frameCount']} "
                         f"videos={snap['totalVideos']}")
                last = snap

            in_study = "studentstudy" in snap["url"] or "knowledge/course" in snap["url"]
            # 已进入播放页但还没 video → 主动点播放按钮触发 video 挂载
            if in_study and snap["totalVideos"] == 0 and not clicked_play:
                clicked_play = True
                if self._click_play_btn(page):
                    self.log("    已点击播放按钮，等待 video 挂载…")

            if (snap["totalVideos"] > 0 or snap["frameCount"] > 1
                    or in_study):
                # 进播放页了，但还要再等 video 真正出现（除非已经等到）
                if snap["totalVideos"] > 0:
                    return True
                if in_study and clicked_play:
                    # 点了播放还没 video，继续等，不提前返回
                    pass
                elif snap["frameCount"] > 1 and not in_study:
                    return True
                elif in_study and not clicked_play:
                    # 刚进播放页，还没点播放，先继续循环去点
                    pass
            time.sleep(1.0)
        return False

    def _click_play_btn(self, page):
        """
        穿透播放器 frame，用真实鼠标点「播放按钮」。返回是否点到。
        安全：不点 video 元素本身，只点明确的播放按钮；只处理播放器 frame。
        """
        sels = [".vjs-big-play-button", ".ans-video-PlayBtn", ".videoArea .play",
                "[class*='playBtn']", "[class*='play-btn']",
                ".vjs-play-control.vjs-paused"]
        for f in page.frames:
            furl = (f.url or "").lower()
            if not any(k in furl for k in ("ananas", "/video", "modules/video")):
                continue
            for sel in sels:
                try:
                    el = f.query_selector(sel)
                    if el and el.is_visible():
                        el.scroll_into_view_if_needed(timeout=2000)
                        box = el.bounding_box()
                        if box:
                            page.mouse.move(box["x"] + box["width"] / 2,
                                            box["y"] + box["height"] / 2)
                            time.sleep(0.2)
                            page.mouse.down(); time.sleep(0.06); page.mouse.up()
                        else:
                            el.click(timeout=2000)
                        return True
                except Exception:
                    continue
        return False

    def _find_first_chapter(self, page):
        """在所有 frame 里找第一个可点章节，返回 {frame, selector, text}。"""
        sels = [
            ".chapter_item", ".catalog_points .item", "[class*=\'chapter\'] li",
            "[class*=\'catalog\'] li a", ".zj_item", ".timeline a",
            "li[class*=\'node\']", ".nodeItem", "[class*=\'task\'] a",
        ]
        for fi, f in enumerate(page.frames):
            for sel in sels:
                try:
                    els = f.query_selector_all(sel)
                    for el in els:
                        try:
                            h = (el.get_attribute("href") or "") + (el.get_attribute("onclick") or "")
                            txt = (el.inner_text() or "").strip()
                            if re.search(r"chapterId|nodeId|knowledgeId|objectId", h, re.I) and txt:
                                return {"frame": fi, "selector": sel, "text": txt[:40]}
                        except Exception:
                            continue
                except Exception:
                    continue
        return None

    def _native_click_chapter(self, page, chap_info):
        """
        用真实鼠标轨迹点击章节（move + down + up），而非 el.click()。
        el.click() 虽然是 isTrusted=true，但超星可能要求完整的
        mousedown/mouseup 序列或前置 hover，故这里显式模拟整套动作。
        """
        try:
            f = page.frames[chap_info["frame"]]
        except Exception:
            return False
        try:
            els = f.query_selector_all(chap_info["selector"])
            for el in els:
                try:
                    txt = (el.inner_text() or "").strip()
                    if txt and txt[:20] == chap_info["text"][:20]:
                        el.scroll_into_view_if_needed(timeout=3000)
                        time.sleep(0.4)
                        box = el.bounding_box()
                        if not box:
                            el.click(timeout=5000)
                            return True
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        # 真实鼠标：先移过去（hover），再按下、抬起
                        page.mouse.move(cx - 30, cy - 10)
                        time.sleep(0.2)
                        page.mouse.move(cx, cy, steps=8)
                        time.sleep(0.3)
                        page.mouse.down()
                        time.sleep(0.08)
                        page.mouse.up()
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _snap(self, page):
        """抓取一页的结构快照（只读）。逐 frame 报告 video/播放按钮/章节，便于定位。"""
        snap = {"url": page.url, "frameCount": len(page.frames),
                "totalVideos": 0, "chapterEls": 0, "frames": []}
        for i, f in enumerate(page.frames):
            info = {"i": i, "url": f.url[:90], "videos": 0, "playBtn": 0, "chapters": 0}
            try:
                info["videos"] = f.evaluate(
                    "() => document.querySelectorAll('video').length")
            except Exception:
                info["videos"] = "?"
            try:
                info["playBtn"] = f.evaluate(
                    "() => document.querySelectorAll("
                    "'.vjs-big-play-button,.ans-video-PlayBtn,[class*=\"playBtn\"],"
                    "[class*=\"play-btn\"]').length")
            except Exception:
                info["playBtn"] = "?"
            try:
                info["chapters"] = f.evaluate(
                    "() => document.querySelectorAll("
                    "'[class*=\"chapter\"],[class*=\"catalog\"],[class*=\"zj\"]').length")
            except Exception:
                info["chapters"] = "?"
            if isinstance(info["videos"], int):
                snap["totalVideos"] += info["videos"]
            if isinstance(info["chapters"], int):
                snap["chapterEls"] += info["chapters"]
            snap["frames"].append(info)
        return snap

    def _dump(self, out_path, result):
        if not out_path:
            out_path = str(user_dir() / "diagnose.json")
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"写诊断文件失败: {e}", "WARN")



    # ---- 遍历 -----------------------------------------------------
    def run(self, page, single=False):
        """
        主流程：进入课程 -> 抓章节 -> 逐个拼URL进播放页 -> 自动播放。
        进入方式用已验证的「导航 iframe 的 studentcourse src」，
        切章节用「chapterId 拼 studentstudy URL」——点击已被证明无效，不用。
        """
        self.log(f"auto-course-player {__VERSION__}  by {__AUTHOR__}  ({__BUILD_DATE__})")
        # ---------- 第 0 步：确保进入带章节树的页面 ----------
        if "studentstudy" not in page.url and "studentcourse" not in page.url:
            self.log("未在课程页，尝试导航 iframe 的 studentcourse src 进入…")
            iframe_src = self._get_iframe_course_url(page)
            if iframe_src:
                self.log(f"进入课程: {iframe_src[:80]}")
                safe_goto(page, iframe_src, timeout=90000)
                self._wait_course_tree(page)
            else:
                self.log("未找到 studentcourse iframe，按当前页处理", "WARN")
        # 关掉可能出现的"多终端登录/异常学习"安全提示，避免遮挡后续点击
        self._dismiss_security_notice(page, "[入口]")

        if single:
            r = self.play_current(page, 1, "当前页")
            self.stats["done" if r == "done" else "empty"] += 1
            return self.stats

        # ---------- 第 1 步：抓章节 ----------
        chapters = self._extract_chapters(page)
        # 过滤出带 chapterId 的真章节
        chapters = [c for c in chapters if c.get("chapterId")]
        if not chapters:
            # 可能是页面异步渲染未完成，等章节树出现后重试一次
            self.log("首次未抓到章节，等待章节树渲染后重试…")
            self._wait_course_tree(page, timeout=40)
            chapters = [c for c in self._extract_chapters(page) if c.get("chapterId")]
        if not chapters:
            # 保存现场 HTML，便于分析空白页真实内容
            try:
                dump = user_dir() / "studentcourse_fail.html"
                dump.write_text(page.content(), encoding="utf-8")
                self.log(f"已保存现场 HTML: {dump}", "WARN")
            except Exception as e:
                self.log(f"保存现场失败: {e}", "WARN")
            self.log("未抓到带 chapterId 的章节，仅播放当前页", "WARN")
            r = self.play_current(page, 1, "当前页")
            self.stats["done" if r == "done" else "empty"] += 1
            return self.stats

        self.log(f"解析到 {len(chapters)} 个章节")
        # 记录进入课程的基础 URL（用于拼播放页）
        course_url = page.url
        done_file = Path(self.cfg.get("progressFile") or (user_dir() / "progress.json"))
        done_set = set()
        if done_file.exists():
            try:
                done_set = set(json.loads(done_file.read_text()).get("done", []))
            except Exception:
                pass

        # ---------- 第 2 步：逐个切章节（已完成的直接跳过） ----------
        # 页面完成标记是否可用（目录页有任一章节被标记完成 → 标记体系可信）。
        # 可信时以页面为准：页面显示未完成 = 存在未完成任务点（多半是题目没交），
        # 即使进度文件记过也要复查 —— 这保证"刚进去先定位到最近的未完成任务点"，
        # 而不是直接跳到最近的未完成视频。
        page_done_count = sum(1 for c in chapters if c.get("done"))
        trust_page = page_done_count > 0
        if trust_page:
            self.log(f"目录页完成标记可用（{page_done_count} 个章节已标完成）")

        skipped = 0
        recheck = 0
        for i, ch in enumerate(chapters, 1):
            cid = ch["chapterId"]
            page_done = bool(ch.get("done"))
            in_done = cid in done_set
            # ① 页面标记已完成 → 所有任务点都完成，直接跳过
            if page_done:
                skipped += 1
                self.log(f"[{i}/{len(chapters)}] 跳过（页面标记已完成）: "
                         f"{ch['title'][:32]}")
                if not in_done:
                    # 页面标记完成但进度文件没记 → 补记并落盘
                    self.stats["done"] += 1
                    done_set.add(cid)
                    save_done(done_file, done_set)
                continue
            # ② 页面显示未完成，但进度文件记过 → 有未完成任务点（多半是题目
            #    没交上），优先复查补做；进去后视频会被完成检测秒跳，不浪费时间
            if in_done:
                if trust_page:
                    recheck += 1
                    self.log(f"[{i}/{len(chapters)}] 复查（页面显示有未完成任务点，"
                             f"优先补做题目）: {ch['title'][:32]}")
                    # 不 continue，走正常切章流程（前置 try_quiz 先补题目）
                else:
                    # 目录页完成标记不可用（全都没标）→ 只能信进度文件
                    skipped += 1
                    self.log(f"[{i}/{len(chapters)}] 跳过（上次已完成）: "
                             f"{ch['title'][:32]}")
                    continue

            self.log(f"--- 节点 {i}/{len(chapters)} [{ch.get('num','')}] {ch['title'][:36]} ---")
            try:
                ok = self._switch_chapter(page, cid)
                if not ok:
                    self.log(f"[{i}] 章节切换失败，跳过", "WARN")
                    self.stats["failed"] += 1
                    continue
                # 新版 toOld 是整页跳转（top.location.href → transfer → studentstudy），
                # 必须等导航完成并重新注入 chaoxing.js，否则后续操作全部失效
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                time.sleep(self.behavior.get("pageSettleSec", 4))
                # 转场/引导页需要点一下才继续（stucoursemiddle 等）
                self.auto_click_gateway(page, f"[{i}]")
                # 顺手关掉任何安全提示遮罩（多终端/异常学习提醒）
                self._dismiss_security_notice(page, f"[{i}]")
                if "studentstudy" in page.url or "transfer" in page.url:
                    self.log(f"已进入播放页: {page.url[:80]}")
                    try:
                        self.inject(page, page.context)
                    except Exception as e:
                        self.log(f"重注入 chaoxing.js 失败: {e}", "WARN")
                    # 缓存本章播放页链接：提交简答题成功后超星会跳回新版空间首页，
                    # 届时用此链接直接重进本章（老版），避免走选课流程 / 触发 toOld 跳转
                    if "studentstudy" in page.url:
                        ch["_study_url"] = page.url
            except Exception as e:
                self.log(f"切换节点失败: {e}", "ERROR")
                self.stats["failed"] += 1
                continue

            # ---- 任务点优先：进章先做题目（测验/作业），再播视频 ----
            # 视频播完再交卷太晚，且题目任务点才是"卡进度"的关键
            try:
                q0 = self.try_quiz(page, f"[{i}]·前置")
                if q0 == "done":
                    self.log(f"[{i}] 前置题目任务点已完成 ✓")
            except Exception:
                pass

            # 提交简答题成功后，超星会把页面带回新版空间首页（space/index），
            # 主动用进入本章时缓存的 studentstudy 链接重进，避免 play_current
            # 漂移后走选课流程，也避免重复触发安全提示/ toOld 整页跳转。
            if "studentstudy" not in page.url and "studentcourse" not in page.url:
                back = ch.get("_study_url")
                if back:
                    self.log(f"[{i}] 提交后页面跳离，主动重进本章播放页…")
                    try:
                        safe_goto(page, back, timeout=60000)
                        try:
                            page.wait_for_load_state(
                                "domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                        time.sleep(self.behavior.get("pageSettleSec", 4))
                        self.auto_click_gateway(page, f"[{i}]")
                        self._dismiss_security_notice(page, f"[{i}]")
                        self.inject(page, page.context)
                        ch["_study_url"] = page.url  # 更新为实际落到的播放页
                    except Exception as e:
                        self.log(f"[{i}] 重进本章失败: {e}", "WARN")
                else:
                    self.log(f"[{i}] 提交后页面跳离，但无缓存播放页链接，"
                             f"交由 play_current 漂移逻辑处理", "WARN")

            r = self.play_current(page, i, ch["title"])
            # 页面漂移（被跳离播放页）→ 尝试回退到播放页并重试一次
            if r == "drift":
                self.log(f"[{i}] 检测到页面漂移，尝试重新进入播放页…", "WARN")
                try:
                    back = self._build_study_url(page.url, ch, course_url)
                    if back:
                        safe_goto(page, back, timeout=60000)
                        time.sleep(self.behavior.get("pageSettleSec", 4))
                        self.inject(page, page.context)
                        r = self.play_current(page, i, ch["title"])
                except Exception as e:
                    self.log(f"[{i}] 回退失败: {e}", "WARN")
            if r == "done":
                # 绿勾确认整章已完成（且无未播完媒体）→ 直接落盘并跳下一章，
                # 不再重复处理页面内的题目/文档（避免「刚说完成又去播音频」）。
                if getattr(self, "_green_skipped", False):
                    self.log(f"[{i}] 绿勾确认整章已完成，跳下一章")
                    self.stats["done"] += 1
                    done_set.add(cid)
                    save_done(done_file, done_set)
                    continue
                # 视频/音频真正播完，题目/文档可能就在当前任务点页面里 → 顺手处理
                tp = self.handle_task_points(page, f"[{i}]")
                if tp == "failed":
                    # 检测到题目但作答/提交失败（如找不到提交按钮）→ 不要记成完成，
                    # 否则下次整体跳过本章，题目永远交不上。归入 failed 待重试。
                    self.log(f"[{i}] 任务点处理失败，本章暂不标记完成，下次重试", "WARN")
                    self.stats["failed"] += 1
                    continue
                self.stats["done"] += 1
                done_set.add(cid)
                save_done(done_file, done_set)
            elif r == "empty":
                # 无视频节点 → 可能是测验 / 文档 / 音频 / 讨论任务点，走处理器链
                tp = self.handle_task_points(page, f"[{i}]")
                if tp == "done":
                    self.stats["done"] += 1
                    done_set.add(cid)
                    save_done(done_file, done_set)
                elif tp == "failed":
                    self.stats["failed"] += 1
                else:
                    self.stats["empty"] += 1
            else:
                # drift / timeout 等其他情况
                self.stats["failed"] += 1

        # 完成情况汇总
        self.log("-" * 56)
        self.log(f"本节课程共 {len(chapters)} 个节点，本次处理结果：")
        if skipped:
            self.log(f"  跳过（已完成）{skipped} 个")
        if recheck:
            self.log(f"  复查（补做未完成任务点）{recheck} 个")
        self.log(f"  本次完成 {self.stats['done']} 个")
        if self.stats["empty"]:
            self.log(f"  跳过（无视频/非任务点）{self.stats['empty']} 个")
        if self.stats.get("drifted"):
            self.log(f"  页面漂移（被跳离播放页）{self.stats['drifted']} 次", "WARN")
        if self.stats["failed"]:
            self.log(f"  失败 {self.stats['failed']} 个", "WARN")
        self.log(f"  进度文件: {done_file}")
        self.log(f"  已累计记录完成 {len(done_set)} 个节点（下次运行自动跳过）")
        self.log("-" * 56)

        return self.stats


# ---------------------------------------------------------------------------
def _safe_accept(dialog):
    """自动接受所有原生弹窗（confirm/alert/prompt），超星交卷确认用。"""
    try:
        dialog.accept()
    except Exception:
        pass


def save_done(done_file, done_set):
    """进度落盘（已完成的章节 id 集合）。"""
    try:
        done_file.parent.mkdir(parents=True, exist_ok=True)
        done_file.write_text(
            json.dumps({"done": sorted(done_set),
                        "updatedAt": datetime.now().isoformat()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass


def user_dir():
    """跨平台的用户数据目录：用户主目录下的 .auto-course-player/"""
    d = Path.home() / ".auto-course-player"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_path(cfg_value, default_name):
    """
    解析配置中的路径。
    留空则回落到 user_dir() 下的默认文件名，保证 Windows/macOS/Linux 都能用。
    """
    if cfg_value:
        p = Path(os.path.expanduser(str(cfg_value))).resolve()
    else:
        p = user_dir() / default_name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def main():
    ap = argparse.ArgumentParser(description="超星学习通自动播放器")
    ap.add_argument("--config", required=True)
    ap.add_argument("--login", action="store_true", help="首次运行，弹出浏览器供手动登录")
    ap.add_argument("--pick", action="store_true",
                    help="自选课程模式：列出账号下所有课程，输入编号选择")
    ap.add_argument("--single", action="store_true", help="只播当前页，不遍历章节")
    ap.add_argument("--diagnose", action="store_true",
                    help="诊断模式：只打印页面 DOM 结构，不播放（排查选择器失效用）")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        for base in (Path.cwd(), HERE.parent):
            if (base / cfg_path).exists():
                cfg_path = base / cfg_path
                break
    if not cfg_path.exists():
        sys.stderr.write(f"配置不存在: {args.config}\n")
        sys.exit(2)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    log = Log(resolve_path(cfg.get("logFile"), "run.log"))
    port = args.port or cfg.get("cdpPort", 9222)
    profile = str(resolve_path(cfg.get("userDataDir"), "browser-profile"))
    cfg["logFile"] = str(log.logfile) if log.logfile else ""
    cfg["userDataDir"] = profile
    cfg["progressFile"] = str(resolve_path(cfg.get("progressFile"), "progress.json"))

    log(f"配置: {cfg_path}")
    log(f"起始页: {cfg['startUrl']}")
    log(f"Profile: {profile}")

    binary = find_browser(cfg.get("browserPath"))
    if not binary:
        sys.stderr.write(
            "未找到可用的 Chromium 内核浏览器。请先执行：\n"
            "    pip3 install playwright && playwright install chromium\n"
        )
        sys.exit(2)
    log(f"浏览器: {binary}")

    proc = None
    if not port_alive(port):
        Path(profile).mkdir(parents=True, exist_ok=True)
        headless = not has_display()
        if headless:
            log("未检测到 DISPLAY，使用无头模式（容器环境）")
        log(f"启动浏览器 port={port}")
        launch_args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1400,950",
            # 容器 / root 环境下必需
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
        if headless:
            launch_args.append("--headless=new")
        proc = subprocess.Popen(
            launch_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if port_alive(port):
                break
            time.sleep(0.5)
        else:
            sys.stderr.write("浏览器启动失败\n")
            sys.exit(2)

    stopping = {"v": False}

    def _sig(*_):
        stopping["v"] = True
        log("中断信号，收尾中…", "WARN")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 全局弹窗自动接受（超星交卷/提交常弹 confirm/alert）。
            # 同时覆盖所有 frame 的 window.confirm/alert，兜底自定义逻辑。
            try:
                page.on("dialog", lambda d: _safe_accept(d))
            except Exception:
                pass

            # ---------------------------------------------------------------
            # 自选课程模式：先确保登录，再列出课程供选择
            # ---------------------------------------------------------------
            if args.pick:
                orig_start = cfg["startUrl"]
                safe_goto(page, COURSE_LIST_URL, timeout=90000)
                if not is_logged_in(page):
                    # 落在登录页 → 等用户登录后按回车
                    wait_login_manual(page, log)
                    # 登录后重新回到课程列表页
                    safe_goto(page, COURSE_LIST_URL, timeout=90000)
                else:
                    log("检测到已登录 ✓，直接继续")
                cfg["startUrl"] = pick_course(page, log, known_url=orig_start)
                # 选中的课就是配置里那门 → 直接用回原始完整链接（已验证可用）
                m_orig = re.search(r"courseid=(\d+)", orig_start)
                m_pick = re.search(r"courseid=(\d+)", cfg["startUrl"]) or \
                         re.search(r"courseId=(\d+)", cfg["startUrl"])
                if m_orig and m_pick and m_orig.group(1) == m_pick.group(1):
                    log("与配置内课程一致，使用原始完整链接（已验证路径）")
                    cfg["startUrl"] = orig_start
                log(f"目标课程: {cfg['startUrl'][:90]}")
                # 倍速选择 0.5-2（超星服务端会对 >2x 的观看时长增速做校验，超过不计数）
                behavior = cfg.setdefault("behavior", {})
                cur = behavior.get("playbackRate", 1.5)
                try:
                    s = input(f"输入播放倍速 (0.5-2，回车默认 {cur}): ").strip()
                except EOFError:
                    s = ""
                if s:
                    try:
                        v = float(s)
                        if 0.5 <= v <= 2:
                            behavior["playbackRate"] = v
                        else:
                            log("倍速超出 0.5-2，使用默认值", "WARN")
                    except ValueError:
                        log("倍速无效，使用默认值", "WARN")
                log(f"播放倍速: {behavior.get('playbackRate', cur)}x")
                log("提示：建议用 2x —— 超星服务端硬顶 2x，再高会被打回且进度可能不计数", "WARN")

            # ---------------------------------------------------------------
            # 诊断模式：全自动。自己启动浏览器、自己进播放页、把结构写成
            # JSON 文件，全程无需手动复制。登录这一步仍需你扫码/账密一次。
            # ---------------------------------------------------------------
            log("打开课程页")
            final_url = safe_goto(page, cfg["startUrl"], timeout=90000)

            # 登录检测：URL 落在 passport/login 即视为未登录。
            # --login 和 --diagnose 都需要登录后的页面，所以两者都走「等待登录」分支。
            wait_login = args.login or args.diagnose
            if not is_logged_in(page):
                if wait_login:
                    wait_login_manual(page, log,
                                      "未登录：请在浏览器窗口中完成登录（扫码或账密）")
                    final_url = safe_goto(page, cfg["startUrl"], timeout=90000)
                    log(f"当前页: {final_url}")
                else:
                    log("未登录！请先用 --login 参数运行一次完成登录", "ERROR")
                    return
            else:
                log(f"检测到已登录 ✓（当前页: {final_url}）")
                if args.login:
                    log("（--login 模式下等待 20s，可手动确认页面）")
                    time.sleep(20)

            player = CXPlayer(cfg, log)

            if args.diagnose:
                out = str(user_dir() / "diagnose.json")
                player.diagnose(page, out_path=out)
                log(f"诊断结果: {out}")
                return

            stats = player.run(page, single=args.single)

            log("=" * 56)
            log(
                f"完成 {stats['done']} 节 | 非视频 {stats['empty']} 节 | "
                f"失败 {stats['failed']} 节 | 恢复次数 {stats['stuck']}"
            )
            log("=" * 56)
            log("提示：建议登录学习通 App 核对进度是否真的记上了")
    finally:
        if proc:
            log("关闭浏览器")
            proc.terminate()


# 课程列表页候选地址（不同超星部署路径不同，挨个试，取第一个能抓到课程的）
# 默认走 mooc1 老版课程列表（课程在 a[href*=courseid=] 链接里，能稳定到「课程选择页面」），
# mooc2-ans 新版仅作兜底。i.mooc/i.chaoxing 新版空间课程不在链接里、需点「课程」菜单才出列表，
# 脚本自动点经常点不到，会导致「到不了课程选择页面」，故不再优先尝试。
COURSE_LIST_URLS = [
    "https://mooc1.chaoxing.com/visit/courses",
    "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courses",
]
COURSE_LIST_URL = COURSE_LIST_URLS[0]  # 登录检测用的默认地址


def pick_course(page, log, known_url=""):
    """
    交互式选课程：依次尝试多个课程列表页地址（不同部署路径不同），
    抓全部课程链接，编号供选择。
    返回选中的课程 URL；若列表抓取失败，允许用户手动在浏览器里打开课程后回车。
    known_url: 配置里的原始完整课程链接（含 clazzid/cpi/enc）。若选中的课与它
               同 courseid，直接返回它，省去 stucoursemiddle 绕行和等待。
    """
    js = """() => {
        const out = [];
        const seen = new Set();
        // 兼容多种课程链接形式（新版个人空间 / 旧版列表）
        const sel = [
            'a[href*="mycourse/stu"]',
            'a[href*="courseid="]',
            'a[href*="courseId="]',
            '[data-courseid]',
            '.course-info a', '.courseName a', '.Mconright a'
        ].join(',');
        document.querySelectorAll(sel).forEach(a => {
            let href = a.getAttribute('href') || a.dataset.courseid || '';
            const m = (href + '').match(/courseid=(\\d+)|courseId=(\\d+)/i);
            if (!m) return;
            const cid = m[1] || m[2];
            if (!/^\\d{6,}$/.test(cid)) return;
            const key = cid;
            if (seen.has(key)) return;
            seen.add(key);
            let title = (a.getAttribute('title') || a.innerText || '').trim();
            if (!title) {
                const p = a.closest('li, .course, .courseItem, .course-info, div');
                const ne = p ? p.querySelector(
                    '.course-name, .courseName, [class*="name"]') : null;
                title = ne ? ne.innerText.trim() : '';
            }
            if (!title) {
                const li = a.closest('li');
                if (li) title = (li.innerText || '').trim().split('\\n')[0];
            }
            const img = a.querySelector('img');
            if (!title && img) title = (img.getAttribute('alt') || '').trim();
            title = title.replace(/\\s+/g, ' ').slice(0, 60);
            out.push({ title: title || ('课程 ' + cid), url:
                'https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid='
                + cid + '&vc=1' });
        });
        return out;
    }"""
    courses = []
    used_url = None
    for url in COURSE_LIST_URLS:
        log(f"尝试课程列表页: {url[:60]}")
        safe_goto(page, url, timeout=60000)
        # 先直接抓一次（老版 mooc1 列表页本身就有课程链接，无需点任何菜单）
        deadline = time.time() + 8
        card_mode = False
        while time.time() < deadline:
            if "passport" in page.url or "login" in page.url:
                break  # 未登录，先跳出换地址（登录检测在外层做）
            try:
                courses = page.evaluate(js) or []
            except Exception:
                courses = []
            if courses:
                used_url = url
                break
            time.sleep(1.0)
        if courses:
            break
        # 直接抓不到 → 可能是新版个人空间，需先点左侧「课程」菜单
        # 多策略尝试：精确文本 → 包含「课程」的菜单项 → 带 course 的链接
        clicked_any = False
        for strategy in range(3):
            try:
                clicked = page.evaluate(
                    """(st) => {
                        const all = [...document.querySelectorAll('a, li, div, span')];
                        if (st === 0) {
                            const el = all.find(e => (e.innerText || '').trim() === '课程');
                            if (el) { el.click(); return 'exact'; }
                        } else if (st === 1) {
                            const el = all.find(e => {
                                const t = (e.innerText || '').trim();
                                return t === '课程' || t === '我的课程' || t === '我学的课';
                            });
                            if (el) { el.click(); return 'fuzzy'; }
                        } else {
                            const el = all.find(e =>
                                /(^|\\/)course/i.test(e.getAttribute('href') || ''));
                            if (el) { el.click(); return 'href'; }
                        }
                        return null;
                    }""", strategy)
                if clicked:
                    clicked_any = True
                    log(f"已点击「课程」菜单（策略 {strategy}）")
                    time.sleep(2)
                    break
            except Exception:
                continue
        if not clicked_any:
            continue  # 这个列表页没戏，直接换下一个地址，不空等
        deadline = time.time() + 12
        card_mode = False
        while time.time() < deadline:
            if "passport" in page.url or "login" in page.url:
                break  # 未登录，先跳出换地址（登录检测在外层做）
            try:
                courses = page.evaluate(js) or []
            except Exception:
                courses = []
            if courses:
                used_url = url
                break
            # 新版卡片式列表（i.chaoxing.com：任务点进度徽标卡片，无 courseid 链接）
            try:
                cards = page.evaluate("""() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('div, li').forEach(el => {
                        const t = el.innerText || '';
                        if (!/任务点进度[:：]?\\s*\\d+\\//.test(t)) return;
                        const deeper = [...el.querySelectorAll('*')].some(
                            c => /任务点进度[:：]?\\s*\\d+\\//.test(c.innerText || ''));
                        if (deeper) return;
                        const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                        const title = lines.find(l =>
                            !/任务点进度/.test(l) && !/^\\d+%$/.test(l)
                            && !/^[\\u4e00-\\u9fa5]{1,4}$/.test(l)) || lines[0];
                        if (seen.has(title)) return;
                        seen.add(title);
                        out.push({ title: (title || '未命名课程').slice(0, 40),
                                   _raw: lines.slice(0, 5).join(' | ') });
                    });
                    return out;
                }""") or []
            except Exception:
                cards = []
            if cards:
                courses = [{"title": c["title"], "url": "", "_card": True}
                           for c in cards]
                used_url = url
                card_mode = True
                log(f"识别到 {len(courses)} 个新版课程卡片")
                for c in cards[:3]:
                    log(f"   卡片原文: {c.get('_raw', c['title'])[:80]}")
                break
            time.sleep(1.5)
        if courses:
            break
    if used_url:
        log(f"课程列表来源: {used_url[:60]}")
    else:
        # 全部候选都没抓到 → 存下新版页面现场，便于适配选择器
        try:
            dump = user_dir() / "courselist_fail.html"
            dump.write_text(page.content(), encoding="utf-8")
            log(f"未抓到课程，已保存页面现场: {dump}", "WARN")
        except Exception:
            pass

    if courses:
        log("=" * 56)
        log("你的课程列表：")
        for i, c in enumerate(courses, 1):
            log(f"  [{i}] {c['title']}")
        log("=" * 56)
        while True:
            try:
                s = input(f"输入课程编号 (1-{len(courses)})，回车默认选 1: ").strip()
            except EOFError:
                s = ""
            if not s:
                s = "1"
            if s.isdigit() and 1 <= int(s) <= len(courses):
                chosen = courses[int(s) - 1]
                log(f"已选择: {chosen['title']}")
                # 卡片式列表：点击对应卡片进入课程
                if chosen.get("_card"):
                    try:
                        ok = page.evaluate(
                            """(title) => {
                                const els = [...document.querySelectorAll('div, li')];
                                for (const el of els) {
                                    const t = el.innerText || '';
                                    if (!/任务点进度[:：]?\\s*\\d+\\//.test(t)) continue;
                                    const deeper = [...el.querySelectorAll('*')].some(
                                        c => /任务点进度[:：]?\\s*\\d+\\//.test(c.innerText || ''));
                                    if (deeper) continue;
                                    const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                                    const tt = lines.find(l =>
                                        !/任务点进度/.test(l) && !/^\\d+%$/.test(l)
                                        && !/^[\\u4e00-\\u9fa5]{1,4}$/.test(l)) || lines[0];
                                    if ((tt || '').slice(0, 40) === title) {
                                        el.click();
                                        return true;
                                    }
                                }
                                return false;
                            }""", chosen["title"])
                        if ok:
                            log("已点击课程卡片，等待进入课程…")
                            time.sleep(6)
                        # 进入后页面 URL 应带 courseid（可能在新标签页）
                        for p2 in page.context.pages:
                            if "courseid=" in p2.url.lower() or "courseId=" in p2.url:
                                if p2 != page:
                                    try:
                                        page.close()
                                    except Exception:
                                        pass
                                    page = p2
                                    page.bring_to_front()
                                break
                        log(f"进入课程后地址: {page.url[:85]}")
                        if "courseid=" not in page.url.lower():
                            log("未自动进入课程，请在浏览器里手动点开该课程", "WARN")
                            input("打开后按回车继续…")
                        # 复用下方的 stucoursemiddle 参数解析逻辑
                        chosen = {"title": chosen["title"], "url": page.url}
                    except Exception as e:
                        log(f"卡片点击失败: {e}，转手动兜底", "WARN")
                        input("请在浏览器里手动打开想刷的课程，然后按回车…")
                        return page.url
                # 快捷：选中的课与配置里的 known_url 同 courseid → 直接用原始完整链接，
                # 省去 stucoursemiddle 绕行（那步参数常不全，会白等并打印"参数不全"）
                if known_url:
                    kr = re.search(r"courseid=(\d+)", known_url)
                    sr = re.search(r"courseid=(\d+)", chosen["url"])
                    if kr and sr and kr.group(1) == sr.group(1):
                        log(f"与配置内课程一致，直接用原始完整链接: {known_url[:80]}")
                        return known_url
                # 优先试 mooc2-ans 新版入口（stucoursemiddle 跳转），
                # 成功则直接落在带完整参数的新版页面
                mc0 = re.search(r"courseid=(\d+)", chosen["url"])
                mz0 = re.search(r"clazzid=(\d+)", chosen["url"])
                if mc0 and mz0:
                    try:
                        v2 = ("https://mooc2-ans.chaoxing.com/mooc2-ans/visit/"
                              f"stucoursemiddle?courseid={mc0.group(1)}"
                              f"&clazzid={mz0.group(1)}&vc=1")
                        safe_goto(page, v2, timeout=45000)
                        time.sleep(3)
                        if "mooc2-ans" in page.url and "enc=" in page.url \
                                and "无效" not in (page.title() or ""):
                            log(f"新版入口成功: {page.url[:85]}")
                            return page.url
                    except Exception:
                        pass
                # 兜底：mooc1 stucoursemiddle 跳转后带全 cpi/enc 参数
                mid = chosen["url"]
                if "stucoursemiddle" not in mid:
                    mc = re.search(r"courseid=(\d+)", mid)
                    mz = re.search(r"clazzid=(\d+)", mid)
                    if mc and mz:
                        mid = ("https://mooc1.chaoxing.com/visit/stucoursemiddle"
                               f"?courseid={mc.group(1)}&clazzid={mz.group(1)}&vc=1")
                try:
                    safe_goto(page, mid, timeout=60000)
                    time.sleep(3)
                    from urllib.parse import urlparse, parse_qs
                    q = parse_qs(urlparse(page.url).query)
                    g = lambda k: (q.get(k) or [None])[0]
                    c = g("courseId") or g("courseid")
                    z = g("clazzid") or g("clazzId")
                    cp, en = g("cpi"), g("enc")
                    if c and z and cp and en:
                        url = ("https://mooc1.chaoxing.com/mooc-ans/mycourse/studentcourse"
                               f"?courseId={c}&clazzid={z}&cpi={cp}&enc={en}")
                        log(f"解析到完整参数，进入目录页: {url[:90]}")
                        return url
                    log(f"参数不全（{page.url[:70]}），使用跳转结果页", "WARN")
                    return page.url
                except Exception as e:
                    log(f"跟随 stucoursemiddle 跳转失败: {e}，用原链接", "WARN")
                    return mid
            print("编号无效，请重新输入")
    # 抓取失败 → 手动兜底
    log("未能自动抓到课程列表。", "WARN")
    log("请在浏览器里手动打开你想刷的课程（进入看到目录的页面），")
    log("然后回到这里按回车继续…")
    input()
    log(f"使用当前页: {page.url[:90]}")
    return page.url


if __name__ == "__main__":
    main()