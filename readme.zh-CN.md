# ⚠️ 此方法已于 2026 年 10 月 1 日失效

下述通过 Excel/BPS 后端接入的方法已失效。仓库保留完整源码和安装说明，可用于本地安装、启动及研究；控制面板能打开不代表上游模型请求可用。

<div align="center">

<h1>Excel 连接服务</h1>

<p><strong>在 Codex 中使用 OpenAI Excel 后端。</strong></p>

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

服务负责将 Codex 请求转换后发往 Excel/BPS 后端，并流式返回回复和工具调用。文件读写、代码修改和命令执行仍由 Codex 完成。

```text
Codex → 本机 Excel 连接服务 → OpenAI Excel/BPS 后端
```

## 功能

| 功能 | 说明 |
| --- | --- |
| 对话 | 流式回复、工具调用、上下文压缩。 |
| 图片 | 看图、生成和编辑图片。 |
| 账号 | Windows 上支持直接登录、手动切换账号、凭据加密保存和令牌续期。 |
| 控制面板 | 查看连接设置、最近请求、账号额度和用量估算。 |
| 配置恢复 | 接入前备份 Codex 配置，关闭服务时可自动恢复。 |

> 本项目并非 OpenAI 或 Microsoft 官方集成。账号需有 Excel/BPS 后端及所选模型的访问权限，并受账号额度限制。

## 界面预览

![连接与设置页面：已保存账号与 Codex 接入状态](docs/images/connection.png)

截图使用示例账号。应用界面目前为中文。

## 快速开始

### Windows

**运行环境：**

- Python 3.11+ 和 Codex；使用 ZIP 下载时无需 Git。
- Edge 或 Chrome（用于登录），以及 WebView2 Runtime（用于桌面窗口）。
- 可访问 Excel/BPS 的 OpenAI 账号。直接登录无需安装或打开 Excel。

#### 1. 安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

也可[下载 ZIP](https://github.com/AuroraWhisperer/excel-proxy/archive/refs/heads/main.zip)，解压后在包含 **启动.vbs** 的文件夹打开 PowerShell，仅执行上面最后两条命令。仓库包含运行所需的源码和资源；`.venv/` 需在本机创建，`tmp/` 仅存放本地归档，不参与程序运行，也不随仓库下载。

#### 2. 打开程序

双击 **启动.vbs** 打开窗口：

以下连接步骤保留为原有流程说明，仍受顶部的 10 月 1 日失效提示约束。

1. 点击 **手动登录**，在官方授权窗口完成登录。
2. 程序会用一次简短模型请求验证账号，消耗少量额度。验证通过后，自动启用账号、备份并更新 Codex 配置。
3. 重启 Codex，开始新对话。

#### 3. 日常使用

选择账号，点击 **使用此账号** 即可切换。进行中的请求继续使用原账号。最小化窗口可保持服务运行，点击 **×** 则停止服务。

连接测试、Excel 会话导入和配置恢复在 **高级设置** 中，详见[登录说明](docs/direct-login.md)。

### macOS

<details>
<summary><strong>使用已有的 Excel 加载项会话</strong></summary>

安装 Git、Python 3.11+，在 OpenAI Excel 加载项中登录，然后执行：

```bash
git clone https://github.com/AuroraWhisperer/excel-proxy.git
cd excel-proxy
bash tools/install_macos.sh
./.venv/bin/python -B app/proxy.py
```

打开[本机控制面板](http://127.0.0.1:8000/)，在 **高级设置** 中点击 **读取 Excel 登录**，再点击 **启用接入** 配置 Codex。首次接入后请重启 Codex。

直接登录和凭据加密保存目前仅支持 Windows。

</details>

## 文档导航

完整文档见[文档索引](docs/README.md)。

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

感谢以下项目的作者和贡献者：

| 项目 | 主要参考内容 |
| --- | --- |
| [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) | Excel/Basispoints 协议适配、工具历史与整批校验、工具传输恢复、图片容量限制。 |
| [Kaixxrua/excel-codex-bridge](https://github.com/Kaixxrua/excel-codex-bridge) | 本机 Codex 与 Excel 的桥接、本地访问保护、客户端工具转换、附件上传与错误处理。 |

具体参考版本和适配范围见[参考项目与改进](docs/参考项目与改进.md)。

## 许可证

除另有说明外，本项目采用 [AGPL-3.0-only](LICENSE) 许可证。

已按 [Unlicense](https://github.com/AuroraWhisperer/excel-proxy/blob/3cf755eb9ed5c162e2f90ec2c950825112c548af/LICENSE) 发布的版本及内容保留原授权。第三方代码与依赖遵循各自的许可证和署名要求。
