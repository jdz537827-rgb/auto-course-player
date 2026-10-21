#!/usr/bin/env python3
"""
signin.py —— 超星学习通远程签到

和刷课共用同一份登录态：不自己走账号密码登录，而是连上已经登录好的浏览器
（CDP），把 cookie 取出来直接调超星的签到接口。
好处是不会触发异地登录 / 密码风控，也不需要把账号密码交出去。

支持的签到类型（接口里的 otherId）：
    0/1 普通、拍照    2 二维码    3 手势    4 位置    5 签到码

用法：
    python signin.py --config chaoxing.json                 # 查一次，有未签就签
    python signin.py --config chaoxing.json --monitor       # 持续监听，来一个签一个
    python signin.py --config chaoxing.json --qr 二维码.png  # 扫码签到（静态图/文本）
    python signin.py --config chaoxing.json --pick-region   # 框选二维码在屏幕上的位置
    python signin.py --config chaoxing.json --qr-screen     # 实时抓屏抢码（动态码）
    python signin.py --config chaoxing.json --dump          # 打印接口原始返回（排查用）

关于动态二维码（重点）
    超星二维码里的 enc 是**会变的**（有的老师把刷新设到 1~10 秒一换）。
    别人发来的截图等你解完码基本已经过期，所以静态解码只适合慢速码；
    真正要抢动态码，必须让程序盯着屏幕实时抓帧 —— 见 --qr-screen：
    它按 3 帧/秒截取屏幕上那块区域，解出 enc 立刻提交，
    整个"抓帧 → 解码 → POST"链路本地完成，通常 200~400ms 内结束。
    前提是这个二维码在你的电脑屏幕上能看到（投屏、会议、远程桌面都算）。

接口说明见 README「远程签到」一节。
"""

import argparse
import base64
import json
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qrdecode                        # noqa: E402
from chaoxing_player import (          # noqa: E402
    Log, find_browser, port_alive, resolve_path,
    is_logged_in, wait_login_manual, safe_goto,
    COURSE_LIST_URL, has_display,
)

MOBILE = "https://mobilelearn.chaoxing.com"
MOOC_API = "https://mooc1-api.chaoxing.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 签到成功/已签，接口都是返回这几个纯文本
OK_WORDS = ("success", "签到成功", "您已签到过了", "已签到")


def walk(obj):
    """深度遍历 JSON，逐个产出 dict。用于兼容超星各版本返回结构不一致。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


# =====================================================================
#  抓屏（不依赖第三方库；装了 Pillow 走 Pillow，没装走 Windows GDI）
# =====================================================================
def _png_from_bgra(raw, w, h):
    """把 GDI 给的 BGRA 原始像素压成 PNG（纯 zlib，不用 Pillow）。"""
    stride = w * 4
    rows = []
    for y in range(h):
        line = raw[y * stride:(y + 1) * stride]
        px = bytearray(stride)
        px[0::4] = line[2::4]      # R
        px[1::4] = line[1::4]      # G
        px[2::4] = line[0::4]      # B
        px[3::4] = b"\xff" * w     # A
        rows.append(b"\x00" + bytes(px))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 1))
            + chunk(b"IEND", b""))


def _gdi_grab(region):
    """Windows GDI BitBlt 抓屏，返回 PNG 字节。region=(x, y, w, h)"""
    import ctypes
    from ctypes import wintypes

    x, y, w, h = [int(v) for v in region]
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    gdi32.SelectObject(mem_dc, bmp)
    gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, 0x00CC0020)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h          # 负值 = 自上而下，省得再翻一次
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)
    return _png_from_bgra(buf.raw, w, h)


def grab_png(region=None):
    """
    抓一帧屏幕（或屏幕上一块区域），返回 PNG 字节；失败返回 None。
    Windows 上 GDI 比 Pillow 快一个数量级（16ms vs 400ms），所以 GDI 优先。
    """
    if sys.platform.startswith("win") and region:
        try:
            return _gdi_grab(region)
        except Exception:
            pass
    try:
        from PIL import ImageGrab
        import io
        if region:
            x, y, w, h = [int(v) for v in region]
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        else:
            img = ImageGrab.grab(all_screens=True)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        return buf.getvalue()
    except Exception:
        pass
    if not region:
        return None
    try:
        return _gdi_grab(region)
    except Exception:
        return None


def dpi_aware():
    """让 Python 的坐标和截屏坐标一致（否则 125% 缩放会错位）。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def pick_region():
    """全屏半透明遮罩上拖一个框，返回 (x, y, w, h)。Esc 取消返回 None。"""
    import tkinter as tk
    dpi_aware()
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.28)
    root.configure(bg="black")
    root.attributes("-topmost", True)
    cv = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    cv.create_text(sw // 2, 40, fill="#7CFC9A", font=("Microsoft YaHei", 18),
                   text="按住鼠标框住二维码区域，松手确定（Esc 取消）")
    box = {"x0": 0, "y0": 0, "rect": None}

    def on_press(e):
        box["x0"], box["y0"] = e.x, e.y
        box["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y,
                                          outline="#7CFC9A", width=3)

    def on_move(e):
        if box["rect"]:
            cv.coords(box["rect"], box["x0"], box["y0"], e.x, e.y)

    def on_release(e):
        root.quit()

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_move)
    cv.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", lambda _e: root.quit())
    root.mainloop()
    try:
        x1, y1, x2, y2 = cv.coords(box["rect"])
    except Exception:
        root.destroy()
        return None
    root.destroy()
    x, y = int(min(x1, x2)), int(min(y1, y2))
    w, h = int(abs(x2 - x1)), int(abs(y2 - y1))
    if w < 40 or h < 40:
        return None
    return [x, y, w, h]


