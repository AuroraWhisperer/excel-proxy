# Excel Proxy

Excel Proxy 是一个本地代理，让 Codex 使用 ChatGPT for Excel 的登录会话。你还是在 Codex 里提问、改代码和运行任务，代理负责把请求转成 Excel 后端接受的格式，再把回复传回来。

```text
Codex → 本机 Excel Proxy → ChatGPT Excel 后端
```

这是一个独立维护的个人项目，目前只接 Excel 后端。文件读取、代码修改和命令执行都由 Codex 客户端完成，代理本身不执行模型返回的代码。

## 目前能做什么

- 转发文本和图片请求，支持流式回复、工具调用和长对话的上下文压缩。
- 提供图片生成和编辑接口，使用同一份 Excel 会话。
- 在本地窗口里读取会话、测试连接、配置 Codex，以及查看最近请求。
- 保存 Codex 原配置，退出代理时按设置恢复。

模型能否使用、还有多少额度，取决于你的账号和 Excel 后端。代理不会解锁权限，也不会在请求失败后偷偷换账号或模型。

## 第一次使用

先准备好：

- Excel 桌面版及官方 ChatGPT 加载项，在加载项里完成登录。
- 已安装的 Codex。
- Python 3.11 或更新版本。Windows 的独立窗口还需要 WebView2 Runtime。

### Windows

把项目放在一个固定目录，在该目录打开 PowerShell，安装依赖：

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

已经能正常启动的环境可以跳过这一步。之后日常使用直接双击 **启动.vbs**。

打开窗口后：

1. 点 **读取 Excel 会话**，等页面显示“会话已就绪”。
2. 选择模型，按需点 **测试连接**。这会发送一条真实短请求，消耗少量额度。
3. 展开 **Codex 与启动设置**，点 **启用接入**。
4. 重启 Codex，开一个新对话开始使用。

已经接入过时，按钮会显示 **更新模型列表**。更新代理后可以点一次，再重启 Codex。

### macOS

在项目目录执行：

```bash
bash tools/install_macos.sh
./.venv/bin/python -B app/proxy.py
```

然后打开[本地控制面板](http://127.0.0.1:8000/)，按上面的步骤读取会话并接入 Codex。

## 启动、退出和恢复配置

Windows 下，重复双击 **启动.vbs** 会唤起已有窗口。最小化时代理继续运行，点窗口右上角的 **×** 才会退出。

启用 Codex 接入时会先备份原配置。默认开启 **关闭代理时恢复原配置**，下次启动代理再重新接入。也可以在面板中点 **恢复原配置**，然后重启 Codex。

更新源码后，要等当前任务结束，关闭旧代理再重新启动。只重复打开启动器，仍然用的是之前那个进程。

本地控制面板在 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)，API 地址是 `http://127.0.0.1:8000/v1`。服务只监听本机，并检查连接和浏览器来源。

## 模型和接口

当前模型列表：

- `gpt-6-astra-excel`
- `gpt-5.6-sol-excel`（默认）
- `gpt-5.6-terra-excel`
- `gpt-5.6-luna-excel`

模型出现在列表里，不代表当前账号一定有权限。可以先在面板测试。

主要接口是 `/v1/responses`，另有模型列表、上下文压缩、图片生成和图片编辑接口。请求需要携带完整对话历史；目前不支持仅靠 `previous_response_id` 续接、强制指定工具或结构化 JSON 输出。

看图支持内嵌 PNG、JPEG、GIF、WebP。每张最多 20 MiB，每个请求最多 20 张，内嵌图片合计最多 32 MiB，随历史一起发送的图片也计入。生图和图片编辑另有参数限制，详见[开发说明](docs/开发说明.md)。

## 请求记录和费用

首页的 **API 费用估算**按记录到的文本 token 和本地参考单价计算，用来了解大致用量。它不是实际账单，也不是 Excel 剩余额度；图片生成的费用不计入这里。

**最近请求**有单独的页面，可以查看时间、模型、耗时和结果。列表最多展示最近 100 条，这个显示限制不会删除历史记录。

需要排查提示词时，可以在设置里打开 **记录请求全文**，只对之后的请求生效，默认关闭。

## 遇到问题先看这里

| 情况 | 处理方法 |
| --- | --- |
| 找不到会话、会话过期或 401 | 在 Excel 的 ChatGPT 加载项里登录或刷新，再读取会话。 |
| 上游提示模型没有权限或返回 403 | 换一个当前 Excel 账号可以使用的模型。 |
| 本地返回 `local_access_required` | 从本机控制面板打开，不要通过其他网站或局域网地址调用。 |
| 429 | 上游限流，稍后再试。 |
| 工具转换失败或连接中断 | 先确认已重启新版代理和 Codex；仍有问题时，保留错误代码和对应请求时间。 |
| 双击启动失败 | 查看弹窗和 `%LOCALAPPDATA%\ghcp_proxy\ghcp-proxy.stderr.log`。 |

更多操作说明放在[使用说明](docs/使用说明.md)里。

## 数据保存在哪里

Windows 的设置在 `%APPDATA%\ghcp_proxy`，会话、日志和历史记录在 `%LOCALAPPDATA%\ghcp_proxy`。Windows 会话使用系统加密保存；macOS 的代理会话只留在内存。

点 **清除缓存**清除的是代理保存的登录会话，不会退出 Excel。Excel 仍然登录时，后续读取可以再次找到它。

为了让工具任务在重启后继续，程序还会保存工具调用参数，里面可能有文件名、命令和代码。这个历史库与“记录请求全文”开关是两回事。消息和图片会发送到 OpenAI 的 Excel 后端处理。

## 目录

```text
ghcp_proxy/
├── 启动.vbs          Windows 双击入口
├── app/              程序、页面和提示词
├── tests/            离线回归测试
├── tools/            安装、测试和诊断脚本
├── docs/             当前说明；旧记录在 archive/ 内
├── requirements.txt  运行依赖
└── .venv/            本项目的 Python 环境
```

运行离线回归测试：

```powershell
./.venv/Scripts/python.exe -B tools/test-proxy-contracts.py
```

macOS 使用 `./.venv/bin/python`。测试会隔离运行数据并阻止真实 HTTP 请求，不消耗模型额度。更详细的接口和维护说明见[开发说明](docs/开发说明.md)，以前的排查过程保存在[历史归档](docs/archive/README.md)。

## 参考与感谢

- [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api)：参考了 Excel / Basispoints 适配、工具历史校验和图片请求限制。
- [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge)：参考了本地桥接、工具转换、图片附件上传和本机访问保护。

感谢两个项目的作者和贡献者公开代码和测试，让这个项目少走了不少弯路。具体参考版本和本次改动记录在[参考项目与改进](docs/参考项目与改进.md)中。
