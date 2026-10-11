# auto-course-player

超星学习通（泛雅 MOOC2-ANS）自动刷课工具 · v1.0

**作者：zkk** · 2026.10.11

---

## 给使用者：三步开始

1. 下载 [`一键部署.bat`](一键部署.bat)，**双击运行**
2. 输入口令（向作者索取）
3. 自动部署完成后，按提示扫码登录 → 输入课程编号 → 回车开刷

之后日常使用只需双击程序目录里的 **`启动刷课.bat`**。

> 首次部署需要联网下载 Python（若无 3.9+）和 playwright，约几分钟，仅此一次。
> 浏览器直接使用系统自带的 Edge / Chrome，不会下载几百 MB 的 chromium。

### 系统要求

- Windows 10 / 11
- 已安装 Edge 或 Chrome
- 联网（仅首次部署）

### 一键更新

程序目录里的 **`一键更新.bat`** 可拉取最新版本，双击即可。它只替换代码文件，**不会覆盖你的 `chaoxing.json` 配置**。

---

## 功能

| 任务点类型 | 支持情况 |
|---|---|
| 视频 | ✅ 自动播放、倍速、防暂停保活 |
| 音频 | ✅ 静音播放（自动甄别真实音频与占位元素） |
| 章节测验 | ✅ 启发式作答 + AI 作答双路 |
| 文档阅读 | ✅ 自动翻阅 |
| 课后讨论 | ✅ AI 生成回帖内容并自动发布 |

### 设计原则

**不伪造心跳包，不重算 enc 签名。**

浏览器注入脚本只做「拟人化」——伪造 visibility / 焦点状态、随机鼠标抖动、拦截外部 pause、抵抗倍速重置。真正的进度心跳由超星**自己的播放器**合法发出，因此不存在伪造请求带来的封号风险。

---

## 配置

配置文件 `chaoxing.json` 由部署脚本从模板生成，常用项：

```jsonc
{
  "behavior": {
    "playbackRate": 1.5,        // 倍速，建议不超过 2（服务端有校验）
    "maxMinutesPerLesson": 120  // 单节超时上限
  },
  "features": {                 // 各类任务点开关，出问题可单独停用
    "quiz": true,
    "document": true,
    "discussion": true
  },
  "ai": {
    "enabled": false,           // AI 自动答题
    "apiKey": ""                // 建议留空，改用环境变量 CX_AI_KEY
  },
  "discussion": {
    "replyHint": "...",         // 喂给 AI 的回帖提示词
    "fallbackReply": ""         // AI 不可用时的兜底回复
  }
}
```

### 关于 API Key

**不要把 key 写进 `chaoxing.json`**（该文件在多人电脑间流转容易泄露）。改用环境变量：

```bat
set CX_AI_KEY=sk-xxxxxx
启动刷课.bat
```

环境变量优先级高于配置文件。

---

## 给开发者

### 本地运行

```bash
pip install playwright
python chaoxing_player.py --config chaoxing.json --pick
```

### 项目结构

```
chaoxing_player.py      主引擎（播放器 / 任务点 / AI 答题 / 主流程）
chaoxing.js             页面内保活注入脚本
chaoxing.template.json  脱敏配置模板（请复制为 chaoxing.json 后修改）
make_deploy.py          打包器：把最新代码打成单文件 一键部署.bat
一键部署.bat            分发用单文件自释放包（含口令验证）
release/                在线更新源文件（version.txt / files.txt / 代码）
player.py + stealth.js  通用版引擎，适配超星以外的平台
analyze.py              新平台探测工具
```

### 新增一种任务点类型（两步，无需改主流程）

1. 在 `CXPlayer` 里写一个方法，返回 `"done"` / `"failed"` / `"none"`：

   ```python
   def try_xxx(self, page, label=""):
       ...
       return "done"
   ```

2. 在文件顶部的注册表里加一行：

   ```python
   TASK_HANDLERS = [
       ("quiz", "try_quiz"),
       ("document", "try_document"),
       ("discussion", "try_discussion"),
       ("xxx", "try_xxx"),       # ← 新增
   ]
   ```

主流程会按序调用，并在 `features` 配置里自动生成对应的开关。

### 发新版

1. 改代码
2. 改 `make_deploy.py` 顶部的 `APP_VERSION`（以及 `AUTHOR` / `PASSWORDS` / `UPDATE_BASE` 如需）
3. 跑 `python make_deploy.py`
4. 提交并推送（见下）

`release/` 目录里的 4 个文件就是对方的更新源。本项目的更新源直接托管在仓库内：

```
https://raw.githubusercontent.com/jdz537827-rgb/auto-course-player/main/release
```

所以**推送到 main 即等于发布新版本**，对方双击「一键更新」即可同步。

### 推送到 GitHub

正常情况用 git 即可：

```bash
git add -A && git commit -m "..." && git push
```

若所在网络封掉了 git over https（`git push` 报 `CONNECT tunnel failed, response 502`
或 `schannel: failed to receive handshake`），改用仓库内的 `gh_push.py`——
它走 GitHub REST API，不依赖 git 协议：

```bash
git add -A
python gh_push.py -m "feat: 某某改动"
```

前提：`gh auth status` 已登录。

---

## 免责声明

本项目仅供学习浏览器自动化（Playwright / CDP）与前端逆向分析技术之用。

请遵守所在学校的规章制度与平台服务条款，合理使用该工具完成学习任务。因使用本工具产生的任何后果由使用者自行承担，与作者无关。

---

## License

[MIT](LICENSE) © 2026 zkk
