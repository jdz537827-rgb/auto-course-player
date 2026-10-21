#!/usr/bin/env python3
"""
make_deploy.py —— 生成单文件自释放「一键部署.bat」

把当前目录下的最新代码打包进一个 bat。接收方双击即可自动：
  1. 释放程序文件到 <bat 所在目录>\\auto-course-player\\
  2. 检测 Python（无 3.9+ 则静默下载安装 3.12，无需管理员）
  3. pip 安装 playwright（浏览器直接用系统自带 Edge/Chrome，
     不下载几百 MB 的 chromium；实在没有系统浏览器才装）
  4. 释放「启动刷课.bat」，并可直接进入扫码登录 + 选课流程

代码更新后重新跑一次本脚本即可出最新包：
    python make_deploy.py

=============== 后续更新只需要动下面这几个地方 ===============
1. 改代码        → 编辑 chaoxing_player.py / chaoxing.js / chaoxing.template.json
2. 升版本号/作者  → 改下面的 APP_VERSION / AUTHOR / BUILD_DATE（横幅、启动器、
                    配置文件头部全部自动同步，不用再手改 bat）
3. 改/加使用口令  → 改 PASSWORDS 列表（可以加任意多个，任一通过即可）
4. 加打包文件     → 在 EMBED 列表加一行 (源文件名, 释放后文件名, 新标签)
改完执行：python make_deploy.py，把新生成的 一键部署.bat 发出去即可。
"""

import base64
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "一键部署.bat"

# ---- 可配置项（改这里，全包同步）---------------------------------
APP_VERSION = "v1.2"
AUTHOR = "zkk"
BUILD_DATE = "2026.10.11"
# 使用口令：作者真实姓名 / 外号，任意一个通过即可（可自行增删）
PASSWORDS = ["张楷堃", "包子"]
# 更新源地址：把 release/ 目录下的 4 个文件上传到任意静态空间后，
# 把这里填成对应目录的 URL（末尾不要带 /）。留空则对方无法在线更新。
# 例：https://raw.githubusercontent.com/你的账号/仓库/main
#     https://gitee.com/你的账号/仓库/raw/master
UPDATE_BASE = "https://raw.githubusercontent.com/jdz537827-rgb/auto-course-player/main/release"
# 发布清单：更新时会按 files.txt 里的清单逐一下载覆盖（不要放 chaoxing.json，
# 否则会覆盖用户自己的配置）
UPDATE_FILES = ["chaoxing_player.py", "chaoxing.js", "signin.py", "qrdecode.py"]

# (本地源文件, 释放后的文件名, payload 标签)
EMBED = [
    ("chaoxing_player.py", "chaoxing_player.py", "F1"),
    ("chaoxing.js", "chaoxing.js", "F2"),
    ("chaoxing.template.json", "chaoxing.json", "F3"),
    ("signin.py", "signin.py", "F7"),
    ("qrdecode.py", "qrdecode.py", "F9"),
]

# 释放目录里的启动器（含中文，按 GBK 嵌入；中文 Windows 的 cmd 直接可读）
LAUNCHER_BAT = (
    "@echo off\r\n"
    "cd /d \"%~dp0\"\r\n"
    "echo ============================================\r\n"
    "echo   超星自动刷课  {{VERSION}}\r\n"
    "echo   作者: {{AUTHOR}}    {{DATE}}\r\n"
    "echo   首次使用：扫码登录 -^> 输入课程编号 -^> 回车\r\n"
    "echo ============================================\r\n"
    "echo.\r\n"
    "set \"PYEXE=python\"\r\n"
    "where python >nul 2>&1\r\n"
    "if errorlevel 1 set \"PYEXE=%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe\"\r\n"
    "\"%PYEXE%\" chaoxing_player.py --config chaoxing.json --pick\r\n"
    "echo.\r\n"
    "echo 已结束，按任意键关闭…\r\n"
    "pause >nul\r\n"
)

