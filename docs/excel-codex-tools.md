# 通过 Basis Points 使用 Codex 工具

Excel 模型仍使用现有会话认证，请求 `https://bps.openai.com/basispoints/api/responses`。
无需启用 Direct 开关：此前的纯聊天方案已撤销，`GHCP_EXCEL_DIRECT` 和
`GHCP_EXCEL_DIRECT_KEEP_INSTRUCTIONS` 不再被代码读取。

本次修改去掉 GHCP 工具协议提示词中的 Excel / 工作簿身份描述，改为通用的
Codex 客户端工具说明。保留调用方的 instructions、system/developer 消息、
工具目录、工具结果、加密 reasoning，以及流式和非流式响应中的工具转换。

## Codex 基础提示词与服务端前缀

Excel 模型目录的 `base_instructions` 现使用 `app/prompts/codex-excel.md`：它是用户
提供的完整 Codex 提示词，已把粘贴文本中的 JSON 转义换行还原，正文共 21,259
字符。此前 GHCP 为所有模型生成的基础提示词只有两句话，描述为 GPT-5 coding
agent。现在仅 Excel 路由使用完整版本，其他模型维持原值。

Codex 从模型目录取得这段基础提示词后，GHCP 将收到的顶层 `instructions`
完整转为 developer 消息，再添加工具协议；输入数组里 Codex 自己带来的
system/developer、环境和工作区消息仍然保留。代理不会再重复硬塞一份完整
提示词，也不会用文件内容覆盖调用方在请求里提供的自定义指令。