# =====================================================================
#  签到器
# =====================================================================
class CXSigner:
    # otherId: 0/1=普通或拍照  2=二维码  3=手势  4=位置  5=签到码
    KIND_NAME = {0: "普通/拍照", 1: "普通/拍照", 2: "二维码",
                 3: "手势", 4: "位置", 5: "签到码"}

    def __init__(self, ctx, log, cfg, dump=False):
        self.ctx = ctx
        self.log = log
        self.cfg = cfg
        self.dump = dump
        self.signed = set()          # 本次运行已处理过的 activeId，避免重复请求
        self._cookies = None

    # ---- HTTP：用浏览器的 cookie 发请求 ----------------------------
    def refresh_cookies(self):
        try:
            self._cookies = self.ctx.cookies()
        except Exception as e:
            self.log(f"读取 cookie 失败: {e}", "WARN")
            self._cookies = []
        return self._cookies

    def _cookie_header(self, url):
        if self._cookies is None:
            self.refresh_cookies()
        host = urllib.parse.urlparse(url).hostname or ""
        parts = []
        for c in self._cookies or []:
            dom = (c.get("domain") or "").lstrip(".")
            if dom and (host == dom or host.endswith("." + dom)):
                parts.append(f"{c.get('name')}={c.get('value')}")
        return "; ".join(parts)

    def get(self, url, timeout=20):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Cookie": self._cookie_header(url),
            "Referer": "https://mobilelearn.chaoxing.com/",
            "Accept": "*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")

    # ---- 身份参数 ---------------------------------------------------
    def uid_fid(self):
        if self._cookies is None:
            self.refresh_cookies()
        uid = fid = ""
        for c in self._cookies or []:
            n = c.get("name")
            if n in ("_uid", "UID"):
                uid = uid or str(c.get("value") or "")
            elif n == "fid":
                fid = fid or str(c.get("value") or "")
        return uid, fid

    # ---- 课程 -------------------------------------------------------
    def course_from_config(self):
        """从配置的 startUrl 解析 courseId / clazzid（classId 与之同值）。"""
        u = self.cfg.get("startUrl", "") or ""
        cid = re.search(r"course[iI]d=(\d+)", u)
        clz = re.search(r"clazz[iI]d=(\d+)", u) or re.search(r"class[iI]d=(\d+)", u)
        if not (cid and clz):
            return None
        return {"courseId": cid.group(1), "classId": clz.group(1), "name": "配置的课程"}

    def all_courses(self):
        """扫账号下所有课程。返回结构各版本不一致，故做容错解析。"""
        uid, _fid = self.uid_fid()
        url = f"{MOOC_API}/mycourse/backclazzdata?view=json&rss=1"
        if uid:
            url += f"&uid={uid}"
        try:
            txt = self.get(url)
        except Exception as e:
            self.log(f"获取课程列表失败: {e}", "WARN")
            return []
        if self.dump:
            self.log(f"[dump] backclazzdata: {txt[:600]}")
        try:
            data = json.loads(txt)
        except Exception:
            return []
        out, seen = [], set()
        for d in walk(data):
            cid = d.get("courseId") or d.get("courseid")
            clz = d.get("classId") or d.get("clazzId") or d.get("clazzid")
            if not (cid and clz):
                continue
            key = f"{cid}/{clz}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "courseId": str(cid), "classId": str(clz),
                "name": d.get("courseName") or d.get("name") or key,
            })
        return out

    # ---- 活动 -------------------------------------------------------
    def activities(self, course, loose=False):
        uid, _fid = self.uid_fid()
        url = (f"{MOBILE}/ppt/activeAPI/taskactivelist"
               f"?courseId={course['courseId']}&classId={course['classId']}")
        if uid:
            url += f"&uid={uid}"
        try:
            txt = self.get(url)
        except Exception as e:
            self.log(f"获取活动列表失败: {e}", "WARN")
            return []
        if self.dump:
            self.log(f"[dump] taskactivelist({course['name']}): {txt[:800]}")
        try:
            data = json.loads(txt)
        except Exception:
            return []
        acts = []
        for d in walk(data):
            aid = d.get("activeId") or d.get("id") or d.get("activePrimaryId")
            if not aid:
                continue
            kind = d.get("activeType")
            status = d.get("status")
            if not loose:
                # 2 = 签到；status 1 = 进行中/未签。字段缺失时放行，交给接口判定
                if kind is not None and str(kind) != "2":
                    continue
                if status is not None and str(status) in ("2", "3"):
                    continue
            acts.append({
                "activeId": str(aid),
                "name": d.get("name") or d.get("activeName") or "签到",
                "activeType": kind,
                "otherId": d.get("otherId"),
                "ifphoto": d.get("ifphoto"),
                "status": status,
                "_course": course,
            })
        uniq, seen = [], set()
        for a in acts:
            if a["activeId"] in seen:
                continue
            seen.add(a["activeId"])
            uniq.append(a)
        return uniq

    def active_info(self, active_id):
        """活动详情。签到码/位置/个别版本的 enc 都能从这里直接抠出来。"""
        try:
            txt = self.get(f"{MOBILE}/v2/apis/active/getPPTActiveInfo"
                           f"?activeId={active_id}")
        except Exception:
            return {}
        if self.dump:
            self.log(f"[dump] getPPTActiveInfo({active_id}): {txt[:600]}")
        try:
            return json.loads(txt)
        except Exception:
            return {}

    def kind_of(self, act, info=None):
        for src in (act or {}, info or {}):
            for d in walk(src):
                v = d.get("otherId")
                if v is None or str(v) == "":
                    continue
                try:
                    return int(v)
                except Exception:
                    continue
        return None

    def check_sign_code(self, aid, code):
        """手势/签到码必须先过服务端校验，直接提交会返回 90002。"""
        if not code:
            return False
        try:
            txt = self.get(f"{MOBILE}/v2/apis/sign/checkSignCode"
                           f"?activeId={aid}"
                           f"&signCode={urllib.parse.quote(str(code))}")
        except Exception as e:
            if self.dump:
                self.log(f"[dump] checkSignCode 异常: {e}", "WARN")
            return False
        if self.dump:
            self.log(f"[dump] checkSignCode: {txt[:200]}")
        return True

    # ---- 提交签到 ---------------------------------------------------
    def sign(self, act, opts):
        aid = act["activeId"]
        course = act.get("_course") or {}
        uid, fid = self.uid_fid()
        if not uid:
            self.log("取不到 uid（cookie 里没有 _uid），请确认浏览器已登录", "WARN")
            return False

        fast = bool(opts.get("fast"))
        info = {} if fast else self.active_info(aid)
        kind = self.kind_of(act, info)
        kname = self.KIND_NAME.get(kind, "")

        sign_code = ""
        enc = opts.get("enc") or ""
        lat, lon = opts.get("lat") or "", opts.get("lon") or ""
        addr = opts.get("address") or ""
        for d in walk(info):
            if not sign_code and d.get("signCode"):
                sign_code = str(d["signCode"])
            if not enc and d.get("enc"):
                enc = str(d["enc"])
            if (not lat or not lon) and (d.get("locationLatitude") or d.get("latitude")):
                lat = str(d.get("locationLatitude") or d.get("latitude"))
                lon = str(d.get("locationLongitude") or d.get("longitude") or "")
                addr = addr or str(d.get("locationText") or d.get("location") or "")

        if kind == 2 and not enc:
            self.log(f"[{act['name']}] 这是扫码签到，接口必须带二维码里的 enc："
                     f"码如果一直变，用 --qr-screen 实时抓", "WARN")
            return False
        if kind in (3, 5) and sign_code:
            self.check_sign_code(aid, sign_code)

        q = {
            "courseId": course.get("courseId", ""),
            "classId": course.get("classId", ""),
            "activePrimaryId": aid,
            "activeId": aid,            # 两个都带上，各版本/各校接口认的不一样
            "general": "1", "sys": "1", "ls": "1",
            "appType": "15", "tid": "", "uid": uid, "ut": "s", "fid": fid,
        }
        if sign_code:
            q["signCode"] = sign_code
        if enc:
            q["enc"] = enc
        if lat and lon:
            q["lat"], q["lon"] = lat, lon
            q["latitude"], q["longitude"] = lat, lon
            q["address"] = addr
            q["ifTiJiao"] = "1"

        url = f"{MOBILE}/pptSign/stuSignajax?" + urllib.parse.urlencode(q)
        try:
            res = self.get(url)
        except Exception as e:
            self.log(f"[{act['name']}] 签到请求失败: {e}", "WARN")
            return False
        res = res.strip()
        if self.dump:
            self.log(f"[dump] sign({aid}) kind={kname}: {res[:300]}")
        if any(w in res for w in OK_WORDS):
            self.log(f"[{act['name']}] 签到成功 ✓  {kname}  ({res[:40]})")
            return True

        # 通用接口不行再试老接口（个别课程/版本只认这个）
        old = {
            "name": "", "address": addr,
            "activeId": aid, "uid": uid, "clientip": "",
            "latitude": lat or "-1",
            "longitude": lon or "-1",
            "fid": fid, "appType": "15", "ifTiJiao": "1",
        }
        if enc:
            old["enc"] = enc
        if sign_code:
            old["signCode"] = sign_code
        try:
            res2 = self.get(f"{MOBILE}/pptSign/stuSignajax?"
                            + urllib.parse.urlencode(old)).strip()
        except Exception:
            res2 = ""
        if any(w in res2 for w in OK_WORDS):
            self.log(f"[{act['name']}] 签到成功 ✓（兼容接口）")
            return True
        hint = ""
        if "90002" in res:
            hint = "（签到码/手势校验没过，多半是码过期或老师换了码）"
        elif kind == 2:
            hint = "（二维码过期是最常见原因：enc 说变就变，要抢在变化的空档里提交）"
        self.log(f"[{act['name']}] 签到未成功{hint}，接口返回: {res[:80] or res2[:80]}", "WARN")
        return False

    # ---- 二维码 / 扫码签到 ------------------------------------------
    def find_activity(self, aid, courses=None):
        for c in courses or self.all_courses():
            for a in self.activities(c, loose=True):
                if a["activeId"] == str(aid):
                    return a
        return None

    def latest_sign_activity(self, courses=None):
        """找当前最新一个还能签的活动（二维码只带 enc、不带 activeId 时兜底）。"""
        best, best_key = None, -1
        for c in courses or self.all_courses():
            for a in self.activities(c):
                if a["activeId"] in self.signed:
                    continue
                try:
                    key = int(a["activeId"])
                except Exception:
                    key = 0
                if key > best_key:
                    best, best_key = a, key
        return best

    def sign_qr_text(self, text, opts, courses=None):
        """二维码内容已经拿到（解码结果或手贴的文本）→ 解析 → 提交。"""
        p = qrdecode.parse_qr(text)
        enc = p.get("enc") or ""
        if not enc:
            self.log("二维码里没解析出 enc，可能扫的不是签到码", "WARN")
            return False
        act = None
        if p.get("activeId"):
            act = self.find_activity(p["activeId"], courses)
        if act is None:
            act = self.latest_sign_activity(courses)
        if act is None:
            self.log("找不到进行中的签到活动，可能老师已结束或你已签过", "WARN")
            return False
        if act["activeId"] in self.signed:
            return False
        course = act["_course"]
        if p.get("courseId") and p.get("classId"):
            course = {"courseId": p["courseId"], "classId": p["classId"],
                      "name": course.get("name", "二维码课程")}
        act = dict(act)
        act["_course"] = course
        o = dict(opts)
        o["enc"] = enc
        o["fast"] = True          # 抢码时不查详情，省一次往返
        self.log(f"抓到 enc={enc[:16]}{'…' if len(enc) > 16 else ''} "
                 f"(activeId={act['activeId']})")
        ok = self.sign(act, o)
        if ok:
            self.signed.add(act["activeId"])
        return ok

    def sign_qr_file(self, target, opts, courses=None, pw=None):
        """静态二维码：图片路径 / 二维码文本 / 纯 enc 串。"""
        txt = qrdecode.decode_any(target, verbose=self.dump, pw=pw)
        if not txt:
            self.log("二维码识别失败：图里没找到码。截图别裁掉白边、别用缩略图", "WARN")
            return False
        self.log(f"二维码内容: {txt[:120]}")
        return self.sign_qr_text(txt, opts, courses)

    # ---- 一轮扫描 ---------------------------------------------------
    def scan(self, courses, opts, loose=False):
        hits = 0
        for c in courses:
            for a in self.activities(c, loose=loose):
                if a["activeId"] in self.signed:
                    continue
                self.log(f"发现待签活动: {a['name']} (activeId={a['activeId']})")
                # 只在成功后才记入 signed：失败了下一轮还要重试
                # （二维码的 enc 说变就变，第一张抓到的可能是刚过期的）
                if self.sign(a, opts):
                    hits += 1
                    self.signed.add(a["activeId"])
        return hits


