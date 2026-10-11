#!/usr/bin/env python3
"""
gh_push.py —— 绕过 git 协议、直接用 GitHub REST API 推送提交

使用场景：
    某些网络环境（公司代理 / 沙箱 / 防火墙）会封掉 git over https 的
    CONNECT 隧道，导致 `git push` 报 "CONNECT tunnel failed, response 502"，
    但 gh CLI（走 api.github.com）依然可用。本脚本用 Git Data API 完成等效推送。

用法：
    git add -A
    python gh_push.py                       # 用 HEAD 的提交信息
    python gh_push.py -m "fix: 修复某某"     # 自定义提交信息

原理：
    1. 读取 git 索引（git ls-files -s），逐文件创建 blob
    2. 用这批 blob 建一棵 tree（全量，不加 base_tree，因此删除文件也能同步）
    3. 建 commit（父提交 = 远端当前分支头）
    4. 更新分支 ref（不存在则创建）

依赖：gh CLI 已登录（gh auth status 通过）。
"""

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(f"[FAIL] {' '.join(cmd[:3])}\n"
                         f"{r.stderr.decode('utf-8', 'ignore')}\n")
        sys.exit(1)
    return r.stdout


def gh_json(args, method=None, fields=None, body=None):
    cmd = ["gh", "api"]
    if method:
        cmd += ["-X", method]
    cmd += args
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    if body is not None:
        cmd += ["--input", "-"]
        r = subprocess.run(cmd, input=json.dumps(body).encode("utf-8"),
                           capture_output=True)
    else:
        r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(f"[FAIL] gh api {args[0]}\n"
                         f"{r.stderr.decode('utf-8', 'ignore')}\n")
        sys.exit(1)
    out = r.stdout.decode("utf-8", "ignore").strip()
    return json.loads(out) if out else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default=None)
    ap.add_argument("-b", "--branch", default=None)
    args = ap.parse_args()

    repo = json.loads(run(["gh", "repo", "view", "--json", "nameWithOwner"]))[
        "nameWithOwner"]
    print(f"仓库: {repo}")

    branch = args.branch or run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()
    msg = args.message or run(
        ["git", "log", "-1", "--pretty=%B"]).decode("utf-8", "ignore").strip()

    # 1) 索引里的文件（mode / blob sha / 路径）
    entries = []
    for line in run(["git", "ls-files", "-s", "-z"]).decode(
            "utf-8", "ignore").split("\0"):
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, _sha, _stage = meta.split()
        entries.append((mode, path))

    if not entries:
        sys.stderr.write("索引为空，请先 git add -A\n")
        sys.exit(1)

    print(f"文件数: {len(entries)}")

    # 2) 逐个建 blob
    tree = []
    for mode, path in entries:
        data = Path(path).read_bytes()
        # 二进制安全：base64 上传。
        # 注意走 --input (stdin) 而非 -f 命令行参数：Windows 命令行上限约 32K，
        # 大文件（如 250KB 的 一键部署.bat）转 base64 后会超限。
        blob = gh_json([f"repos/{repo}/git/blobs"], method="POST",
                       body={"content": base64.b64encode(data).decode(),
                             "encoding": "base64"})
        tree.append({"path": path.replace("\\", "/"), "mode": mode,
                     "type": "blob", "sha": blob["sha"]})
        print(f"  blob {path}  ({len(data)} 字节)")

    # 3) 建 tree
    t = gh_json([f"repos/{repo}/git/trees"], method="POST", body={"tree": tree})
    print(f"tree: {t['sha'][:8]}")

    # 4) 建 commit（父提交 = 远端分支头，空仓库则无父）
    try:
        head = gh_json([f"repos/{repo}/git/ref/heads/{branch}"])
        parents = [head["object"]["sha"]]
        print(f"父提交: {parents[0][:8]}")
    except SystemExit:
        parents = []
        print("空仓库，创建首个提交")

    c = gh_json([f"repos/{repo}/git/commits"], method="POST",
                body={"message": msg, "tree": t["sha"], "parents": parents})
    print(f"commit: {c['sha'][:8]}")

    # 5) 更新或创建分支 ref
    if parents:
        gh_json([f"repos/{repo}/git/refs/heads/{branch}"], method="PATCH",
                fields={"sha": c["sha"]})
    else:
        gh_json([f"repos/{repo}/git/refs"], method="POST",
                fields={"ref": f"refs/heads/{branch}", "sha": c["sha"]})

    print(f"\n推送完成 → https://github.com/{repo}/tree/{branch}")
    print(f"提交: {msg.splitlines()[0] if msg else '(空)'}")


if __name__ == "__main__":
    main()
