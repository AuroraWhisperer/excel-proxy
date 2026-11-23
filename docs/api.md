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

桌面服务通过 `wss://bps.openai.com/basispoints/api/responses` 发送 `response.create`。流式客户端收到 SSE 事件，非流式客户端收到汇总后的 JSON。面板的模型连接测试也经过这条 WebSocket。附件上传、图片生成与编辑使用各自的 HTTPS 接口。

`GET /healthz` 仅检查本机已选凭据和生成队列。`credential` 为 `available` 或 `renewable` 时返回 200，`missing`、`expired`、`unreadable` 返回 503。`renewable` 表示本机有续期材料。检查只读取状态，模型实际权限请用面板的连接测试验证。`generation_queue` 显示正在占用槽位的请求数 `active`、等待槽位的请求数 `pending` 和当前限额。

控制面板路径为 `/ui`（连接与设置）、`/ui/requests`（最近请求）和 `/ui/usage`（用量与费用）；根路径会跳转到连接页。

## 模型

| 模型 ID | 推理级别 |
| --- | --- |
| `gpt-6-astra-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-6-sol-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-6-luna-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-5.6-sol-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-5.6-terra-excel` | `low`、`medium`、`high`、`xhigh` |
| `gpt-5.6-luna-excel` | `low`、`medium`、`high`、`xhigh` |

Responses 请求接受省略 `-excel` 的别名。省略模型时使用 `gpt-5.6-sol-excel`；未知模型返回 400。模型是否真正可用仍由账号权限决定，列入本地目录不代表账号已经获得访问权限。

## Responses 兼容范围

