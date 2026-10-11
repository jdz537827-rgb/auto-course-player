#!/usr/bin/env python3
"""
auto-course-player —— 网页版课程平台自动播放引擎

定位：把「一个有视频的课程页面」自动播完。通过 CDP 连接已登录的浏览器，
      注入保活脚本 + 轮询进度 + 自动切下一节，直到课程全部完成。

用法：
    python3 player.py --config configs/example.json
    python3 player.py --config configs/example.json --dry-run

依赖：
    pip3 install playwright
    浏览器需以 --remote-debugging-port=9222 启动（或由本脚本拉起）
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write(
        "缺少 playwright，请先执行：sudo pip3 install playwright && playwright install chromium\n"
    )
    sys.exit(2)

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
STEALTH_JS = (HERE / "stealth.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class Log:
    def __init__(self, logfile: Path | None = None):
        self.logfile = logfile
        if logfile:
            logfile.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line, flush=True)
        if self.logfile:
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
def find_browser():
    """跨平台查找 Chromium 内核：优先 Playwright 自带，其次系统安装。"""
    import glob
    import shutil

    candidates = []
    for base in (
        os.path.expanduser("~/.cache/ms-playwright"),
        os.path.expanduser("~/Library/Caches/ms-playwright"),
        os.path.expanduser("~/AppData/Local/ms-playwright"),
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    ):
        if base and os.path.isdir(base):
            candidates += glob.glob(os.path.join(base, "chromium-*", "chrome-*", "chrome"))
            candidates += glob.glob(os.path.join(base, "chromium-*", "chrome-*", "chrome.exe"))
            candidates += glob.glob(os.path.join(base, "chromium-*", "chrome-*", "Chromium.app",
                                                 "Contents", "MacOS", "Chromium"))
    for c in candidates:
        if os.path.exists(c):
            return c

    for p in (
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
        "/usr/local/bin/chromium", "/opt/google/chrome/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.exists(p):
            return p

    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def user_dir():
    """跨平台的用户数据目录。"""
    d = Path.home() / ".auto-course-player"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------
class Browser:
    """连接或拉起一个带 CDP 调试端口的 Chromium。"""

    def __init__(self, cfg: dict, log: Log):
        self.cfg = cfg
        self.log = log
        self.port = cfg.get("cdpPort", 9222)
        self.proc = None

    def _cdp_alive(self) -> bool:
        import socket

        with socket.socket() as s:
            s.settimeout(0.6)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def _launch(self):
        profile = self.cfg.get("userDataDir") or str(user_dir() / "browser-profile")
        Path(profile).mkdir(parents=True, exist_ok=True)

        binary = find_browser()
        if not binary:
            raise RuntimeError(
                "未找到可用的 Chromium 内核浏览器。请先执行：\n"
                "    pip3 install playwright && playwright install chromium"
            )

        args = [
            binary,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1280,900",
            # 容器 / root 环境下必需
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
        if self.cfg.get("headless"):
            args.append("--headless=new")

        self.log(f"拉起浏览器: {binary} (port={self.port})")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        for _ in range(40):
            if self._cdp_alive():
                return
            time.sleep(0.5)
        raise RuntimeError(f"浏览器 CDP 端口 {self.port} 未就绪")

    def connect(self, pw):
        if not self._cdp_alive():
            self._launch()
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, ctx

    def cleanup(self):
        if self.proc:
            self.proc.terminate()


# ---------------------------------------------------------------------------
# 播放器
# ---------------------------------------------------------------------------
class CoursePlayer:
    def __init__(self, cfg: dict, log: Log):
        self.cfg = cfg
        self.log = log
        self.sel = cfg.get("selectors", {})
        self.behavior = cfg.get("behavior", {})
        self.stats = {"played": 0, "skipped": 0, "failed": 0}

    # -- 工具 ---------------------------------------------------------
    def _q(self, page, key, timeout=8000):
        """按配置取元素，返回 ElementHandle 或 None。"""
        selector = self.sel.get(key)
        if not selector:
            return None
        try:
            return page.wait_for_selector(selector, timeout=timeout, state="attached")
        except PWTimeout:
            return None

    def _q_all(self, page, key):
        selector = self.sel.get(key)
        if not selector:
            return []
        return page.query_selector_all(selector)

    def _inject(self, page):
        page.evaluate(
            """(cfg) => { window.__acp_config = cfg; }""",
            {
                "playbackRate": self.behavior.get("playbackRate", 1.0),
                "mouseJitter": self.behavior.get("mouseJitter", True),
                "minRate": self.behavior.get("playbackRate", 1.0),
            },
        )
        page.evaluate(STEALTH_JS)

    # -- 单节播放 -----------------------------------------------------
    def play_one(self, page, index: int, title: str = "") -> str:
        """
        播放当前页面的视频直到结束。
        返回 'done' | 'empty' | 'timeout'
        """
        label = f"#{index} {title}".strip()
        self._inject(page)

        max_wait = self.behavior.get("maxMinutesPerLesson", 90) * 60
        poll = self.behavior.get("pollIntervalSec", 5)
        stall_limit = self.behavior.get("stallToleranceSec", 90)
        rate = self.behavior.get("playbackRate", 1.0)

        started = time.time()
        last_progress = -1.0
        last_change = time.time()
        announced = False

        while True:
            elapsed = time.time() - started
            if elapsed > max_wait:
                self.log(f"{label} 超过单节上限 {max_wait/60:.0f} 分钟，跳过")
                return "timeout"

            status = page.evaluate("() => window.__acp_status && window.__acp_status()")

            if not status or not status.get("hasVideo"):
                # 页面没有视频：可能是纯文档节，或需要手动切课
                self.log(f"{label} 未检测到 video 元素")
                return "empty"

            if status.get("finished") and status.get("ended"):
                dur = status.get("duration", 0)
                self.log(
                    f"{label} 播放完成 ✓  (时长 {dur/60:.1f} 分钟, 耗时 {elapsed/60:.1f} 分钟)"
                )
                return "done"

            # 进度停滞检测
            if status["progress"] > last_progress + 0.001:
                last_progress = status["progress"]
                last_change = time.time()
                if not announced:
                    self.log(
                        f"{label} 开始播放  总时长 {status['duration']/60:.1f} 分钟 "
                        f"@ {status.get('rate', 1)}x"
                    )
                    announced = True

            if time.time() - last_change > stall_limit:
                self.log(
                    f"{label} 进度停滞 {stall_limit}s（当前 {last_progress*100:.1f}%），尝试恢复",
                    "WARN",
                )
                page.evaluate(
                    """() => {
                        window.__acp_allowPause = true;
                        document.querySelectorAll('video').forEach(v => {
                            const p = v.play(); if (p && p.catch) p.catch(()=>{});
                        });
                        window.__acp_allowPause = false;
                    }"""
                )
                last_change = time.time()

            # 周期打印进度
            if int(elapsed) % 60 == 0 and int(elapsed) > 0:
                self.log(
                    f"{label} 进度 {status['progress']*100:5.1f}%  "
                    f"({status['currentTime']/60:.1f}/{status['duration']/60:.1f} 分钟)"
                )

            time.sleep(poll)

    # -- 课程遍历 -----------------------------------------------------
    def run(self, page) -> dict:
        """按配置的 courseList 遍历全部课时。"""
        lessons = self.cfg.get("courseList", [])
        if not lessons:
            self.log("courseList 为空，仅播放当前页面", "WARN")
            lessons = [{"url": page.url, "title": "当前页面"}]

        for i, lesson in enumerate(lessons, 1):
            url = lesson.get("url")
            title = lesson.get("title", "")
            if not url:
                continue

            self.log(f"--- 第 {i}/{len(lessons)} 节 ---")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                self.log(f"打开超时: {url}", "ERROR")
                self.stats["failed"] += 1
                continue

            # 等播放器就绪
            time.sleep(self.behavior.get("pageSettleSec", 3))

            result = self.play_one(page, i, title)
            if result == "done":
                self.stats["played"] += 1
                self._mark_done(i)
            elif result == "empty":
                self.stats["skipped"] += 1
            else:
                self.stats["failed"] += 1

        return self.stats

    def _mark_done(self, index: int):
        f = Path(self.cfg.get("progressFile") or (user_dir() / "progress.json"))
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.setdefault("done", [])
        if index not in data["done"]:
            data["done"].append(index)
        data["updatedAt"] = datetime.now().isoformat()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="网页版课程自动播放引擎")
    ap.add_argument("--config", required=True, help="配置文件路径 (JSON)")
    ap.add_argument("--dry-run", action="store_true", help="只校验配置，不启动浏览器")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        for base in (Path.cwd(), SKILL_DIR):
            if (base / cfg_path).exists():
                cfg_path = base / cfg_path
                break
    if not cfg_path.exists():
        sys.stderr.write(f"配置文件不存在: {args.config}\n")
        sys.exit(2)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    log = Log(Path(cfg["logFile"]) if cfg.get("logFile") else (user_dir() / "run.log"))

    log(f"配置加载成功: {cfg_path}")
    log(f"课程数: {len(cfg.get('courseList', []))}")

    if args.dry_run:
        required = ["startUrl", "selectors"]
        missing = [k for k in required if k not in cfg]
        if missing:
            log(f"缺少必需配置项: {missing}", "ERROR")
            sys.exit(1)
        if not cfg.get("selectors", {}).get("video"):
            log("selectors.video 未配置", "WARN")
        log("配置校验通过（dry-run）")
        return

    br = Browser(cfg, log)
    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True
        log("收到中断信号，正在收尾…", "WARN")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        with sync_playwright() as pw:
            browser, ctx = br.connect(pw)
            page = ctx.new_page() if not ctx.pages else ctx.pages[0]

            log(f"打开起始页: {cfg['startUrl']}")
            page.goto(cfg["startUrl"], wait_until="domcontentloaded", timeout=60000)

            # 给用户留出登录时间
            login_wait = cfg.get("behavior", {}).get("loginWaitSec", 0)
            if login_wait:
                log(f"等待 {login_wait}s 供登录 / 加载…")
                time.sleep(login_wait)

            player = CoursePlayer(cfg, log)
            stats = player.run(page)

            log("=" * 52)
            log(
                f"完成：播放 {stats['played']} 节 | "
                f"跳过 {stats['skipped']} 节 | 失败 {stats['failed']} 节"
            )
            log("=" * 52)
    finally:
        br.cleanup()


if __name__ == "__main__":
    main()
