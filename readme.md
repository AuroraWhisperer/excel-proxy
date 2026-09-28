<div align="center">

<h1>Excel Connection Service</h1>

<p><strong>Use the OpenAI Excel backend from Codex.</strong><br>
A local connection service for streaming replies, tool calls, images, and account management.</p>

<p><strong>English</strong> · <a href="readme.zh-CN.md">简体中文</a></p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.11 or later">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-555555" alt="Windows and macOS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Unlicense-3A7D44" alt="License: Unlicense"></a>
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="#acknowledgments">Acknowledgments</a> ·
  <a href="https://github.com/AuroraWhisperer/excel-proxy/issues">Report an issue</a>
</p>

</div>

Keep working in Codex as usual. Excel Connection Service translates requests for the Excel/BPS backend and streams replies and tool calls back to the client. Codex handles file access, code edits, and shell commands.

```text
Codex → Excel Connection Service on your machine → OpenAI Excel/BPS backend
```

## What you can do

| Capability | What it provides |
| --- | --- |
| **Everyday Codex work** | Streaming replies, tool calls, and context compaction for long conversations. |
| **Images** | Image input, generation, and editing through the Excel backend. |
| **Account management** | Direct sign-in without opening Excel, manual account switching, encrypted credentials, and token refresh on Windows. |
| **Local dashboard** | Connection settings, recent requests, account quotas, and usage estimates in one place. |
| **Configuration recovery** | Back up the original Codex configuration and restore it when the service stops. |

> This is an independent project, not an official OpenAI or Microsoft integration. Your account needs access to the Excel/BPS backend and the selected model; upstream access and quota limits still apply.

## Quick start

### Windows

**Requirements:** Git, Python 3.11 or later, Codex, Edge or Chrome for sign-in, and the WebView2 Runtime for the desktop window. You also need an OpenAI account with Excel/BPS access. Excel itself is not required for direct sign-in.

**1. Install** — run in PowerShell:

```powershell
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

**2. Sign in** — double-click **启动.vbs**. The interface is currently in Chinese; these are the labels shown in the app:

1. Click **登录并连接** (Sign in and connect) and finish sign-in in the official authorization window.
2. The app checks the account with a short model request, which uses a small amount of quota. On success, it enables the account, backs up your Codex configuration, and configures the service.
3. Restart Codex and start a new conversation.

**3. Use it daily** — choose a saved account and click **使用此账号** (Use this account) to switch. Requests already in progress keep their original account. Minimizing the window keeps the service running; closing **×** stops it.

Connection tests, Excel session import, and configuration recovery are under **高级设置** (Advanced settings). See the [sign-in guide](docs/direct-login.md) for account management details.

<details>
<summary><strong>macOS setup — use an existing Excel add-in session</strong></summary>

Install Git and Python 3.11 or later, and sign in to the OpenAI Excel add-in first. Then run:

```bash
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
bash tools/install_macos.sh
./.venv/bin/python -B app/proxy.py
```

Open the [local dashboard](http://127.0.0.1:8000/). Under **高级设置**, click **读取 Excel 登录** to load the add-in session, then **启用接入** to configure Codex. Restart Codex after the first setup.

Direct sign-in with encrypted account storage is currently Windows-only.

</details>

## Everyday use and reference

Expand the section you need. The [full guides](#documentation) cover setup, account handling, and implementation details.

<details>
<summary><strong>Start, stop, update, or restore the Codex configuration</strong></summary>

On Windows, double-clicking **启动.vbs** again brings up the existing window.

By default, **关闭服务时恢复原配置** restores your previous Codex configuration on exit and reconnects it on the next launch. To restore it manually, click **恢复原配置** in Advanced settings and restart Codex.

To update, let active tasks finish, close the service, and run:

```powershell
git pull --ff-only
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Then launch it again. A second window without stopping the old process does not load updated code. On macOS, use `./.venv/bin/python` for the dependency command.

For console debugging on Windows:

```powershell
./.venv/Scripts/python.exe -B app/proxy.py
```

</details>

<details>
<summary><strong>API endpoints, models, and compatibility limits</strong></summary>