# =====================================================================
#  实时抓屏抢码
# =====================================================================
def screen_loop(signer, opts, courses, region, fps, pw, log,
                max_sec=0, stop_on_success=True):
    """
    盯着屏幕上固定区域，一解出新的 enc 就立刻提交。
    动态二维码（1~10 秒一变）只能这么抢：截图转发这条路来不及。
    """
    dec = qrdecode.FrameDecoder(pw=pw, verbose=log)
    seen = set()
    t0 = time.time()
    frames = tried = 0
    log(f"开始实时抓屏：区域={region or '整屏'}，{fps or 3} 帧/秒，Ctrl+C 结束")
    log("（把二维码显示在屏幕上，别让窗口盖住它）")
    try:
        while True:
            if max_sec and time.time() - t0 > max_sec:
                log(f"抓了 {max_sec}s 没签上，先停下。可能是码没露出来，或活动已结束", "WARN")
                return False
            t_frame = time.time()
            png = grab_png(region)
            frames += 1
            if not png:
                log("抓屏失败：没有屏幕可抓（远程/无桌面会话？）", "WARN")
                time.sleep(0.5)
                continue
            txt = dec.decode(png)
            tried += 1
            if txt:
                p = qrdecode.parse_qr(txt)
                enc = p.get("enc") or ""
                if enc and enc not in seen:
                    seen.add(enc)
                    t1 = time.time()
                    ok = signer.sign_qr_text(txt, opts, courses)
                    ms = int((time.time() - t1) * 1000)
                    log(f"抓帧→提交耗时 {ms} ms（本次已见 {len(seen)} 个不同的码）")
                    if ok and stop_on_success:
                        log(f"搞定 ✓  共抓 {frames} 帧")
                        return True
            # 控速
            cost = time.time() - t_frame
            gap = 1.0 / (fps or 3) - cost
            if gap > 0:
                time.sleep(gap)
    except KeyboardInterrupt:
        log("抓屏结束")
        return False
    finally:
        dec.close()


