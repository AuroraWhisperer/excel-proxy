<div align="center">

<h1>Excel 连接服务</h1>

<p><strong>在 Codex 中使用 OpenAI Excel 后端。</strong><br>
本机服务，支持流式回复、工具调用、图片处理与账号管理。</p>

<p><a href="readme.md">English</a> · <strong>简体中文</strong></p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.11 或更新版本">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-555555" alt="Windows 和 macOS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Unlicense-3A7D44" alt="许可证：Unlicense"></a>
</p>

<p>
  <a href="#快速开始">快速开始</a> ·
  <a href="#文档导航">文档导航</a> ·
  <a href="#致谢">致谢</a> ·
  <a href="https://github.com/AuroraWhisperer/excel-proxy/issues">问题反馈</a>
</p>

</div>

继续在 Codex 中提问、编写代码和处理项目。Excel 连接服务将请求转换给 Excel/BPS 后端，再把回复和工具调用流式传回客户端。文件读取、代码修改和命令执行由 Codex 完成。

```text
Codex → 本机 Excel 连接服务 → OpenAI Excel/BPS 后端
```

## 能做什么

| 功能 | 使用体验 |
| --- | --- |
| **日常编程** | 支持流式回复、工具调用，以及长对话的上下文压缩。 |
| **图片处理** | 通过 Excel 后端看图、生成图片和编辑图片。 |
| **账号管理** | Windows 可直接登录，无需打开 Excel；支持手动切换账号、凭据加密保存和令牌续期。 |
| **本机控制面板** | 集中查看连接设置、最近请求、账号额度和用量估算。 |
| **配置恢复** | 接入前备份 Codex 原配置，关闭服务时可自动恢复。 |

> 本项目为独立项目，并非 OpenAI 或 Microsoft 官方集成。账号需要具备 Excel/BPS 后端及所选模型的访问权限；服务方的权限与额度限制仍然生效。

## 快速开始

### Windows

**准备条件：** Git、Python 3.11 或更新版本、Codex、用于登录的 Edge 或 Chrome，以及桌面窗口所需的 WebView2 Runtime。还需要一个可访问 Excel/BPS 的 OpenAI 账号。直接登录不需要安装或打开 Excel。

**1. 安装** — 在 PowerShell 中执行：

```powershell
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

**2. 登录** — 双击 **启动.vbs** 打开窗口，应用界面目前为中文：

1. 点击 **登录并连接**，在官方授权窗口完成登录。
2. 程序会发送一次简短模型请求验证账号，消耗少量额度。验证成功后启用账号、备份 Codex 原配置，并自动配置本地接入。
3. 重启 Codex，开启新对话即可使用。

**3. 日常使用** — 选择已保存的账号，点击 **使用此账号** 即可切换。已经开始的请求继续使用原账号。最小化窗口会保持服务运行；点击 **×** 关闭窗口会停止服务。

连接测试、Excel 会话导入和配置恢复位于 **高级设置**。账号管理的详细行为见[登录说明](docs/direct-login.md)。

<details>
<summary><strong>macOS 安装 — 使用已有的 Excel 加载项会话</strong></summary>

先安装 Git、Python 3.11 或更新版本，并在 OpenAI Excel 加载项中完成登录，然后执行：

```bash
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
bash tools/install_macos.sh
./.venv/bin/python -B app/proxy.py
```

打开[本机控制面板](http://127.0.0.1:8000/)，在 **高级设置** 中点击 **读取 Excel 登录**，再点击 **启用接入** 配置 Codex。首次接入后请重启 Codex。

直接登录与账号凭据加密保存目前仅支持 Windows。

</details>

## 日常操作与进阶说明

按需展开下面的内容。安装、账号管理和实现细节也可查阅[完整文档](#文档导航)。

<details>
<summary><strong>启动、关闭、更新与恢复 Codex 配置</strong></summary>

Windows 下再次双击 **启动.vbs**，会显示已经运行的窗口。

默认开启 **关闭服务时恢复原配置**：退出时恢复先前的 Codex 配置，下次启动时重新接入。手动恢复时，在高级设置中点击 **恢复原配置**，然后重启 Codex。

更新前，先等待当前任务完成并关闭服务，再执行：

```powershell
git pull --ff-only
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

完成后重新启动。旧进程未退出时，仅再次打开窗口不会加载新代码。macOS 的依赖安装命令请使用 `./.venv/bin/python`。

Windows 下需要查看控制台输出时，可运行：

```powershell
./.venv/Scripts/python.exe -B app/proxy.py
```

</details>

<details>
<summary><strong>API 接口、模型与兼容范围</strong></summary>

服务监听 `127.0.0.1:8000`，并检查本机连接与浏览器来源。API 基础地址为 `http://127.0.0.1:8000/v1`。

| 接口 | 用途 |
| --- | --- |
| `POST /v1/responses` | 回复与工具调用，支持流式输出 |
| `POST /v1/responses/compact` | 上下文压缩 |
| `GET /v1/models` | 本地模型目录 |
| `POST /v1/images/generations` | 图片生成 |
| `POST /v1/images/edits` | 图片编辑 |