The service listens on `127.0.0.1:8000` and checks local connections and browser origins. The API base URL is `http://127.0.0.1:8000/v1`.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/responses` | Responses and tool calls, including streaming |
| `POST /v1/responses/compact` | Context compaction |
| `GET /v1/models` | Local model catalog |
| `POST /v1/images/generations` | Image generation |
| `POST /v1/images/edits` | Image editing |

**Models:** 6-Astra Excel, 5.6-Sol Excel (default), 5.6-Terra Excel, and 5.6-Luna Excel. Use `GET /v1/models` to retrieve API model IDs; availability depends on your account.

**Compatibility:**

- Include the conversation history with each request. Continuing with only `previous_response_id` and forcing a particular tool are not supported.
- Structured JSON output is requested through the prompt and validated before it is returned. Invalid output produces an error.
- Image input accepts inline PNG, JPEG, GIF, and WebP: up to 20 MiB per image, 20 images per request, and 32 MiB of inline image data in total. Images in conversation history count toward these limits.
- The service does not execute model-generated code or silently switch accounts or models when a request fails.

Generation and editing have separate limits; see the [developer notes](docs/开发说明.md).

</details>

<details>
<summary><strong>Usage estimates, local data, and sign-in privacy</strong></summary>

The dashboard has three pages: connection settings, recent requests, and usage. The request list shows the latest 100 entries without deleting older history. API cost estimates use recorded text tokens and reference prices bundled with the app. They are not a bill or a measure of your remaining Excel quota, and they exclude image generation costs.

| Platform | Storage |
| --- | --- |
| Windows | Settings and saved accounts: `%APPDATA%\ghcp_proxy`. Logs, usage records, and tool history: `%LOCALAPPDATA%\ghcp_proxy`. Saved credentials use Windows DPAPI encryption. |
| macOS | Settings and history: `~/Library/Application Support/ghcp_proxy`. Caches: `~/Library/Caches/ghcp_proxy`. The service session stays in memory. |

Full request logging is off by default. **记录请求全文** records future requests for debugging. Tool-call arguments are stored separately for continuity after a restart; they may contain filenames, commands, or code even when full request logging is off. Messages and images are sent to the OpenAI Excel backend.

The optional **账号密码登录** form automates credential entry. Its 2FA flow sends the supplied TOTP secret to the third-party site [2fa.fun](https://2fa.fun/) to obtain a code. The normal **登录并连接** flow lets you enter the verification code yourself on the official page. See the [sign-in notes](docs/direct-login.md).

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Problem | What to try |
| --- | --- |
| Missing session, expired session, or HTTP 401 | Sign in again. For Excel session import, refresh the add-in and read its session again. |
| Upstream HTTP 403 or a model access error | Select a model your account can use, then test the connection. |
| HTTP 429 | Wait for the upstream rate limit to clear. |
| `local_access_required` | Open the dashboard on this machine using `127.0.0.1`; do not call it from another website or a LAN address. |
| Tool conversion error or interrupted reply | Restart the updated service and Codex. If it happens again, keep the error code and request time. |
| The Windows launcher fails | Check the error dialog and `%LOCALAPPDATA%\ghcp_proxy\ghcp-proxy.stderr.log`. |

More help: [user guide](docs/使用说明.md) · [report an issue](https://github.com/AuroraWhisperer/excel-proxy/issues).

</details>

<details>
<summary><strong>Development and offline tests</strong></summary>

Application code, pages, and prompts are in `app/`. Offline regressions are in `tests/`, and setup and diagnostic scripts are in `tools/`.

Run the Python regressions with the repository's virtual environment:

```powershell
./.venv/Scripts/python.exe -B tools/run-offline-tests.py
```

On macOS, use `./.venv/bin/python`. The runner isolates runtime directories and blocks real HTTP requests, so these tests do not use model quota. Pass a unittest module, class, or method name for a focused run.

The dashboard countdown tests use Node.js:

```text
node --test tests/test_quota_countdown.js
```

</details>

## Documentation

The detailed guides are currently in Chinese. Start with the [中文 README](readme.zh-CN.md) or the [documentation index](docs/README.md).

| Guide | Read it for |
| --- | --- |
| [User guide](docs/使用说明.md) | Setup, everyday use, and troubleshooting |
| [Sign-in and multiple accounts](docs/direct-login.md) | Direct sign-in, account switching, encrypted storage, and token refresh |
| [Account quotas](docs/account-balances.md) | Account imports, quota displays, and reporting periods |
| [Developer notes](docs/开发说明.md) | Module responsibilities, API limits, tests, and runtime options |
| [References and changes](docs/参考项目与改进.md) | Implementation references and the changes they informed |

## Acknowledgments

Thanks to the authors, maintainers, and contributors of these projects for sharing their implementations and tests:

| Project | What we referenced |
| --- | --- |
| [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) | Excel/Basispoints protocol handling, tool history and batch validation, tool transport recovery, and image limits. |
| [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge) | Local Codex-to-Excel bridging, local access protection, client tool conversion, attachment uploads, and error handling. |

The [reference notes](docs/参考项目与改进.md) record the specific source versions and the scope of each adaptation.

## License

Released under the [Unlicense](LICENSE).