# 释放目录里的签到启动器（GBK 嵌入）
# 注意：cmd 解析复合块时不理会引号，块内任何括号都会打断解析，
# 所以下面 if/else 块内的文本一律不含括号。
SIGNIN_BAT = (
    "@echo off\r\n"
    "cd /d \"%~dp0\"\r\n"
    "echo ============================================\r\n"
    "echo   超星远程签到  {{VERSION}}\r\n"
    "echo   作者: {{AUTHOR}}    {{DATE}}\r\n"
    "echo ============================================\r\n"
    "echo.\r\n"
    "if not \"%~1\"==\"\" goto :qrdrop\r\n"
    "echo   1  只签一次，普通/位置/手势/签到码\r\n"
    "echo   2  持续监听，来一个签一个\r\n"
    "echo   3  扫码签到，给一张二维码图或二维码文字\r\n"
    "echo   4  扫码抢签，二维码在屏幕上每秒都在变\r\n"
    "echo   5  框选二维码在屏幕上的位置\r\n"
    "echo.\r\n"
    "set \"M=1\"\r\n"
    "set /p \"M=请选择 1 到 5，直接回车为 1: \"\r\n"
    "set \"PYEXE=python\"\r\n"
    "where python >nul 2>&1\r\n"
    "if errorlevel 1 set \"PYEXE=%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe\"\r\n"
    "if \"%M%\"==\"2\" goto :mon\r\n"
    "if \"%M%\"==\"3\" goto :qr\r\n"
    "if \"%M%\"==\"4\" goto :screen\r\n"
    "if \"%M%\"==\"5\" goto :pick\r\n"
    "\"%PYEXE%\" signin.py --config chaoxing.json\r\n"
    "goto :end\r\n"
    ":qrdrop\r\n"
    "set \"PYEXE=python\"\r\n"
    "where python >nul 2>&1\r\n"
    "if errorlevel 1 set \"PYEXE=%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe\"\r\n"
    "echo 二维码图片: %~1\r\n"
    "\"%PYEXE%\" signin.py --config chaoxing.json --qr \"%~1\"\r\n"
    "goto :end\r\n"
    ":qr\r\n"
    "echo.\r\n"
    "echo 把二维码图片拖进这个窗口，或直接粘贴二维码里的文字，然后回车：\r\n"
    "set \"QRF=\"\r\n"
    "set /p \"QRF=图片或文字: \"\r\n"
    "set \"QRF=%QRF:\"=%\"\r\n"
    "\"%PYEXE%\" signin.py --config chaoxing.json --qr \"%QRF%\"\r\n"
    "goto :end\r\n"
    ":screen\r\n"
    "echo.\r\n"
    "echo 先把二维码显示在屏幕上（投屏/会议/远程桌面），本窗口别挡住它。\r\n"
    "echo 程序会一直盯着那块区域抢码，签上就自动停。Ctrl+C 可中断。\r\n"
    "\"%PYEXE%\" signin.py --config chaoxing.json --qr-screen --fps 4\r\n"
    "goto :end\r\n"
    ":pick\r\n"
    "\"%PYEXE%\" signin.py --config chaoxing.json --pick-region\r\n"
    "goto :end\r\n"
    ":mon\r\n"
    "\"%PYEXE%\" signin.py --config chaoxing.json --monitor\r\n"
    ":end\r\n"
    "echo.\r\n"
    "echo 已结束，按任意键关闭…\r\n"
    "pause >nul\r\n"
)