这不代表删除了 BPS 服务端的原生 Excel 提示词。参考项目
[excel-codex-bridge 的限制](https://github.com/Kaixxrua/excel-codex-bridge#限制)
明确提到每次请求仍有约 2.2 万 token 的服务端固定前缀；它的
[请求构造代码](https://github.com/Kaixxrua/excel-codex-bridge/blob/18c69d16122d5c7510d394cd47c2168fedf5bd4b/src/excel_codex_bridge/excel_upstream.py#L1664)
同样是附加 caller instructions 和工具协议，没有替换服务端提示词的实现。
它的
[模型目录](https://github.com/Kaixxrua/excel-codex-bridge/blob/18c69d16122d5c7510d394cd47c2168fedf5bd4b/src/excel_codex_bridge/codex_config.py#L23)
使用自编的简短 Codex 提示词，并非本次用户提供的全文。

`run_officejs` 仍是当前适配器的传输工具名称：GHCP 将其转换成实际的 Codex
工具调用，由客户端执行，再把结果按上游原始调用身份回传。删除这层协议
会破坏工具使用；本次修改没有尝试将任意 Codex tools schema 直接发送给 BPS。

图像生成或编辑需要客户端实际提供相应工具；代理能转发目录中的 function、
custom 及 namespace 工具，不会凭空增加图像服务，也不新增对 BPS 原生
`image_generation` 工具类型的支持。

正常退出旧代理后，从仓库目录启动修改后的代码：

```powershell
.\.venv\Scripts\python.exe .\proxy.py
```

继续使用原来的 Excel 模型别名，建议开启新会话，避免复用先前纯聊天方案或旧
提示词的会话状态。可用一个连续任务检查：先读取项目文件，再按要求修改，
运行检查，最后调用客户端提供的图像工具生成图片。只有真实完成这些操作才算
端到端验证通过，模型声称“可以使用工具”不算验证。

完整基础提示词需让 Codex 加载更新后的 GHCP 模型目录。使用新版代理重新启用
GHCP 的 Codex 代理配置即可生成目录，然后重启 Codex 并创建新会话。
已启用的配置可先正常关闭再重新启用以重新生成目录。
[Codex 配置文档](https://learn.chatgpt.com/docs/config-file/config-reference)
将 `model_catalog_json` 定义为启动时读取的模型目录路径。2026-09-25 的本机检查
显示主配置仍使用 `model_provider = "openai"`，且尚无 GHCP 模型目录；本次代码
修改不会自动把当前原生 Codex 会话切换到代理。

本地合成测试验证命令 → 自定义 patch → 命名空间图像工具的调用转换与结果重放，
以及工具迭代中 task/turn 标识和加密 reasoning 的保留；它不实际执行命令、
编辑项目或生成图片。BPS 服务端自己的提示词无法通过修改本地字符串保证移除。

2026-09-25 验证结果：44 项定向检查通过，语法及 diff 检查通过。另使用现有
会话做了真实 BPS 工具循环：三次请求均返回 HTTP 200，模型依次调用 `read_note`
读取临时文件、调用 `save_svg` 写入包含该文件随机标签和圆形的 SVG，收到成功
结果后回复 `DONE`。SVG 已解析验证，临时文件已自动清理。此项实测验证真实
上游连续调用客户端工具及 SVG 绘图，没有验证 Codex 桌面端的图像生成插件。

接入完整 Codex 提示词后，49 项定向检查通过，并重新执行了真实 BPS 工具循环：
三次请求仍均为 HTTP 200，完成 `read_note` → `save_svg` → `DONE`。各轮请求体
中的 Codex 提示词均与文件正文相同，首轮上游报告 27,257 input tokens。
该计数不能用于断言服务端提示词被删除。正文 SHA-256 为
`7e5a369ac7a07aef29a92ea577cc55f3b53cd09e636e2904efe7139ae03d79f1`（不含末尾换行）。

## 长任务连续性修复

认证仍使用 GHCP 已保存的 Excel GPT 账号和会话；这些修改不替换当前 Codex
的登录账号，也不修改 token 获取流程。

- `/responses/compact` 现在返回 `response.compaction` 和可重放的 compaction
  项，保留用户指令。下一轮展开摘要并去除已压缩的旧工具历史。摘要请求关闭
  客户端工具；空摘要、未完成响应和认证失败不会被当成压缩成功。
- 流式和非流式响应从 `response.output_item.done` 补全最终事件遗漏的输出，
  保留 reasoning 和工具顺序。收到真正的完成事件后立即结束，不再等待可能
  损坏的 HTTP 尾部。提前 EOF、无法翻译的原生工具和只有 reasoning 的空完成
  会显式失败，避免让客户端误以为任务已完成。
- 未完成的工具调用不会提前交给客户端执行。非流式协议错误直接返回失败，
  不会在收到响应后自动重放模型请求；仅连接建立失败保留有限重试。
  流式错误交由客户端的有限重试机制处理，不会无限循环。
- 原始工具调用写入状态目录下的 `excel-native-calls.sqlite3`，内存仍最多保留
  512 条，磁盘按最近使用时间保留 60 天，写入时清理过期记录。这样较长历史或
  代理重启后的重放仍能恢复原始 ID 和参数。此文件包含工具参数及其中可能出现的
  代码，是本地重放状态，不是调试日志；不单独保存账号请求头或工具执行结果。
- 计算 turn ID 时忽略会变动的客户端附加字段，保留最新用户消息明确提供的
  turn ID，以区分新用户轮次和同一轮中的工具继续执行。

压缩的接口形状参考
[Responses compact 文档](https://developers.openai.com/api/reference/python/resources/beta/subresources/responses/methods/compact)。
本地摘要编码用于桥接，不等于 BPS 原生加密压缩，也不能保证摘要完全无损。

2026-09-25 本次验证：69 项定向检查通过，包含 600 条工具调用历史超过内存
上限后的精确重放、独立 Python 进程中的重放、真实 FastAPI 路由配合模拟上游的
工具调用 → 压缩 → 继续调用，以及断流、损坏尾部和重试耗尽。语法和 diff
检查通过，测试状态目录均已清理，没有重启用户正在运行的代理。

本次新增的实账号长任务测试命令被工具自动审批拒绝，返回 `blocked by policy`，
没有提供更具体理由，因此未执行。此前三请求实测不代表本次长任务回归通过；
用户已选择自行测试真实 Excel 账号。

测试时先正常退出旧 GHCP，再从本目录运行上述启动命令，沿用 Excel 模型和
连接方式，在新任务里执行一个包含读文件、改代码和运行检查的完整项目任务。
重点观察工具结果回来后是否继续，以及上下文压缩后能否接着完成剩余工作。
若再次停下，记录时间、模型名和最后显示的是正常最终回复还是报错/重连，
便于对照请求轨迹。无需提供 token。

模型主动认为任务完成、账号额度或会话失效、上游拒绝和用户取消仍可能结束
任务；代理不会把这些情况改成无限自动继续。
