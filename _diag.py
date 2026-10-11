from pathlib import Path
raw = Path("一键部署.bat").read_bytes().decode("utf-8", errors="ignore")
clean = raw.replace("\r\n", "\n").replace("\r", "")
lines = clean.split("\n")
N = len(lines)
print("clean line count:", N)
# 关键结构行
def show(kw):
    hits = [i for i,l in enumerate(lines) if kw in l]
    print(f"  '{kw}': {len(hits)} 次; 首现@{hits[:3]} 末现@{hits[-3:] if hits else []}")
for kw in ["@echo off", ":extract", "call :extract", "goto :eof", "CX_EXTRACT_ONLY",
           "chaoxing_player.py --config", "powershell", "curl", "certutil",
           "InstallAllUsers", "playwright install", "chromium"]:
    show(kw)
# payload 块内的文件标记
pidx=[i for i,l in enumerate(lines) if l.startswith('>>"%TMPB64%" echo @@FILE:')]
print("all @@FILE markers:", pidx)
eidx=[i for i,l in enumerate(lines) if l.startswith('>>"%TMPB64%" echo @@END')]
print("all @@END markers:", eidx)
# 看文件结尾 5 行
print("--- tail 5 ---")
for l in lines[-5:]:
    print(repr(l[:80]))