# bat 主脚本（纯 ASCII，避免任何代码页问题）。{PAYLOAD} 处追加 base64 数据块。
BAT_MAIN = r"""@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "SELF=%~f0"
title Chaoxing Auto Player - One-Click Deploy
echo ============================================================
echo   Chaoxing Auto Course Player - One-Click Deploy
echo.
echo   Author: {{AUTHOR}}      Date: {{DATE}}      Version: {{VERSION}}
echo ============================================================
echo.

rem ---------- 口令验证（作者授权使用）----------
echo   本程序由 {{AUTHOR}} 制作，使用前需要口令验证。
echo   提示：作者的真实姓名或外号。
set "TRIES=3"
:auth
set "PWOK="
set /p "PW=请输入口令: "
{{AUTH_CHECK}}
if defined PWOK goto :auth_ok
set /a "TRIES-=1"
if !TRIES! GTR 0 (
    echo   口令错误，还可尝试 !TRIES! 次。
    echo.
    goto :auth
)
echo [ERROR] 口令错误，程序退出。
pause
exit /b 1
:auth_ok
echo   验证通过，欢迎使用。
echo.

set "WORKDIR=%~dp0auto-course-player"
if not exist "%WORKDIR%" mkdir "%WORKDIR%"

echo [1/5] Extracting files...
call :extract_one F1 "%WORKDIR%\chaoxing_player.py" || goto :fail
call :extract_one F2 "%WORKDIR%\chaoxing.js" || goto :fail
call :extract_one F3 "%WORKDIR%\chaoxing.json" || goto :fail
call :extract_one F4 "%WORKDIR%\启动刷课.bat" || goto :fail
call :extract_one F5 "%WORKDIR%\一键更新.bat" || goto :fail
call :extract_one F6 "%WORKDIR%\version.txt" || goto :fail
call :extract_one F7 "%WORKDIR%\signin.py" || goto :fail
call :extract_one F8 "%WORKDIR%\一键签到.bat" || goto :fail
call :extract_one F9 "%WORKDIR%\qrdecode.py" || goto :fail
echo       Files extracted to: %WORKDIR%
echo.

rem ---- test mode: set CX_EXTRACT_ONLY=1 to stop after extraction ----
if defined CX_EXTRACT_ONLY (
    echo [MODE] CX_EXTRACT_ONLY set - extraction done.
    exit /b 0
)

echo [2/5] Checking Python...
set "PYEXE="
call :find_py
if not defined PYEXE (
    echo       Python 3.8+ not found. Downloading Python 3.12.7 ...
    set "PYINST=%TEMP%\python-3.12.7-amd64.exe"
    curl -L -o "!PYINST!" "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
    if not exist "!PYINST!" goto :fail_py
    echo       Installing Python silently, no admin required ...
    "!PYINST!" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1
    timeout /t 5 >nul
    set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    if not exist "!PYEXE!" goto :fail_py
)
echo       Python: !PYEXE!
"!PYEXE!" --version
echo.

echo [3/5] Checking playwright...
"!PYEXE!" -c "import playwright" >nul 2>&1
if not errorlevel 1 (
    echo       already installed, skip.
) else (
    echo       Installing playwright ...
    "!PYEXE!" -m pip install --upgrade pip >nul 2>&1
    "!PYEXE!" -m pip install playwright
    if errorlevel 1 (
        rem 常见故障：pip 被配了访问不到的镜像源，回退官方源再试一次
        echo       retry with official PyPI ...
        "!PYEXE!" -m pip install playwright -i https://pypi.org/simple
        if errorlevel 1 goto :fail_pip
    )
)
echo       done.
echo.

echo [4/5] Checking browser...
set "HASBROWSER="
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" set "HASBROWSER=1"
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" set "HASBROWSER=1"
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "HASBROWSER=1"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "HASBROWSER=1"
if defined HASBROWSER (
    echo       Using system Edge/Chrome - no download needed.
) else (
    echo       No Edge/Chrome found, downloading Playwright chromium...
    "!PYEXE!" -m playwright install chromium
)
echo.

echo [5/5] Install complete!
echo ============================================================
echo   Files are in:  %WORKDIR%
echo.
echo   Usage: double-click  "启动刷课.bat"  in that folder.
echo   First run: scan QR code to login, then pick your course.
echo.
echo   Update: double-click  "一键更新.bat"  to get the latest version.
echo ============================================================
echo.
set /p "GO=Start now (scan QR to login)? [Y/n]: "
if /i "!GO!"=="n" exit /b 0
cd /d "%WORKDIR%"
"!PYEXE!" chaoxing_player.py --config chaoxing.json --pick
pause
exit /b 0

rem ==================================================================
:find_py
rem 坑（已踩两次）：cmd 解析复合块 ( ) 时**不理会引号**，块内任何括号都会被打断，
rem 报「此时不应有 ...」后直接终止。所以所有 python -c 都必须写在复合块外面，
rem 一律用 `if errorlevel 1 goto` 跳转来控制流程。
where python >nul 2>&1
if errorlevel 1 goto :fp_py3
python -c "import sys; sys.exit(0 if sys.version_info[0]*100+sys.version_info[1]>=308 else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto :eof
:fp_py3
where python3 >nul 2>&1
if errorlevel 1 goto :fp_store
python3 -c "import sys; sys.exit(0 if sys.version_info[0]*100+sys.version_info[1]>=308 else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=python3"
if defined PYEXE goto :eof
:fp_store
for %%V in (313 312 311 310 309 308) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :eof
    )
)
goto :eof

rem ==================================================================
:extract_one
rem %1=payload tag  %2=output path
set "TAG=%~1"
set "OUTP=%~2"
set "B64F=%TEMP%\cx_%TAG%.b64"
if exist "%B64F%" del /f "%B64F%"
(for /f "usebackq delims=" %%L in (`findstr /B /L /C:"@@%TAG%:" "%SELF%"`) do (
    set "ln=%%L"
    echo(!ln:~5!
)) > "%B64F%"
certutil -decode "%B64F%" "%OUTP%" >nul 2>&1
del /f "%B64F%" >nul 2>&1
if not exist "%OUTP%" exit /b 1
exit /b 0

rem ==================================================================
:fail
echo [ERROR] Extract failed. Try re-downloading this file.
pause
exit /b 1

:fail_py
echo [ERROR] Python install failed. Please install Python 3.9+ manually,
echo         then double-click this file again.
pause
exit /b 1

:fail_pip
echo [ERROR] pip install playwright failed. Check your network/proxy,
echo         then double-click this file again.
pause
exit /b 1

rem ==================================================================
rem  Payload below (base64, prefixed). Never executed.
rem ==================================================================
"""


