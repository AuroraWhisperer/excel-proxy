# API 与兼容范围

[项目首页](../readme.zh-CN.md) · [English](../readme.md) · [文档目录](README.md)

本文面向需要了解接口行为或接入其他本机客户端的读者。使用 Codex 时，通常由控制面板自动配置，无需手动调用接口。

## 地址与接口

API 基础地址为 `http://127.0.0.1:8000/v1`。服务只监听本机，并检查连接来源、Host 和浏览器来源；浏览器请求必须同源，本机原生客户端可以不带 Origin。

| 接口 | 用途 |
| --- | --- |
| `GET /v1/models` | 获取本地模型列表，不要求先登录。 |
| `POST /v1/responses` | 文本、看图、流式回复及客户端工具调用。 |
| `POST /v1/responses/compact` | 生成供下一轮继续使用的上下文摘要。 |
| `POST /v1/images/generations` | 生成图片。 |
| `POST /v1/images/edits` | 编辑图片。 |

上述接口也支持去掉 `/v1` 的路径。POST 请求接收 JSON 对象；数组、数字、字符串、`null` 或损坏的 JSON 返回 400。图片编辑也使用 JSON，不接受客户端直接提交 multipart。

控制面板路径为 `/ui`（连接与设置）、`/ui/requests`（最近请求）和 `/ui/usage`（用量与费用）；根路径会跳转到连接页。

## 模型

| 模型 ID | 推理级别 |
| --- | --- |
| `gpt-6-astra-excel` | `medium`、`high`、`xhigh` |
| `gpt-5.6-sol-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-5.6-terra-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-5.6-luna-excel` | `low`、`medium`、`high`、`xhigh` |

Responses 请求接受省略 `-excel` 的别名。省略模型时使用 `gpt-5.6-sol-excel`；未知模型返回 400。模型是否真正可用仍由账号权限决定，列入本地目录不代表账号已经获得访问权限。

## Responses 兼容范围

- **对话历史：** 每次请求需携带完整历史；`previous_response_id` 和强制工具选择目前返回 400。
- **工具执行：** 支持客户端声明的 function、custom 和 namespace 工具。本地服务负责转换调用，文件操作和命令执行由客户端完成。
- **整批校验：** 一批工具必须全部通过校验才会交给客户端；未知工具、重复 ID 或未完成的调用不会先执行其中的有效部分。设置 `parallel_tool_calls=false` 时，多个调用会被拒绝。
- **流式回复：** 等待时每 15 秒保活。上游返回普通 JSON 时，会转换为 Responses 事件并向客户端保持 `text/event-stream`；未完成的输出或失败不会作为成功返回。
- **请求失败：** 不会静默切换账号或模型，开始生成后的传输错误不会自动重放整个请求。同账号的认证续期规则见[直接登录与多账号](direct-login.md#凭据与续期)。
- **推理与压缩：** 推理摘要请求统一映射为 Excel 支持的 `auto`，没有上游摘要时不编造；上下文压缩失败时保留原历史。

### 结构化输出

`text.format` 的 `json_object` 和 `json_schema` 通过开发者指令与本地校验兼容，可用于 Codex 的标题、描述和回合摘要。这不是上游原生约束解码；Schema 只允许文档内引用。

流式结构化消息在完成并通过校验后统一发出，不回放未经校验的文本增量；普通文本和推理摘要仍增量转发。JSON 或 Schema 不匹配返回 `excel_invalid_structured_output`，不额外请求模型重试或编造结果。

工具封包、参数校验、纠错预算和历史重建的实现细节见[开发说明](开发说明.md#工具协议与请求生命周期)。

## 图片输入

通过 Responses 看图时，接受内嵌的 base64 图片：

| 项目 | 限制 |
| --- | --- |
| 格式 | PNG、JPEG、GIF、WebP |
| 单张大小 | 最多 20 MiB |
| 每次请求数量 | 最多 20 张 |
| 解码后合计大小 | 最多 32 MiB |

消息、工具结果和对话历史中一同提交的图片都计入限制。格式、编码、数量和大小检查全部通过后，才上传消息附件；这些检查不包含图片尺寸或像素内容解码。格式或大小不合适会明确报错，不会丢弃图片后继续回答。

## 图片生成与编辑

生成和编辑均使用 `gpt-image-2`，返回 PNG 的 `data[].b64_json`。

| 字段 | 支持值 |
| --- | --- |
| `prompt` | 必填 |
| `n` | 1–3，默认 1 |
| `size` | `auto`、`1024x1024`、`1536x1024`、`1024x1536`、`1280x720` |
| `quality` | `auto`、`low`、`medium`、`high` |
| `background` | `auto`、`opaque` |

编辑使用 `images: [{"image_url": "data:image/png;base64,..."}]`，接受 1–3 张 PNG、JPEG 或 WebP，每张最多 20 MiB。本地服务转换成上游需要的 multipart，不自行下载远程图片。

不支持客户端直接提交 multipart、遮罩、透明背景、图片流式输出或非 PNG 输出。生图和编辑可能消耗账号额度，面板的文本 token 费用估算不包含图片费用。

托管 Codex 配置里的 `x-openai-actor-authorization = "excel-proxy"` 是图片工具接入标记，不是登录凭据。实际认证使用用户明确选中的直接登录账号，或高级设置中显式选择的 Excel 缓存会话。

## 遇到错误

登录失效、模型权限、限流和启动失败的处理见[使用说明中的常见问题](使用说明.md#常见问题)。本地数据与请求日志的范围见[隐私与本地数据](privacy.md)。
