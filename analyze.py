#!/usr/bin/env python3
"""
analyze.py —— 课程页探测工具

拿到一个新平台时先跑这个。它会：
  1. 打开课程页，抓取所有 video 元素及其属性
  2. 监听网络请求，识别进度上报接口（心跳）
  3. 探测页面上的目录 / 下一节按钮候选
  4. 输出可直接粘贴进 config 的 selectors 片段

用法：
    python3 analyze.py --url https://xxx.com/course/1
    python3 analyze.py --url https://xxx.com/course/1 --login-wait 60
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.stderr.write("缺少 playwright：sudo pip3 install playwright\n")
    sys.exit(2)

HERE = Path(__file__).resolve().parent


def find_browser():
    """跨平台查找 Chromium 内核：优先 Playwright 自带，其次系统安装。"""
    import glob, shutil

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


def analyze(url: str, login_wait: int, port: int, out: Path):
    binary = find_browser()
    if not binary:
        sys.stderr.write("未找到 chromium / google-chrome\n")
        sys.exit(2)

    import subprocess
    import socket

    alive = False
    with socket.socket() as s:
        s.settimeout(0.5)
        alive = s.connect_ex(("127.0.0.1", port)) == 0

    proc = None
    if not alive:
        print(f"[*] 启动浏览器 port={port}")
        proc = subprocess.Popen(
            [
                binary,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={Path.home()}/.auto-course-player/browser-profile",
                "--no-first-run",
                "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                "--window-size=1280,900",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    alive = True
                    break
            time.sleep(0.5)
        if not alive:
            sys.stderr.write("浏览器启动失败\n")
            sys.exit(2)

    traffic = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 记录所有 XHR / fetch
            def on_resp(resp):
                try:
                    req = resp.request
                    if req.resource_type in ("xhr", "fetch"):
                        body = ""
                        try:
                            body = req.post_data or ""
                        except Exception:
                            pass
                        traffic.append(
                            {
                                "method": req.method,
                                "url": req.url,
                                "status": resp.status,
                                "postData": body[:400],
                            }
                        )
                except Exception:
                    pass

            page.on("response", on_resp)

            print(f"[*] 打开 {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            if login_wait:
                print(f"[*] 等待 {login_wait}s，请在此时间内完成登录")
                time.sleep(login_wait)
            else:
                time.sleep(6)

            # --- 探测 video ---
            videos = page.evaluate(
                """() => Array.from(document.querySelectorAll('video')).map(v => ({
                    id: v.id || '',
                    className: String(v.className || ''),
                    src: (v.currentSrc || v.src || '').slice(0, 200),
                    duration: v.duration || 0,
                    paused: v.paused,
                    hasSourceChild: v.querySelectorAll('source').length > 0,
                    parentClass: String((v.parentElement && v.parentElement.className) || '')
                }))"""
            )

            # --- 探测候选按钮 / 列表 ---
            candidates = page.evaluate(
                """() => {
                    const out = { nextBtn: [], toggles: [], items: [] };
                    document.querySelectorAll('a,button,div[role=button],li').forEach(el => {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 24) return;
                        const cls = String(el.className || '');
                        if (/下一|next|继续/i.test(t)) {
                            out.nextBtn.push({ text: t, tag: el.tagName, className: cls });
                        }
                    });
                    const seen = new Set();
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.getAttribute('href') || '';
                        if (/lesson|chapter|video|course|section|node/i.test(href)) {
                            const key = href;
                            if (seen.has(key)) return;
                            seen.add(key);
                            out.items.push({
                                text: (a.innerText || '').trim().slice(0, 40),
                                href: href.slice(0, 160),
                                className: String(a.className || '')
                            });
                        }
                    });
                    return out;
                }"""
            )

            # 触发一次播放，观察心跳
            print("[*] 尝试触发播放以捕获进度上报…")
            page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; const p = v.play(); if (p&&p.catch) p.catch(()=>{}); }
                }"""
            )
            time.sleep(12)

            status = page.evaluate(
                "() => window.__acp_status ? window.__acp_status() : null"
            )
            page_title = page.title()

            # --- 汇总 ---
            heartbeat = [
                t
                for t in traffic
                if any(
                    k in t["url"].lower()
                    for k in ("progress", "heartbeat", "record", "learn", "study", "log", "report", "play")
                )
            ]

            result = {
                "pageTitle": page_title,
                "finalUrl": page.url,
                "videos": videos,
                "videoStatusAfterPlay": status,
                "nextButtonCandidates": candidates["nextBtn"][:8],
                "tocItemCandidates": candidates["items"][:30],
                "totalXhrRequests": len(traffic),
                "heartbeatCandidates": heartbeat[:20],
                "allXhrSample": traffic[:30],
            }

            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            print("\n" + "=" * 60)
            print(f"页面标题: {page_title}")
            print(f"最终 URL: {page.url}")
            print(f"video 数量: {len(videos)}")
            for i, v in enumerate(videos):
                print(
                    f"  [{i}] id={v['id']!r} class={v['className']!r} "
                    f"duration={v['duration']:.1f}s paused={v['paused']}"
                )
                print(f"      src={v['src'][:110]}")
            if status:
                print(
                    f"播放状态: progress={status.get('progress', 0)*100:.1f}% "
                    f"rate={status.get('rate')} paused={status.get('paused')}"
                )
            print(f"\n下一节按钮候选: {json.dumps(candidates['nextBtn'][:5], ensure_ascii=False)}")
            print(f"目录项候选数: {len(candidates['items'])}")
            for it in candidates["items"][:8]:
                print(f"  - {it['text'][:30]!r} -> {it['href'][:80]}")
            print(f"\nXHR 总数: {len(traffic)}  疑似心跳: {len(heartbeat)}")
            for h in heartbeat[:8]:
                print(f"  {h['method']} {h['url'][:100]}  [{h['status']}]")
                if h["postData"]:
                    print(f"      body: {h['postData'][:150]}")
            print("=" * 60)
            print(f"\n完整结果已写入: {out}")

            print("\n--- 建议填写的 selectors ---")
            sel = {"video": "video"}
            if videos and videos[0]["className"]:
                first_cls = videos[0]["className"].split()[0]
                sel["video"] = f"video.{first_cls}"
            if candidates["nextBtn"]:
                first = candidates["nextBtn"][0]
                if first["className"]:
                    sel["nextBtn"] = f"{first['tag'].lower()}.{first['className'].split()[0]}"
                else:
                    sel["nextBtn"] = f"{first['tag'].lower()}:has-text(\"{first['text']}\")"
            print(json.dumps(sel, ensure_ascii=False, indent=2))

            if proc:
                print("\n[*] 浏览器保持运行以便你继续检查。关闭请手动 kill 或复用该端口。")

    finally:
        pass


def main():
    ap = argparse.ArgumentParser(description="课程页探测工具")
    ap.add_argument("--url", required=True, help="课程页 URL")
    ap.add_argument(
        "--login-wait",
        type=int,
        default=0,
        help="打开页面后等待秒数，用于手动登录",
    )
    ap.add_argument("--port", type=int, default=9222, help="CDP 端口")
    ap.add_argument(
        "--out",
        default=str(HERE.parent / "configs" / "analysis.json"),
        help="分析结果输出路径",
    )
    args = ap.parse_args()

    analyze(args.url, args.login_wait, args.port, Path(args.out))


if __name__ == "__main__":
    main()