def payload_block(tag: str, data: bytes) -> str:
    s = base64.b64encode(data).decode("ascii")
    lines = [f"@@{tag}:{s[i:i + 76]}" for i in range(0, len(s), 76)]
    return "\r\n".join(lines) + "\r\n"


# 一键更新脚本（释放到程序目录，对方双击即可拉取最新版）
UPDATER_BAT = (
    "@echo off\r\n"
    "cd /d \"%~dp0\"\r\n"
    "echo ============================================\r\n"
    "echo   一键更新\r\n"
    "echo ============================================\r\n"
    "set \"BASE={{BASE}}\"\r\n"
    "if \"%BASE%\"==\"\" (\r\n"
    "    echo 未配置更新源，无法在线更新。\r\n"
    "    pause\r\n"
    "    exit /b 1\r\n"
    ")\r\n"
    "rem 用独立临时目录，并在下载前清掉旧文件：\r\n"
    "rem 否则 curl 失败时会读到上一次残留的版本号，误判成有新版本。\r\n"
    "set \"TMPD=%TEMP%\\acp_upd\"\r\n"
    "if not exist \"%TMPD%\" md \"%TMPD%\"\r\n"
    "if exist \"%TMPD%\\version.txt\" del /f /q \"%TMPD%\\version.txt\"\r\n"
    "if exist \"%TMPD%\\files.txt\" del /f /q \"%TMPD%\\files.txt\"\r\n"
    "echo 检查新版本...\r\n"
    "rem -f 让 HTTP 错误（404/502 等）也返回非 0，避免把错误页当版本文件\r\n"
    "curl -fsSL -o \"%TMPD%\\version.txt\" \"%BASE%/version.txt\"\r\n"
    "if errorlevel 1 goto :netfail\r\n"
    "if not exist \"%TMPD%\\version.txt\" goto :netfail\r\n"
    "set \"NEWV=\"\r\n"
    "set /p \"NEWV=\"<\"%TMPD%\\version.txt\"\r\n"
    "set \"CURV=unknown\"\r\n"
    "if exist version.txt set /p \"CURV=\"<version.txt\r\n"
    "echo   当前版本: %CURV%\r\n"
    "echo   最新版本: %NEWV%\r\n"
    "if \"%NEWV%\"==\"\" (\r\n"
    "    echo [ERROR] 更新源信息异常，请联系作者。\r\n"
    "    pause\r\n"
    "    exit /b 1\r\n"
    ")\r\n"
    "if /i \"%NEWV%\"==\"%CURV%\" (\r\n"
    "    echo   已经是最新版，无需更新。\r\n"
    "    pause\r\n"
    "    exit /b 0\r\n"
    ")\r\n"
    "echo 下载更新文件...\r\n"
    "curl -fsSL -o \"%TMPD%\\files.txt\" \"%BASE%/files.txt\"\r\n"
    "if errorlevel 1 goto :netfail\r\n"
    "if not exist \"%TMPD%\\files.txt\" goto :netfail\r\n"
    "rem 先全部下载到临时目录，全部成功才覆盖原文件：\r\n"
    "rem 否则某个文件下载失败会用空文件把能用的程序覆盖掉。\r\n"
    "set \"OK=1\"\r\n"
    "for /f \"usebackq delims=\" %%F in (\"%TMPD%\\files.txt\") do (\r\n"
    "    echo   %%F\r\n"
    "    if exist \"%TMPD%\\%%F\" del /f /q \"%TMPD%\\%%F\"\r\n"
    "    curl -fsSL -o \"%TMPD%\\%%F\" \"%BASE%/%%F\"\r\n"
    "    if errorlevel 1 (\r\n"
    "        echo   [WARN] %%F 下载失败\r\n"
    "        set \"OK=0\"\r\n"
    "    )\r\n"
    ")\r\n"
    "if \"%OK%\"==\"0\" (\r\n"
    "    echo.\r\n"
    "    echo [ERROR] 部分文件下载失败，已保留原有文件，请稍后重试。\r\n"
    "    pause\r\n"
    "    exit /b 1\r\n"
    ")\r\n"
    "for /f \"usebackq delims=\" %%F in (\"%TMPD%\\files.txt\") do (\r\n"
    "    if exist \"%%F\" del /f /q \"%%F\"\r\n"
    "    copy /y \"%TMPD%\\%%F\" \"%%F\" >nul\r\n"
    ")\r\n"
    "echo %NEWV%> version.txt\r\n"
    "echo.\r\n"
    "echo 更新完成: %CURV% 到 %NEWV%\r\n"
    "pause\r\n"
    "exit /b 0\r\n"
    ":netfail\r\n"
    "echo.\r\n"
    "echo [ERROR] 无法连接更新源，请检查网络后重试。\r\n"
    "echo 当前程序未做任何改动，可继续使用。\r\n"
    "pause\r\n"
    "exit /b 1\r\n"
)


