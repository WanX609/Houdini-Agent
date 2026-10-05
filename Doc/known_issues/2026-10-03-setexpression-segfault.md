# Houdini 22 GUI：setExpression 导致段错误

## 问题与现场证据

在 Houdini 22.0.368（windows-x86_64，FX）的桌面进程中，Agent 执行 AI
生成的 Python 代码时，`hou.Parm.setExpression()` 会触发 C++ 层段错误。
Python 的 `try/except` 无法捕获 signal 11；必须避免进入该 API。

本机两次崩溃的日志位于 `%LOCALAPPDATA%\Temp\houdini_temp\`：

- `crash.modular_pipes_4k.fbx.WanX_31208_log.txt`：00:16，uptime 33862 s。
- `crash.modular_pipes_4k.fbx.WanX_4196_log.txt`：00:23，uptime 415 s。

两份日志的关键栈相同：

```text
Caught signal 11
+0x33611e66 [HOMF_Parm::setExpression] D:\houdini_22\bin\libHOMF.dll
  ← _hou.pyd ← PyRun_FileExFlags
```

对应现场 `.hip` 同目录保留；Windows CrashDumps 为空，WER 无 Houdini 条目。
此处仅记录日志文件名和关键栈，不收录完整本机日志。

## 已实测结论

以下结论来自本机 `D:\houdini_22\bin\hython.exe`（22.0.368）的交接验证：

- 无头 hython 中 `setExpression` 正常工作；模拟 bridge 的 `sys.settrace`、
  StringIO 重定向和 `exec` 环境也不崩。多关键帧参数只抛出干净的
  `hou.OperationFailed`（`Parameter must have exactly one keyframe`）。
  现场崩溃属于 GUI 桌面进程的 libHOMF 路径，无头测试无法复现崩溃本身。
- **Houdini 22 不存在 `opparm -e`**：帮助无此 flag，实测报
  `Can't find node -e`。该方案作废，禁止使用。
- `hou.undos.beginGroup` 在 22.0 Python API 不存在；bridge 已用
  `try/except` 包住，与本次段错误无关。

## 已验证的 Hscript 替代方案

使用 `opscript` 重建表达式通道时采用的命令：

```hscript
chadd -t 0 0 /obj/node tx
chkey -t 0 -v 0 -V 0 -m 0 -M 0 -a 0 -A 0 -F 'ch("../ref/tx") + 1' /obj/node/tx
```

实测与 `setExpression` 的通道状态一致：关键帧数为 1、`parm.expression()`
相同、`parm.eval()` 相同。`chadd -t 0 0` 的零长度段是关键，表达式全局生效。

- 节点和通道必须使用绝对路径，避免 hscript 当前目录影响解析。
- `chkey -F` 的表达式用单引号包裹；表达式含单引号时直接拒绝，不尝试转义。
- 已有多个关键帧时先执行 `chrm /obj/node/tx`。多 key 通道上
  `parm.expression()` 的报错是读取 API 的前置条件，不代表 `chkey` 失败。
- `chadd` 在已有通道上出现 `exist/already` 类错误可以忽略；`chkey` 必须带
  `-t` 或 `-f`，此实现使用 `-t 0`。
- 此方案仅支持 **Hscript 语言表达式**，不能写入 Python 语言通道表达式。

## Agent 的修复行为

新增专用 `set_parameter_expression` 工具，普通参数值仍使用 `set_node_parameter`。
模型通过工具 schema 获取用法，不依赖额外的 system prompt 禁令，也不向
`execute_python` 的执行环境注入表达式助手。

```json
{
  "node_path": "/obj/x",
  "param_name": "tx",
  "expression": "ch(\"../y/tx\")",
  "language": "Hscript"
}
```

该工具仅处理单个 Float/Int 参数的 Hscript 表达式，内部调用
`set_expression_safe`，使用上述已验证通道命令，不调用 `hou.Parm.setExpression`。
返回序列化撤销快照，并接入确认模式、场景修改/cook 保护及两套 UI 的撤销记录。
从无动画数值改为表达式时，撤销会清除新通道，再恢复原值。

为避免覆盖无法完整恢复的动画，工具拒绝多关键帧、非零时间关键帧、数值动画
关键帧及已有 Python 表达式。含单引号的表达式也拒绝；旧表达式含单引号时
同样拒绝覆盖，以保证撤销可恢复。设置失败时尝试安全回滚，回滚失败一并返回错误。
底层助手原有的多 key 清除行为不变，但专用工具不暴露它来覆盖用户动画。

`execute_python` 默认扫描并拒绝 `.setExpression(` 调用（允许中间空白），
返回专用工具用法。外部 MCP 的 `execute_python_code` 同样拦截。
检查基于代码文本，注释或字符串中的匹配也会被拒绝；不是动态属性访问的完整沙箱。

退出机制保留：`HOUDINI_AGENT_SETEXPRESSION_GUARD` 默认开启，设置为
`0`、`false`、`off`、`no`（忽略大小写和首尾空白）可关闭拦截。
该开关不会改变专用工具的安全实现或撤销恢复路径。

四处既有撤销/恢复路径的 Hscript 表达式使用安全助手。Python 表达式的用户手动
恢复保守保留原 API，并注明已知 GUI 崩溃风险；专用工具拒绝设置和覆盖该语言，
不提供未经验证的 Python 替代实现。

MCP 注册同时修复一处将节点网络工具尾部错误缩进到注册阶段的问题，并用
`functools.wraps` 保留包装后的工具签名，确保新工具能完成注册和生成参数信息。

## 验证及版本范围

项目 README 声明支持 Houdini 20.5+。本次实际可运行的版本如下：

| 版本 | 数值通道/专用工具无头检查 | GUI 回归 | 说明 |
| --- | --- | --- | --- |
| 22.0.368 | 19 项通过 | 待手动验证 | 本次崩溃报告版本 |
| 18.5.532 | 19 项通过 | 未验证 | 在项目声明支持范围之外，仅作补充证据 |
| 20.5.x | 未验证 | 未验证 | 本机没有可运行安装 |
| 21.x | 未验证 | 未验证 | 本机没有可运行安装 |

无头检查验证 Float/Int 通道、关键帧数、表达式文本、跨帧求值、引用更新、
值/表达式撤销及拒绝输入。它不复现 GUI 段错误，也不证明未测试版本兼容。
不声称全版本或所有参数类型兼容。

离线测试使用 mock hou，覆盖通道命令、拦截与开关、工具分派、注册签名、
输入拒绝、快照/回滚、只读模式限制及确认模式元数据。运行：

```text
python -m pytest tests/test_safe_expr.py tests/test_parameter_expression.py -v
python -m pytest
```

修改的 Python 文件和运行副本分别使用 `python -W error -m py_compile` 检查。
同步后需要重启 Agent 和 Houdini，或重新加载桥接代码，才能让已加载的模块生效。
