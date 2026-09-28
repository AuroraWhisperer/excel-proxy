# Excel Proxy

A local proxy that lets Codex use the ChatGPT Excel/BPS backend. You work in Codex as usual; the proxy translates requests, streams replies, and passes tool calls back to the client.

```text
Codex → Excel Proxy on your machine → ChatGPT Excel backend
```

On Windows, you can sign in through the app without opening Excel, save several accounts, and choose which one to use. macOS uses an existing session from the ChatGPT Excel add-in.

This is an independent project, not an official OpenAI or Microsoft integration. Your account still needs access to the upstream models. The proxy does not grant access or bypass account limits.

## What it supports

- Streaming responses, tool calls, image input, and context compaction for long conversations.
- Image generation and editing through the Excel backend.
- Manual account switching, encrypted credential storage, and token refresh on Windows.
- A local dashboard for connections, recent requests, account quotas, and usage estimates.
- Codex configuration backups and restoration when you stop the proxy.

File access, code edits, and shell commands are handled by Codex. The proxy does not execute code returned by the model, and it does not silently switch accounts or models when a request fails.

## Get started on Windows

You need Python 3.11 or later, Codex, Edge or Chrome for sign-in, and the WebView2 Runtime for the desktop window. You also need a ChatGPT account that can use the Excel/BPS backend.

Clone the repository and install its dependencies in PowerShell:

```powershell
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Double-click **启动.vbs** to open the app. The interface is currently in Chinese; the labels below match the buttons you will see.

1. Click **登录并启用代理** (Sign in and enable proxy) and complete sign-in in the official authorization window.
2. The app sends a short request to check the account. This uses a small amount of quota. If it succeeds, the app enables the account, backs up your Codex configuration, and points Codex at the proxy.
3. Restart Codex and start a new conversation.
4. To switch between saved accounts, select one and click **使用此账号** (Use this account). Requests already in progress keep their original account.

Connection tests, the older Excel session import, and configuration recovery are under **高级设置** (Advanced settings).

## macOS setup

Install Python 3.11 or later and sign in to the ChatGPT Excel add-in first. Then run:

```bash
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
bash tools/install_macos.sh
./.venv/bin/python -B app/proxy.py
```

Open the [local dashboard](http://127.0.0.1:8000/). Under **高级设置**, click **读取 Excel 登录** to load the add-in session, then **启用接入** to configure Codex. Restart Codex after the first setup.

Direct sign-in with encrypted account storage is currently Windows-only.

## Starting, stopping, and updating

On Windows, double-clicking the launcher again brings up the existing window. Minimizing it leaves the proxy running; closing it with **×** stops the proxy.

By default, **关闭代理时恢复原配置** restores your previous Codex configuration on exit and reconnects it on the next launch. You can also click **恢复原配置** in Advanced settings and restart Codex to restore the configuration manually.

To update, let active tasks finish, close the proxy, and run:

```powershell
git pull --ff-only
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Then launch it again. Opening a second window without stopping the old process does not load updated code. On macOS, use `./.venv/bin/python` for the dependency command.

For console debugging on Windows:

```powershell
./.venv/Scripts/python.exe -B app/proxy.py
```

## API and limits

The service listens on `127.0.0.1:8000` and checks local connections and browser origins. The API base URL is `http://127.0.0.1:8000/v1`.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/responses` | Responses and tool calls, including streaming |
| `POST /v1/responses/compact` | Context compaction |
| `GET /v1/models` | Local model catalog |
| `POST /v1/images/generations` | Image generation |
| `POST /v1/images/edits` | Image editing |

The bundled catalog includes `gpt-6-astra-excel`, `gpt-5.6-sol-excel` (the default), `gpt-5.6-terra-excel`, and `gpt-5.6-luna-excel`. These are the proxy's model IDs; appearing in the catalog does not guarantee that your account can use a model.

Requests must include the conversation history. Continuing with only `previous_response_id` and forcing a particular tool are not supported. Structured JSON output is requested through the prompt and validated before it is returned; invalid output produces an error.

Image input accepts inline PNG, JPEG, GIF, and WebP: up to 20 MiB per image, 20 images per request, and 32 MiB of inline image data in total. Images included in conversation history count toward those limits. Generation and editing have separate limits documented in the [developer notes](docs/开发说明.md).

## Usage and local data

The dashboard has three pages: connection settings, recent requests, and usage. The request list shows the latest 100 entries without deleting older history. API cost estimates use recorded text tokens and reference prices bundled with the app. They are not a bill or a measure of your remaining Excel quota, and they exclude image generation costs.

On Windows, settings and saved accounts live in `%APPDATA%\ghcp_proxy`; logs, usage records, and tool history live in `%LOCALAPPDATA%\ghcp_proxy`. Saved credentials are encrypted with Windows DPAPI. On macOS, settings and history use `~/Library/Application Support/ghcp_proxy`, caches use `~/Library/Caches/ghcp_proxy`, and the proxy session stays in memory.

Full request logging is off by default. Enabling **记录请求全文** records future requests for debugging. Tool-call arguments are stored separately so tasks can continue after a restart; they may contain filenames, commands, or code even when full request logging is off. Messages and images are sent to the OpenAI Excel backend.

The optional **账号密码登录** form automates credential entry. Its 2FA flow sends the supplied TOTP secret to the third-party site `2fa.fun` to obtain a code. Use the normal **登录并启用代理** flow if you prefer to enter your verification code yourself. See the [sign-in notes](docs/direct-login.md) for details.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| Missing session, expired session, or HTTP 401 | Sign in again. If you use the Excel session import, refresh the add-in and read its session again. |
| Upstream HTTP 403 or a model access error | Select a model your account can use, then test the connection. |
| HTTP 429 | Wait for the upstream rate limit to clear. |
| `local_access_required` | Open the dashboard on this machine using `127.0.0.1`; do not call it from another website or a LAN address. |
| Tool conversion error or interrupted reply | Restart the updated proxy and Codex. If it happens again, keep the error code and request time. |
| The Windows launcher fails | Check the error dialog and `%LOCALAPPDATA%\ghcp_proxy\ghcp-proxy.stderr.log`. |

## Development

Application code, pages, and prompts are in `app/`. Offline regressions are in `tests/`, and setup and diagnostic scripts are in `tools/`.

Run the Python regressions with the repository's virtual environment:

```powershell
./.venv/Scripts/python.exe -B tools/run-offline-tests.py
```

On macOS, use `./.venv/bin/python`. The runner isolates runtime directories and blocks real HTTP requests, so these tests do not use model quota. You can pass a unittest module, class, or method name to run a smaller selection.

The dashboard countdown tests use Node.js:

```text
node --test tests/test_quota_countdown.js
```

The [documentation index](docs/README.md) links to the user guide, account handling, and API details. Those guides are currently in Chinese.

## Credits and license

Thanks to [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) and [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge) for their work on Excel protocol handling, tool history, image uploads, and local access protection. The [reference notes](docs/参考项目与改进.md) describe the implementations consulted.

Released under the [Unlicense](LICENSE).
