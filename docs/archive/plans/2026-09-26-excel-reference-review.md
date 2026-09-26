# Excel reference review implementation plan

**Goal:** 修复参考项目对照中发现的本地访问、工具重放和图片请求问题，整理通俗文档。

**Architecture:** 保留当前 Excel 单上游、FastAPI 服务和 Windows 启动入口。在请求入口统一检查本地来源；在工具重放时核对调用内容；在图片上传前检查完整请求并按图片隔离等待。

**Tech Stack:** Python 3.11+、FastAPI / ASGI、httpx、unittest。

## Constraints

- 保留开始时已有的未提交修改；任务前快照在 `.tmp/excel-review-20260926/baseline/`。
- 只使用仓库 `.venv` 和 `tools/test-proxy-contracts.py`，阻止真实 httpx 传输。
- 不改变用户运行中的服务、Excel 登录、Codex 配置和 AppData 数据。
- 文档使用中文 Markdown；旧记录归入 `docs/archive/`，不删除历史内容。
- 参考版本：sub2api `594cdf0d6027fe7097ef42fe029c22713b9cc989`；excel-codex-bridge `8a277dfcdbb647d2ef4d714e31b6a98260a63a79`。

## Task 1: 本地访问与请求格式

Files: `app/local_access.py`, `app/proxy.py`, `app/util.py`, `tests/test_local_access.py`, `tests/test_request_validation.py`, `tools/test-proxy-contracts.py`。

- [x] 添加 HTTP 回归：外部 Host、Origin、来源 IP 均返回 403；本地无 Origin 的客户端和同源控制面板正常访问；拒绝前不读取请求体、不执行接口。
- [x] 添加非对象 JSON（数组、字符串、数字、null）和损坏 JSON 回归，确认返回 400 且日志不包含请求片段。
- [x] 用纯 ASGI 中间件检查连接来源和 Host，允许控制面板同源 Origin，拒绝跨站来源。不包装流式响应迭代器。
- [x] 修正 JSON 对象验证和错误日志，已有 HTTP 集成测试改用真实回环 Host。
- [x] 运行 `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_local_access test_request_validation`。

## Task 2: 工具历史碰撞

Files: `app/excel_upstream.py`, `tests/test_excel_tool_compatibility.py`。

- [x] 回归同一 call_id 被不同参数、命名空间或 custom 输入复用的情况。
- [x] 用现有转换器还原缓存调用，比较完整工具名称和 JSON 参数 / 原始 custom 文本；不匹配时沿用现有历史重建路径。
- [x] 转换器增加内部 `remember=False` 参数，用于无副作用核对及整个批次校验；整批成功后才保存原生调用。
- [x] 验证格式不同但语义一致的 JSON 保留原生项，大整数和 custom 文本不被改写；无效批次不污染缓存。
- [x] 运行 `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_tool_compatibility test_excel_continuity test_excel_stream_recovery`。

## Task 3: 图片限制与并发

Files: `app/excel_images.py`, `tests/test_excel_images.py`, `tests/test_excel_request_compat.py`。

- [x] 回归未知 MIME、空或损坏 base64、单张 20 MiB、单请求 20 张 / 32 MiB 限制；包含无效后续图片时不先上传前面的图片。
- [x] 检查消息和工具结果中的内嵌图片；工具结果继续保留内嵌形式。
- [x] 不同图片可以独立上传，同账号同图仍只上传一次；等待被取消后不遗留锁。
- [x] 运行 `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_images test_excel_request_compat`。

## Task 4: 目录与文档验收

- [x] 首页改为简短入口，新增 `docs/使用说明.md`、`docs/开发说明.md`、`docs/参考项目与改进.md`。
- [x] 旧排查文档及实施计划移入 `docs/archive/`，添加目录索引，保留文件内容。
- [x] 文档按当前界面校准：读取会话、手动测试、Codex 接入、本月 API 费用估算、独立请求记录页。
- [x] 说明两个参考项目的链接、参考范围、致谢、已知能力边界和本次实际验证范围。
- [x] 运行完整离线回归、语法检查、Markdown 本地链接检查和 `git diff --check`。

初始验证：273 项离线回归通过。

最终验证：291 项离线回归通过；53 个 Python 文件语法解析通过；27 个当前文档本地链接有效；19 份旧文档归档后字节一致；git diff --check 通过。未发送真实模型请求。