def fill(main: str, launcher: str, template: bytes, updater: str = "") -> tuple:
    """把版本/作者/口令/更新源等常量注入 bat、启动器、更新脚本、配置模板。"""
    auth_check = "".join(
        f'if /i "!PW!"=="{p}" set "PWOK=1"\r\n' for p in PASSWORDS)
    main = (
        main.replace("{{AUTHOR}}", AUTHOR)
            .replace("{{VERSION}}", APP_VERSION)
            .replace("{{DATE}}", BUILD_DATE)
            .replace("{{AUTH_CHECK}}", auth_check)
    )
    launcher = (
        launcher.replace("{{VERSION}}", APP_VERSION)
                .replace("{{AUTHOR}}", AUTHOR)
                .replace("{{DATE}}", BUILD_DATE)
    )
    updater = updater.replace("{{BASE}}", UPDATE_BASE)
    signin_launcher = (
        SIGNIN_BAT.replace("{{VERSION}}", APP_VERSION)
                  .replace("{{AUTHOR}}", AUTHOR)
                  .replace("{{DATE}}", BUILD_DATE)
    )
    # 配置模板头部同步版本信息
    import json
    conf = json.loads(template.decode("utf-8"))
    conf["_author"] = AUTHOR
    conf["_version"] = APP_VERSION
    conf["_date"] = BUILD_DATE
    template = json.dumps(conf, ensure_ascii=False, indent=2).encode("utf-8")
    return main, launcher, template, updater, signin_launcher