- **对话历史：** `input` 必须是字符串或全部由对象组成的数组；缺失、`null`、其他类型及数组中的非对象返回 400。每次请求需携带完整历史，`previous_response_id`、未展开的 `item_reference`、未在本次历史中找到对应先前调用的工具结果均返回 400。
- **工具执行：** 支持客户端声明的 function、custom 和 namespace 工具，通过 `run_officejs` 传输。默认协议要求一次调用一个工具，`update_plan` 也使用这条路径；文件操作和命令执行由客户端完成。
- **托管工具与选择：** 上游托管的搜索、生图、代码解释器等声明不会作为客户端工具开放。顶层 `tools` 为数组时，指向已移除工具，或指向已识别的搜索、生图、`code_interpreter`、`file_search`、`computer_use_preview` 的强制选择改为 `auto`；其他强制工具选择返回 400。`tool_choice="none"` 不提供工具目录。
- **重复工具声明：** 同一批次的同名工具在类型、参数 schema、custom 格式和 `strict` 相同时保留第一份，描述和展示字段不参与冲突判断；调用约束冲突时，在请求上游前返回 400，错误参数为 `tools`。
- **历史配置：** 支持顶层历史条目 `additional_tools` 与 `configuration_update`。后续工具声明更新本轮目录中的同名定义；旧调用直接依据客户端提交的完整参数重建，不依赖旧目录或持久缓存。历史推理配置支持 `reasoning.effort` 和可选的 `mode: "standard"`，当前请求显式指定的强度优先。
- **协作历史：** 消息保留 `author`、`recipient`、原角色和 `agent_message` 类型，文本与图片顺序保持不变；不再转换成额外的用户消息。内部字段 `internal_chat_message_metadata_passthrough` 不转发。
- **内容规范化：** 内容数组中的字符串转为文本段，支持的图片写法统一为 `input_image`。音频、文件、视频及其他不支持的内容类型返回 400；非法内容和孤立工具结果会在附件上传与生成前被拒绝。
- **整批校验：** 一批工具必须全部通过校验才会交给客户端；未知工具、重复 ID 或未完成的调用不会先执行其中的有效部分。设置 `parallel_tool_calls=false` 时，多个调用会被拒绝。
- **流式回复：** 等待时每 15 秒保活。上游返回普通 JSON 时，会转换为 Responses 事件并向客户端保持 `text/event-stream`；未完成的输出或失败不会作为成功返回。
- **请求失败：** 不会静默切换账号或模型，开始生成后的传输错误不会自动重放整个请求。同账号的认证续期规则见[直接登录与多账号](direct-login.md#凭据与续期)。
- **推理与压缩：** 上游仅发送 `reasoning_effort`，推理摘要按上游实际返回内容转发。`none`、`minimal` 映射为 `low`，`max`、`ultra` 映射为 `xhigh`；未知强度或非 `standard` 推理模式返回 400。上下文压缩失败时保留原历史。

调用方显式提供的 `instructions` 保留为独立的开发者消息，随后只添加一条工具协议消息，再接完整历史。工具结果的空字符串和错误文本保持原样。已有原生压缩条目及它之前的历史会保留，不以本地推断裁剪。

缓存路由只使用显式的顶层 `prompt_cache_key`，保留其中的空白并与调用作用域一起哈希；未提供时不从请求体的 `session_id`、别名或 `client_metadata` 合成缓存键。请求模型显式发送到上游；默认推理强度为 `medium`，未提供 `context_management` 数组时使用 200,000 token 的压缩阈值。

### 结构化输出

`text.format` 的 `json_object` 和 `json_schema` 通过同一条协议消息末尾的指令与本地校验兼容，可用于 Codex 的标题、描述和回合摘要。最终回答必须恰好是一个 JSON 值，不带 Markdown 围栏或额外文字；`json_object` 仍要求对象，`json_schema` 可按 schema 接受数组或其他 JSON 类型。格式对象缺失有效 `type` 时返回 400。这不是上游原生约束解码；Schema 只允许文档内引用。

流式结构化消息在完成并通过校验后统一发出，不回放未经校验的文本增量；普通文本和推理摘要仍增量转发。JSON 或 Schema 不匹配返回 `excel_invalid_structured_output`，不额外请求模型重试或编造结果。

工具封包、参数校验、纠错预算和历史重建的实现细节见[开发说明](开发说明.md#工具协议与请求生命周期)。

### 并发与 429

默认每个账号最多同时占用 4 个生成槽位，进程合计最多 64 个请求等待槽位。文本、压缩、生图和编辑共用这组限额。每次生成 POST 的本地等待预算为 30 秒，包含等待槽位和启动限速的时间。已经发出的生成使用其传输超时。前置附件上传、登录和余额查询使用各自的路径。

| HTTP 429 错误码 | 含义 | 客户端处理 |
| --- | --- | --- |
| `excel_queue_full` | 本地等待队列已满 | 等活动请求结束后重试，响应带 `Retry-After: 1`。 |
| `excel_queue_timeout` | 本次生成 POST 的启动等待预算耗尽 | 降低同时发起的请求数，响应带 `Retry-After: 1`。 |
| `excel_rate_limited` | 上游拒绝本次请求 | 按有效 `Retry-After` 等待。秒数和带时区的 HTTP 日期统一换算为 0–604800 秒，无效值省略。 |

## 图片输入

通过 Responses 看图时，接受内嵌的 base64 图片、绝对 HTTPS 图片 URL 和显式 `file_id`。支持 `input_image`、`image_url`、带 `source` 的图片和 MCP `mimeType`／`data` 图片写法；URL 不得包含内嵌凭据，`detail` 只能是 `auto`、`low`、`high`。内嵌图片限制如下：

| 项目 | 限制 |
| --- | --- |
| 格式 | PNG、JPEG、GIF、WebP |
| 单张大小 | 最多 20 MiB |
| 每次请求数量 | 最多 20 张 |
| 解码后合计大小 | 最多 32 MiB |

消息、工具结果和对话历史中一同提交的图片都计入数量限制，内嵌图片另计入大小限制。内容类型、图片引用、格式、编码、数量和大小检查全部通过后，才上传消息中的内嵌附件；这些检查不包含远程图片下载、图片尺寸或像素内容解码。格式或大小不合适会明确报错，不会丢弃图片后继续回答。

工具结果中的内嵌截图、`file_id` 和 HTTPS URL 在规范化后留在原结果中，保持文本与图片顺序，不拆成额外消息。

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