# =====================================================================
def ensure_browser(cfg, log, port):
    """复用 chaoxing_player 的浏览器探测逻辑，必要时拉起一个带 profile 的实例。"""
    binary = find_browser(cfg.get("browserPath"))
    if not binary:
        log("未找到可用浏览器", "ERROR")
        return None
    if port_alive(port):
        return "existing"
    profile = str(resolve_path(cfg.get("userDataDir"), "browser-profile"))
    Path(profile).mkdir(parents=True, exist_ok=True)
    import subprocess
    args = [binary, f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--no-sandbox",
            "--disable-dev-shm-usage", "--window-size=1400,950"]
    if not has_display():
        args.append("--headless=new")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if port_alive(port):
            return "launched"
        time.sleep(0.5)
    return None


def save_region(cfg_path, region, log):
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("signin", {})["qrRegion"] = region
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        log(f"区域已写入配置: {region}（下次 --qr-screen 直接用）")
    except Exception as e:
        log(f"写入配置失败: {e}", "WARN")


def main():
    ap = argparse.ArgumentParser(description="超星学习通远程签到")
    ap.add_argument("--config", default="chaoxing.json")
    ap.add_argument("--monitor", action="store_true", help="持续监听，来一个签一个")
    ap.add_argument("--interval", type=int, default=60, help="监听间隔（秒）")
    ap.add_argument("--all-courses", action="store_true",
                    help="扫描账号下全部课程（默认只签配置里那门）")
    ap.add_argument("--enc", default=None, help="二维码签到：直接给 enc")
    ap.add_argument("--qr", default=None,
                    help="扫码签到：二维码图片路径 / 二维码文本 / enc")
    ap.add_argument("--qr-screen", action="store_true",
                    help="实时抓屏抢码（动态二维码用这个）")
    ap.add_argument("--pick-region", action="store_true",
                    help="框选二维码在屏幕上的区域，存进配置")
    ap.add_argument("--region", default=None, help="抓屏区域 x,y,w,h")
    ap.add_argument("--fps", type=float, default=3.0, help="抓屏帧率，默认 3")
    ap.add_argument("--seconds", type=int, default=0, help="抓屏最长跑多少秒，0=一直跑")
    ap.add_argument("--watch-qr", default=None,
                    help="盯着某个文件夹，新截图存进去就自动签（慢速码用）")
    ap.add_argument("--lat", default=None, help="位置签到：纬度")
    ap.add_argument("--lon", default=None, help="位置签到：经度")
    ap.add_argument("--address", default=None, help="位置签到：详细地址")
    ap.add_argument("--object-id", default=None, help="拍照签到：超星云盘 objectId")
    ap.add_argument("--loose", action="store_true", help="宽松过滤（接口变更时排查用）")
    ap.add_argument("--dump", action="store_true", help="打印接口原始返回")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        for base in (Path.cwd(), HERE):
            if (base / cfg_path).exists():
                cfg_path = base / cfg_path
                break
    if not cfg_path.exists():
        sys.stderr.write(f"配置不存在: {args.config}\n")
        sys.exit(2)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    log = Log(resolve_path(cfg.get("logFile"), "run.log"))
    port = args.port or cfg.get("cdpPort", 9222)

    scfg = cfg.get("signin", {}) or {}
    loc = scfg.get("location", {}) or {}
    opts = {
        "enc": args.enc or scfg.get("enc") or "",
        "lat": args.lat or loc.get("lat") or "",
        "lon": args.lon or loc.get("lon") or "",
        "address": args.address or loc.get("address") or "",
        "object_id": args.object_id or scfg.get("photoObjectId") or "",
    }
    interval = args.interval or scfg.get("pollSeconds", 60)

    # 框选区域只跟屏幕打交道，不用连浏览器
    region = None
    if args.region:
        try:
            region = [int(v) for v in str(args.region).split(",")]
            assert len(region) == 4
        except Exception:
            sys.stderr.write("--region 格式应为 x,y,w,h\n")
            sys.exit(2)
    if args.pick_region:
        region = pick_region()
        if not region:
            print("没框到有效区域")
            sys.exit(1)
        print(f"框选区域: {region}")
        save_region(cfg_path, region, log)
        # 顺手截一张验证能不能解出码
        png = grab_png(region)
        if png:
            txt = qrdecode.decode_bytes(png, verbose=True)
            print("试解结果:", txt or "（这一帧没解出码，确认码确实在这个区域里）")

    if region is None:
        region = scfg.get("qrRegion") or None
        if region:
            region = [int(v) for v in region]

    if not ensure_browser(cfg, log, port):
        sys.exit(2)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.stderr.write("缺少 playwright：pip install playwright\n")
        sys.exit(2)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        safe_goto(page, COURSE_LIST_URL, timeout=90000)
        if not is_logged_in(page):
            wait_login_manual(page, log)
            safe_goto(page, COURSE_LIST_URL, timeout=90000)
        else:
            log("已登录 ✓")

        signer = CXSigner(ctx, log, cfg, dump=args.dump)
        cs = []

        def courses(force=False):
            nonlocal cs
            if cs and not force:
                return cs
            if args.all_courses or not signer.course_from_config():
                cs = signer.all_courses()
                log(f"扫描到 {len(cs)} 门课程")
            else:
                cs = [signer.course_from_config()]
            return cs

        # ---- 1) 实时抓屏抢码（动态二维码）--------------------------
        if args.qr_screen:
            if not region:
                log("没设抓屏区域，默认整屏（慢一点）。强烈建议先跑 --pick-region", "WARN")
            ok = screen_loop(signer, opts, courses(), region, args.fps,
                             pw, log, max_sec=args.seconds)
            if ok or not (args.monitor or args.watch_qr):
                return

        # ---- 2) 静态二维码 / 手贴 enc --------------------------------
        if args.qr or args.enc:
            target = args.qr or args.enc
            if signer.sign_qr_file(target, opts, courses(), pw=pw):
                return

        # ---- 3) 盯文件夹 -------------------------------------------------
        if args.watch_qr:
            d = Path(args.watch_qr).expanduser()
            if not d.is_dir():
                log(f"文件夹不存在: {d}", "ERROR")
                return
            log(f"盯着 {d}：把二维码截图丢进去就自动签（Ctrl+C 结束）")
            seen = {}
            last_scan = 0.0
            try:
                while True:
                    for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime,
                                    reverse=True)[:30]:
                        if f.suffix.lower() not in qrdecode.IMG_EXT:
                            continue
                        mt = f.stat().st_mtime
                        if seen.get(f.name) == mt:
                            continue
                        seen[f.name] = mt
                        if time.time() - mt > 1800:      # 只认半小时内的新图
                            continue
                        log(f"发现新图: {f.name}")
                        signer.sign_qr_file(f, opts, courses(), pw=pw)
                    if args.monitor and time.time() - last_scan > interval:
                        signer.refresh_cookies()
                        signer.scan(courses(), opts, loose=args.loose)
                        last_scan = time.time()
                    time.sleep(2)
            except KeyboardInterrupt:
                log("监听结束")
            return

        # ---- 4) 普通/位置/手势/签到码 签到 -------------------------------
        n = signer.scan(courses(), opts, loose=args.loose)
        log(f"本轮完成签到 {n} 个" if n else "本轮没有可签的活动")

        if not args.monitor:
            return

        log(f"进入监听模式，每 {interval}s 检查一次（Ctrl+C 退出）")
        try:
            while True:
                time.sleep(interval)
                signer.refresh_cookies()
                n = signer.scan(courses(), opts, loose=args.loose)
                if n:
                    log(f"签到成功 {n} 个")
        except KeyboardInterrupt:
            log("监听结束")


if __name__ == "__main__":
    main()