**模型：** 6-Astra Excel、5.6-Sol Excel（默认）、5.6-Terra Excel 和 5.6-Luna Excel。实际 API 模型 ID 可通过 `GET /v1/models` 获取，可用性取决于账号权限。

**兼容范围：**

- 每次请求需携带对话历史；不支持仅靠 `previous_response_id` 接续，也不支持强制指定某个工具。
- 结构化 JSON 输出通过提示词请求，返回前会进行校验；校验失败会明确报错。
- 看图支持内嵌 PNG、JPEG、GIF、WebP：每张最多 20 MiB，每次请求最多 20 张，内嵌图片合计最多 32 MiB。对话历史中的图片也计入限制。
- 服务不执行模型生成的代码，请求失败时也不会静默切换账号或模型。

生图和编辑图片使用单独的限制，详见[开发说明](docs/开发说明.md)。

</details>

<details>
<summary><strong>用量估算、本地数据与登录隐私</strong></summary>

控制面板包含连接设置、最近请求和用量三个页面。请求列表展示最近 100 条记录，不会因此删除更早的历史。API 成本按已记录的文本 token 和内置参考价估算，不是实际账单，也不代表 Excel 剩余额度，且不包含图片生成费用。

| 平台 | 存储位置 |
| --- | --- |
| Windows | 设置与保存的账号：`%APPDATA%\ghcp_proxy`。日志、用量记录与工具历史：`%LOCALAPPDATA%\ghcp_proxy`。凭据使用 Windows DPAPI 加密。 |
| macOS | 设置与历史：`~/Library/Application Support/ghcp_proxy`。缓存：`~/Library/Caches/ghcp_proxy`。服务会话仅保留在内存中。 |

请求全文记录默认关闭。开启 **记录请求全文** 后，会保存后续请求以便排查。工具调用参数会单独保存，用于重启后的任务接续；即使关闭全文记录，其中仍可能包含文件名、命令和代码。消息和图片会发送到 OpenAI 的 Excel 后端。

可选的 **账号密码登录** 会自动填写凭据；其 2FA 流程会将提供的 TOTP 密钥发送到第三方网站 [2fa.fun](https://2fa.fun/) 获取验证码。常规的 **登录并连接** 流程可在官方页面手动输入验证码。具体行为见[登录说明](docs/direct-login.md)。

</details>

<details>
<summary><strong>常见问题</strong></summary>

| 遇到的情况 | 处理方法 |
| --- | --- |
| 未找到会话、会话过期或 HTTP 401 | 重新登录；使用 Excel 会话导入时，刷新加载项后再次读取会话。 |
| 上游 HTTP 403 或模型权限错误 | 选择当前账号可用的模型，再测试连接。 |
| HTTP 429 | 等待服务方的限流解除后重试。 |
| `local_access_required` | 在本机通过 `127.0.0.1` 打开控制面板，不要从其他网站或局域网地址调用。 |
| 工具转换错误或回复中断 | 重启更新后的服务与 Codex；若仍复现，保留错误代码和请求时间。 |
| Windows 双击启动失败 | 查看错误弹窗及 `%LOCALAPPDATA%\ghcp_proxy\ghcp-proxy.stderr.log`。 |

更多帮助：[使用说明](docs/使用说明.md) · [提交问题](https://github.com/AuroraWhisperer/excel-proxy/issues)。

</details>

<details>
<summary><strong>开发与离线测试</strong></summary>

应用代码、页面和提示词位于 `app/`，离线回归测试位于 `tests/`，安装和诊断脚本位于 `tools/`。

使用仓库虚拟环境运行 Python 回归测试：

```powershell
./.venv/Scripts/python.exe -B tools/run-offline-tests.py
```

macOS 请使用 `./.venv/bin/python`。测试运行器会隔离运行数据目录并阻止真实 HTTP 请求，不消耗模型额度。可附加 unittest 模块、类或方法名，只运行指定用例。

控制面板倒计时测试使用 Node.js：

```text
node --test tests/test_quota_countdown.js
```

</details>

## 文档导航

详细文档目前均为中文，可按下表选择，也可查看[文档索引](docs/README.md)。

| 文档 | 内容 |
| --- | --- |
| [使用说明](docs/使用说明.md) | 安装、日常操作与常见问题 |
| [直接登录与多账号](docs/direct-login.md) | 登录、账号切换、加密存储与令牌续期 |
| [账号额度](docs/account-balances.md) | 账号导入、额度显示与统计周期 |
| [开发说明](docs/开发说明.md) | 模块职责、API 限制、测试与运行选项 |
| [参考项目与改进](docs/参考项目与改进.md) | 参考实现与具体改动说明 |

## 致谢

感谢以下项目的作者、维护者与贡献者。公开的实现与测试，为本项目的协议适配和功能完善提供了参考：

| 项目 | 主要参考内容 |
| --- | --- |
| [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) | Excel/Basispoints 协议适配、工具历史与整批校验、工具传输恢复、图片容量限制。 |
| [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge) | 本机 Codex 与 Excel 的桥接、本地访问保护、客户端工具转换、附件上传与错误处理。 |

[参考项目与改进](docs/参考项目与改进.md)记录了具体参考版本和各项适配的范围。

## 许可证

本项目采用 [Unlicense](LICENSE)。
