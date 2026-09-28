<div align="center">

<h1>Excel Connection Service</h1>

<p><strong>Use the OpenAI Excel backend from Codex.</strong><br>
A local connection service for streaming replies, tool calls, images, and account management.</p>

<p><strong>English</strong> · <a href="readme.zh-CN.md">简体中文</a></p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.11 or later">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-555555" alt="Windows and macOS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-3A7D44" alt="License: AGPL-3.0-only"></a>
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="#acknowledgments">Acknowledgments</a> ·
  <a href="#license">License</a> ·
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

## Interface preview

![Connection settings with saved accounts and Codex connection status](docs/images/connection.png)

The connection page, shown with example accounts. The interface is currently in Chinese.

## Quick start

### Windows

**Requirements:**

- Git, Python 3.11 or later, and Codex.
- Edge or Chrome for sign-in, and the WebView2 Runtime for the desktop window.
- An OpenAI account with Excel/BPS access. Excel itself is not required for direct sign-in.

#### 1. Install

Run in PowerShell:

```powershell
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

#### 2. Sign in

Double-click **启动.vbs**. The interface is currently in Chinese; these are the labels shown in the app:

1. Click **登录并连接** (Sign in and connect) and finish sign-in in the official authorization window.
2. The app checks the account with a short model request, which uses a small amount of quota. On success, it enables the account, backs up your Codex configuration, and configures the service.
3. Restart Codex and start a new conversation.

#### 3. Use it daily

Choose a saved account and click **使用此账号** (Use this account) to switch. Requests already in progress keep their original account. Minimizing the window keeps the service running; closing **×** stops it.

Connection tests, Excel session import, and configuration recovery are under **高级设置** (Advanced settings). See the [sign-in guide](docs/direct-login.md) for account management details.

### macOS

<details>
<summary><strong>Set up with an existing Excel add-in session</strong></summary>

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

## Documentation

Choose a guide below, or browse the [documentation index](docs/README.md). Detailed guides are currently in Chinese.

| Guide | Read it for |
| --- | --- |
| [User guide](docs/使用说明.md) | Setup, updates, configuration recovery, and troubleshooting |
| [Sign-in and multiple accounts](docs/direct-login.md) | Direct sign-in, account switching, encrypted storage, and token refresh |
| [Account quotas](docs/account-balances.md) | Account imports, quota displays, and reporting periods |
| [API and compatibility](docs/api.md) | Endpoints, model IDs, tool support, and image limits |
| [Privacy and local data](docs/privacy.md) | Data sent to services, credential storage, and request logs |
| [Developer notes](docs/开发说明.md) | Module responsibilities, internal protocols, tests, and runtime options |
| [References and changes](docs/参考项目与改进.md) | Implementation references and the changes they informed |

## Acknowledgments

Thanks to the authors, maintainers, and contributors of these projects for sharing their implementations and tests:

| Project | What we referenced |
| --- | --- |
| [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) | Excel/Basispoints protocol handling, tool history and batch validation, tool transport recovery, and image limits. |
| [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge) | Local Codex-to-Excel bridging, local access protection, client tool conversion, attachment uploads, and error handling. |

The [reference notes](docs/参考项目与改进.md) record the specific source versions and the scope of each adaptation.

## License

Unless otherwise noted, this project is licensed under the **GNU Affero General Public License v3.0 only** (`AGPL-3.0-only`). **Commercial use is allowed.** The following is a summary; the full terms are in [LICENSE](LICENSE).

- **Copies and changes:** Preserve copyright, license, and warranty notices and include the license. When distributing modified versions, identify the changes and their dates, and license the covered work as a whole under AGPL-3.0-only.
- **Binary distribution:** Provide the complete Corresponding Source using a method permitted by section 6, including the build scripts and installation information required by the license.
- **Modified network services:** Prominently offer every user interacting remotely with a modified version free access to that version's Corresponding Source, as required by section 13.
- **Warranty and liability:** The software is provided as is; the warranty disclaimer and liability limitations in sections 15–16 apply to the extent permitted by law.

Versions and material already released under the [Unlicense](https://github.com/AuroraWhisperer/excel-proxy/blob/3cf755eb9ed5c162e2f90ec2c950825112c548af/LICENSE) retain that grant; this change does not revoke it. Third-party code and dependencies retain their own licenses and attribution requirements.
