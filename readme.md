# Excel Proxy

个人私有仓库：[AuroraWhisperer/excel-proxy](https://github.com/AuroraWhisperer/excel-proxy)。独立维护，使用新的 Git 历史。

通过已登录的 ChatGPT Excel 加载项，为 Codex 提供本地 Responses API。Excel 是唯一上游，无需 GitHub 账号、Copilot、Copilot SDK、Node.js 或 npx。

```text
Codex → 本地代理 → ChatGPT Excel 后端
```

代理仅监听本机回环地址：

- 仪表盘：`http://127.0.0.1:8000/`
- API：`http://127.0.0.1:8000/v1`

## 安装与启动

Windows 日常使用：

1. 在 `D:\Work\ghcp_proxy` 双击 **启动.vbs**。代理在后台运行，准备好后自动打开仪表盘，全程不需要终端。
2. 再次双击 **启动.vbs** 会打开已有实例的仪表盘。
3. 双击 **停止.vbs** 正常关闭代理，并按设置恢复 Codex 原配置。关闭浏览器标签页不会停止代理。

启动失败会弹出错误提示。日志保存在 `%LOCALAPPDATA%\ghcp_proxy\ghcp-proxy.stderr.log` 和 `ghcp-proxy.stdout.log`。

### 首次安装或重建开发环境

需要 Python 3.11+。在 Windows 或 macOS 的 Excel 桌面版中打开官方 ChatGPT 加载项并登录。

Windows PowerShell，在项目目录执行：

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

安装完成后双击 **启动.vbs**。需要在终端查看输出时可执行：

```powershell
./.venv/Scripts/python.exe -B app/proxy.py
```

macOS：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -B app/proxy.py
```

macOS 也可使用 `bash tools/install_macos.sh` 安装。打开仪表盘后：

1. 点击 **读取 Excel 会话**。Windows 从 Office WebView2 缓存读取，并使用 DPAPI 保存；macOS 从 Excel 的 WebKit 存储读取。
2. 选择模型，按需点击 **测试连接**。测试会发送一条短请求，消耗少量上游额度，不会自动执行。
3. 展开 **Codex 与启动设置**，点击 **启用接入**，然后重启 Codex。

无需抓包、调试端口、自定义证书或修改系统代理。会话过期时，在 Excel 中刷新 ChatGPT 加载项，再重新读取。

## 模型与接口

`GET /v1/models` 返回本地 Excel 模型目录，不需要先登录：

| 模型 ID | 推理级别 |
| --- | --- |
| `gpt-6-astra-excel` | `medium`, `high`, `xhigh` |
| `gpt-5.6-luna-excel` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.6-terra-excel` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.6-sol-excel` | `low`, `medium`, `high`, `xhigh` |

Responses 请求也接受去掉 `-excel` 的名称。省略模型时使用 `gpt-5.6-sol-excel`；未知模型返回 400，不会切换到其他后端。模型是否可用取决于当前 Excel 账号权限。

保留的模型接口：

- `POST /v1/responses`：文本、图片、流式回复和 Codex 工具调用。
- `POST /v1/responses/compact`：压缩上下文，保留用户指令与可继续使用的摘要。
- `GET /v1/models`：Excel 模型列表。

上述接口也支持不带 `/v1` 的路径。Chat Completions、Anthropic Messages、GitHub 登录、模型重映射、Copilot 配额和自动更新接口已移除。

请求必须包含对话历史。`previous_response_id`、强制工具选择和结构化输出格式会明确返回 400。图片通过 Excel 附件接口上传，同一账号内复用缓存的文件 ID。工具调用、图片结果与加密推理状态可在后续回合重放。

推理摘要请求统一使用 Excel 网关支持的 `auto` 模式；上游没有返回摘要时，代理不会生成虚构摘要。生成开始后的传输错误不会自动重放请求。上下文压缩失败时保留原始历史，客户端可以重试。

## 仪表盘与配置

仪表盘集中提供 Excel 会话读取、清除缓存、手动连接测试、Codex 配置、登录启动、终端快捷命令和请求详情。页面没有外部 CDN 依赖。

启用 Codex 前会备份已有配置；**恢复原配置** 可撤销接入。默认关闭代理时恢复配置，下次启动时重新接入。升级已有代理配置会更新为 Excel 模型，保留最初的配置备份。

为兼容已有配置和历史记录，继续使用 `ghcp_proxy` 数据目录、`GHCP_*` 环境变量和原有快捷命令名（Windows 为 `Start-GHProxy` / `Stop-GHProxy`，macOS 为 `start-ghproxy` / `stop-ghproxy`）。更新后重启代理，在仪表盘更新 Codex 接入，再重启 Codex。

用量页面只统计经过代理的 Excel 请求，展示输入、缓存和输出 token。旧的其他后端记录不会混入统计。服务方的额度与费用记录为准。

## 排查问题

- **未找到会话 / 401**：打开 Excel，登录或刷新 ChatGPT 加载项，再读取会话。
- **403 / 模型不可用**：改选该 Excel 账号有权使用的模型。
- **429**：等待上游限流解除后重试。
- **测试失败**：仪表盘会区分会话认证、模型权限、限流、请求兼容、响应不完整及超时。连接测试仅验证完整文本回复。
- **查看提示词**：在设置中开启 **记录请求全文**，再从新的请求记录打开详情。默认关闭全文记录；错误响应不会转发上游的原始请求正文。

通过本地 API 检查或清除代理缓存：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/config/excel-session
Invoke-RestMethod -Method Delete http://127.0.0.1:8000/api/config/excel-session
```

Excel 仍保持登录时，后续读取会重新载入本机会话。

## 上游网络配置

| 环境变量 | 用途 |
| --- | --- |
| `GHCP_UPSTREAM_TIMEOUT_SECONDS` | 非流式请求超时，默认 300 秒 |
| `GHCP_UPSTREAM_PROXY` | HTTP/HTTPS 上游代理 |
| `GHCP_HTTP_PROXY`, `GHCP_HTTPS_PROXY` | 分协议设置代理 |
| `GHCP_NO_PROXY` | 不经过代理的主机 |
| `GHCP_UPSTREAM_TLS_VERIFY` | TLS 证书校验开关 |

标准 `HTTP_PROXY`、`HTTPS_PROXY` 和 `NO_PROXY` 也会被读取。Excel 请求使用 HTTP/1.1，避免网关的 HTTP/2 流兼容问题。

## 开发与验证

目录按用途组织：

```text
ghcp_proxy/
├── 启动.vbs              # Windows 双击启动并打开仪表盘
├── 停止.vbs              # Windows 双击正常关闭
├── app/                  # 代理源码与 Windows 启动器
│   ├── proxy.py          # 服务入口
│   ├── static/           # 仪表盘页面
│   └── prompts/          # Codex 提示词
├── tests/                # 离线回归测试
├── tools/                # 测试入口、安装和诊断脚本
├── docs/                 # 说明与实施记录
├── .venv/                # 本项目 Python 环境
└── requirements.txt      # Python 依赖
```

配置继续保存在 `%APPDATA%\ghcp_proxy`，会话、日志和历史记录继续保存在 `%LOCALAPPDATA%\ghcp_proxy`，不随源码目录调整而改变。项目路径改变后，已启用的登录启动或终端快捷命令需在仪表盘重新安装。

运行隔离的离线回归，自动阻止真实 HTTP 传输：

```powershell
./.venv/Scripts/python.exe -B tools/test-proxy-contracts.py
```

macOS 使用 `./.venv/bin/python`。可在命令后追加 unittest 模块、类或方法名做定向检查。不运行广泛的 pytest 自动发现，也不搜索或修改生成的 `mutants/` 目录。

核心模块位于 `app/`：`proxy.py` 负责请求生命周期；`excel_upstream.py` 负责 Excel 协议；`excel_images.py` 处理附件；`excel_session_capture.py` 读取本机会话；`proxy_client_config.py` 管理 Codex 配置；`dashboard.py` 与 `static/dashboard.html` 提供本地用量和操作界面。