def main():
    parts = [BAT_MAIN]
    launcher = LAUNCHER_BAT
    total = 0
    # F3（配置模板）先注入版本信息再嵌入
    bat_main, launcher, _, updater, signin_launcher = fill(
        BAT_MAIN, launcher, (HERE / "chaoxing.template.json").read_bytes(),
        UPDATER_BAT)
    parts = [bat_main]
    for src, _dst, tag in EMBED:
        p = HERE / src
        if not p.exists():
            sys.stderr.write(f"缺少文件: {p}\n")
            sys.exit(2)
        data = p.read_bytes()
        if src == "chaoxing.template.json":
            _, _, data, _, _ = fill(bat_main, launcher, data, updater)
        total += len(data)
        parts.append(payload_block(tag, data))
        print(f"  嵌入 {src:<26} {len(data):>8} 字节  [{tag}]")
    # F4：启动器（GBK 编码，cmd 中文环境显示正常）
    parts.append(payload_block("F4", launcher.encode("gbk")))
    print(f"  嵌入 {'启动刷课.bat':<26} {len(launcher.encode('gbk')):>8} 字节  [F4]")
    # F5：一键更新脚本；F6：本地版本号
    parts.append(payload_block("F5", updater.encode("gbk")))
    print(f"  嵌入 {'一键更新.bat':<26} {len(updater.encode('gbk')):>8} 字节  [F5]")
    ver = (APP_VERSION + "\r\n").encode("ascii")
    parts.append(payload_block("F6", ver))
    print(f"  嵌入 {'version.txt':<26} {len(ver):>8} 字节  [F6]")
    # F8：签到启动器
    parts.append(payload_block("F8", signin_launcher.encode("gbk")))
    print(f"  嵌入 {'一键签到.bat':<26} {len(signin_launcher.encode('gbk')):>8} 字节  [F8]")

    # 同时产出 release/ 目录：把这 4 个文件上传到任意静态空间即可供对方更新
    rel = HERE / "release"
    rel.mkdir(exist_ok=True)
    for f in UPDATE_FILES:
        (rel / f).write_bytes((HERE / f).read_bytes())
    (rel / "version.txt").write_bytes(ver)
    (rel / "files.txt").write_bytes(("\r\n".join(UPDATE_FILES) + "\r\n").encode("ascii"))
    print(f"\n发布文件已就绪: {rel}  （上传到静态空间后，把 UPDATE_BASE 填成该目录 URL）")

    out = "".join(parts).replace("\n", "\r\n").replace("\r\r\n", "\r\n")
    # bat 内含少量中文（启动器文件名/提示语），用 GBK 写出：
    # 中文 Windows 的 cmd 默认 ANSI=GBK，可正确解析与显示
    with open(OUT, "w", encoding="gbk", newline="") as fp:
        fp.write(out)
    print(f"\n生成: {OUT}  ({OUT.stat().st_size} 字节, 源文件共 {total} 字节)")
    print("提示: 可用 CX_EXTRACT_ONLY=1 模式测试释放是否正确。")


if __name__ == "__main__":
    main()
