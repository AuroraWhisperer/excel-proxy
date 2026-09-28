<div align="center">

<h1>Excel 连接服务</h1>

<p><strong>在 Codex 中使用 OpenAI Excel 后端。</strong><br>
本机服务，支持流式回复、工具调用、图片处理与账号管理。</p>

<p><a href="readme.md">English</a> · <strong>简体中文</strong></p>

<p>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.11 或更新版本">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-555555" alt="Windows 和 macOS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-3A7D44" alt="许可证：AGPL-3.0-only"></a>
</p>

<p>
  <a href="#快速开始">快速开始</a> ·
  <a href="#文档导航">文档导航</a> ·
  <a href="#致谢">致谢</a> ·
  <a href="#许可证">许可证</a> ·
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

## 界面预览

![连接与设置页面：已保存账号与 Codex 接入状态](docs/images/connection.png)

连接与设置页面，使用示例账号展示。应用界面目前为中文。

## 快速开始

### Windows

**准备条件：**

- Git、Python 3.11 或更新版本，以及 Codex。
- 用于登录的 Edge 或 Chrome，以及桌面窗口所需的 WebView2 Runtime。
- 一个可访问 Excel/BPS 的 OpenAI 账号。直接登录不需要安装或打开 Excel。

#### 1. 安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

#### 2. 登录

双击 **启动.vbs** 打开窗口，应用界面目前为中文：

1. 点击 **登录并连接**，在官方授权窗口完成登录。
2. 程序会发送一次简短模型请求验证账号，消耗少量额度。验证成功后启用账号、备份 Codex 原配置，并自动配置本地接入。
3. 重启 Codex，开启新对话即可使用。

#### 3. 日常使用

选择已保存的账号，点击 **使用此账号** 即可切换。已经开始的请求继续使用原账号。最小化窗口会保持服务运行；点击 **×** 关闭窗口会停止服务。

连接测试、Excel 会话导入和配置恢复位于 **高级设置**。账号管理的详细行为见[登录说明](docs/direct-login.md)。

### macOS

<details>
<summary><strong>使用已有的 Excel 加载项会话安装与连接</strong></summary>

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

## 文档导航

详细文档目前均为中文，可按下表选择，也可查看[文档索引](docs/README.md)。

| 文档 | 内容 |
| --- | --- |
| [使用说明](docs/使用说明.md) | 安装、更新、恢复配置与常见问题 |
| [直接登录与多账号](docs/direct-login.md) | 登录、账号切换、加密存储与令牌续期 |
| [账号额度](docs/account-balances.md) | 账号导入、额度显示与统计周期 |
| [API 与兼容范围](docs/api.md) | 接口、模型 ID、工具支持与图片限制 |
| [隐私与本地数据](docs/privacy.md) | 数据去向、凭据保存与请求日志 |
| [开发说明](docs/开发说明.md) | 模块职责、内部协议、测试与运行选项 |
| [参考项目与改进](docs/参考项目与改进.md) | 参考实现与具体改动说明 |

## 致谢

感谢以下项目的作者、维护者与贡献者。公开的实现与测试，为本项目的协议适配和功能完善提供了参考：

| 项目 | 主要参考内容 |
| --- | --- |
| [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) | Excel/Basispoints 协议适配、工具历史与整批校验、工具传输恢复、图片容量限制。 |
| [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge) | 本机 Codex 与 Excel 的桥接、本地访问保护、客户端工具转换、附件上传与错误处理。 |

[参考项目与改进](docs/参考项目与改进.md)记录了具体参考版本和各项适配的范围。

## 许可证

除另有说明外，本项目采用 **GNU Affero General Public License v3.0 only**（`AGPL-3.0-only`）。**允许商业使用。** 以下为条款摘要，完整条款以 [LICENSE](LICENSE) 为准。

- **复制与修改：** 分发时须保留版权、许可和免责声明，并附上许可证。分发修改版时须注明修改内容及日期，受协议覆盖的作品整体须继续按 AGPL-3.0-only 授权。
- **二进制分发：** 须按第 6 条允许的方式提供完整的对应源代码，包括协议要求的构建脚本与安装资料。
- **修改版联网服务：** 修改后通过网络供用户交互时，须按第 13 条显著提供免费获取该运行版本对应源代码的方式。
- **担保与责任：** 软件按现状提供；在适用法律允许的范围内，适用第 15–16 条的免责声明和责任限制。

此前已按 [Unlicense](https://github.com/AuroraWhisperer/excel-proxy/blob/3cf755eb9ed5c162e2f90ec2c950825112c548af/LICENSE) 发布的版本及内容保留原授权，本次变更不撤销既有授权。第三方代码与依赖仍遵循各自的许可证和署名要求。
