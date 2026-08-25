# Claude Code 源码分析

> 来源：cli-project1-source-code | 语言：TypeScript | 运行时：Bun | UI：React + Ink

## 一、项目概述

Claude Code 是 Anthropic 官方的 CLI AI 编码助手。它是一个完整的 TypeScript 应用，使用 React + Ink 渲染终端 UI，通过 `@anthropic-ai/sdk` 调用 Claude API。

## 二、技术栈

| 层面 | 技术 |
|---|---|
| 语言 | TypeScript（含 .tsx React 组件） |
| 运行时/打包 | Bun（`bun:bundle` 宏、`MACRO.*` 内联常量） |
| UI | React + Ink（终端 React 渲染器） |
| SDK | `@anthropic-ai/sdk` |
| 关键库 | lodash-es、chalk、strip-ansi、zod、`@modelcontextprotocol/sdk` |

## 三、入口与启动

### 3.1 CLI 入口：`entrypoints/cli.tsx`

引导流程：
1. 检查特殊标志（`--version`、`--dump-system-prompt`、MCP 服务器等）
2. 动态导入其余模块（加快启动速度）

### 3.2 设置：`setup.ts`

处理工作目录、Git 上下文、权限配置、会话管理。

### 3.3 主 UI：`main.tsx`（~803KB）

巨大的主应用渲染文件，包含主要的 UI 布局和组件组合。

## 四、核心架构：QueryEngine + 工具系统

### 4.1 代理循环：QueryEngine.ts + query.ts

这是 Claude Code 的核心循环：

```
用户输入 → 构建消息 → 调用 Claude API → 流式接收 → 收集 tool_use 块
  → runTools() 执行工具 → 工具结果反馈给模型 → 循环（直到模型不再请求工具）
```

**QueryEngine**（`QueryEngine.ts:184`）管理对话生命周期：
- `submitMessage()`：接受提示，处理用户输入（斜杠命令、附件）
- 构建 `processUserInputContext`（即 `ToolUseContext`）
- 调用 `query()` 进入主循环

**query()**（`query.ts:219`）递归循环：
1. 构建消息（系统提示 + 工具结果 + 历史）
2. 调用模型 API
3. 从流中消耗 `tool_use` 块
4. 执行工具（`runTools()` 或 `StreamingToolExecutor`）
5. 如果 `needsFollowUp` → 循环回到步骤 1，否则完成

### 4.2 工具编排：toolOrchestration.ts

`runTools()`（第 19-82 行）：
1. `partitionToolCalls()` 将工具调用分区：
   - **并发安全** 的工具分组并行执行
   - **非并发安全** 的工具各自独立顺序执行
2. 并发批次：`runToolsConcurrently()`
3. 顺序批次：`runToolsSerially()`，每个工具执行后应用上下文修改器

### 4.3 单工具执行生命周期：toolExecution.ts

`runToolUse()`（第 337-490 行）覆盖完整生命周期：

```
1. 工具查找（按名称，支持别名）
2. 中止检查
3. Zod 模式验证输入
4. 工具特定的 validateInput()
5. Bash 分类器投机启动（仅 Bash）
6. 输入反填（backfillObservableInput）
7. PreToolUse 钩子（允许修改输入/阻止执行）
8. 权限检查（hasPermissionsToUseTool）
9. 工具执行（tool.call()）
10. 结果映射（mapToolResultToToolResultBlockParam）
11. PostToolUse 钩子
12. 错误处理（分类错误类型，返回 is_error: true）
```

## 五、工具接口设计（Tool.ts）

### 5.1 Tool 类型（第 362-695 行）

一个泛型对象类型，每个工具必须/可以实现：

**必填字段：**
- `name` — 唯一标识符
- `inputSchema` — Zod 模式（运行时验证模型输入）
- `call(args, context, canUseTool, parentMessage, onProgress)` — 核心执行函数
- `description()` — 生成工具功能描述
- `prompt()` — 返回系统提示块，教模型如何使用工具
- `renderToolUseMessage()` — UI 渲染
- `mapToolResultToToolResultBlockParam()` — 结果序列化为 API 格式
- `userFacingName()` — UI 显示名称

**可选但关键：**
- `validateInput()` — 调用前的自定义验证
- `checkPermissions()` — 工具特定权限
- `isConcurrencySafe()` — 是否可并行
- `isReadOnly()` — 是否只读
- `isDestructive()` — 是否不可逆
- `getPath()` — 提取操作的路径（用于权限规则匹配）

### 5.2 ToolUseContext（第 158-300 行）

工具执行时收到的巨大上下文对象，包含：
- `options` — 命令、调试标志、工具列表、MCP 客户端
- `abortController` — 超时/取消
- `readFileState` — 文件状态缓存
- `getAppState()` / `setAppState()` — 全局应用状态
- `messages` — 当前对话消息
- `agentId` — 子代理上下文
- `fileReadingLimits` — 读取限制
- `toolDecisions` — 接受/拒绝跟踪

### 5.3 buildTool()（第 783-792 行）

接受部分 ToolDef，通过 `TOOL_DEFAULTS` 填充默认值（安全保守）：
- `isEnabled: true`
- `isConcurrencySafe: false`
- `isReadOnly: false`
- `isDestructive: false`
- `checkPermissions: { behavior: 'allow' }`

## 六、代表性工具深度分析

### 6.1 FileReadTool

**validateInput()** 做了大量预检查：
1. PDF pages 参数格式和范围
2. 路径展开 + 拒绝规则检查
3. UNC 路径安全检查（阻止 NTLM 凭据泄漏）
4. 二进制扩展检查
5. 设备路径检查（`/dev/zero` 会挂起）

**call()** 核心逻辑：
1. 去重检查：同一文件 + 相同参数 + mtime 未变 → 返回 `file_unchanged` 存根
2. 技能发现：匹配技能目录时触发后台加载
3. 按类型处理：Jupyter Notebook、图片（压缩/调整大小）、PDF、文本
4. 令牌预算验证（`countTokensWithAPI()`）
5. 更新 `readFileState`

**结果映射**：根据类型转换为不同格式（图片→base64 块、notebook→单元格等）

### 6.2 BashTool

**权限检查**：
1. 针对允许/拒绝/询问规则检查命令模式（如 `Bash(git *)`）
2. 自动模式下运行安全分类器
3. 支持 `_simulatedSedEdit` 用于 sed 权限流

**call()** 核心逻辑：
1. 通过 `runShellCommand()` 生成器运行，逐步产生进度
2. 输出累积到 `EndTruncatingAccumulator`
3. **语义结果解释**：`interpretCommandResult()` 分析退出码和输出，判断操作是否真的失败
4. 超大输出持久化到磁盘（供模型用 Read 工具读取）
5. 图片输出检测（base64 数据）

### 6.3 FileEditTool

**validateInput()** 做了最严格的预检查：
1. 团队记忆文件中的机密扫描
2. 拒绝规则匹配
3. UNC 路径检查
4. 文件大小检查（最大 1GB 防 OOM）
5. 检测并保留原始文件编码
6. 处理不存在的文件（`old_string=''` 时创建）
7. Jupyter Notebook 重定向
8. **过期检查**：验证文件自上次读取后被修改 → 拒绝编辑
9. 查找 `old_string`（处理引号变体）
10. 多个匹配检查（需要 `replace_all: true`）
11. 设置文件保护

**call()** 核心逻辑：
1. LSP 诊断跟踪
2. 文件历史备份
3. **二次过期检查**（编辑前重新读取验证 mtime）
4. 生成补丁 + 写入磁盘
5. LSP 通知（`didChange`、`didSave`）
6. VS Code 通知
7. 更新 `readFileState`
8. Git diff 计算

## 七、权限系统

### 7.1 主入口：hasPermissionsToUseTool

`permissions.ts:473`，检查链：

```
1a. 整个工具被拒绝？（工具级拒绝规则）
1b. 整个工具标记为询问？
1c. 工具特定的 checkPermissions（每个工具自定义）
1d. 工具特定拒绝？
1e. 需要用户交互？
1f. 内容特定的询问规则？
1g. 安全检查（.git/.claude/.vscode 等路径免绕过）
2a. bypassPermissions 模式？
2b. 自动模式安全分类器
2c. 拒绝跟踪（连续拒绝过多 → 回退到完整提示）
3.  PermissionRequest 钩子（无头代理）
4.  默认允许
```

### 7.2 权限规则

支持规则模式：
- `Bash(git *)` — 允许所有 git 命令
- `Bash(npm publish:*)` — 询问 npm publish
- 路径模式：`.git/config` — 拒绝

### 7.3 文件权限

`filesystem.ts` — 检查受保护路径、危险路径（`.gitconfig`、`.bashrc`）、危险目录（`.git`、`.vscode`、`.claude`）。

## 八、上下文与提示管理（context.ts）

### 8.1 getSystemContext()

收集并注入到系统提示的内容：
- Git 状态、分支
- 当前日期
- 平台信息
- 缓存破坏令牌

### 8.2 getUserContext()

收集用户级上下文：
- CLAUDE.md 文件内容（向上遍历目录）
- MEMORY.md 内容
- 用户规则

### 8.3 设计特点

- **代码构建**而非字符串模板
- 上下文是可组合的部分
- 缓存友好的确定性排序

## 九、工具注册（tools.ts）

### 9.1 三层注册

1. **静态导入**：所有内置工具模块
2. **条件导入**：`USER_TYPE === 'ant'` / `feature('PROACTIVE')` 等标志
3. **getAllBaseTools()**：返回所有可能工具的数组（含条件逻辑）

### 9.2 getTools() 生产过滤

1. 简单模式 → 仅 BashTool + FileReadTool + FileEditTool
2. 过滤拒绝规则匹配的工具
3. 按 `isEnabled()` 过滤
4. 排序（保证提示缓存稳定性）

### 9.3 工具池组装

`assembleToolPool()`：内置工具 + MCP 工具，按名称去重（内置优先）。

## 十、状态管理

React 上下文 + 选择器模式：
- `state/AppState.tsx` — 主状态容器
- `state/store.ts` — 存储实现
- `state/selectors.ts` — 选择器
- 工具通过 `getAppState()` / `setAppState()` 读写状态

## 十一、总结：Claude Code 的优缺点

### 值得借鉴

1. **精致的工具接口** — Tool 类型设计了完整的生命周期（验证→权限→执行→结果映射→渲染）
2. **工具并发编排** — isConcurrencySafe 分区，充分用并行
3. **文件过期检测** — FileEdit 执行前二次验证文件未被外部修改
4. **三级权限系统** — 规则 + 分类器 + 交互式，安全且灵活
5. **代码构建的上下文** — 比字符串模板更可组合
6. **缓存友好** — 工具列表排序、确定性提示构建

### 不足之处

1. **硬编码的线性循环** — QueryEngine 的 while 循环不支持用户自定义流程
2. **无图编排** — 不能插入预处理/后处理节点、不能条件分支到不同的处理路径
3. **单模型绑定** — 深度绑定 Anthropic API
4. **巨型单文件** — main.tsx 803KB，query.ts 69KB
5. **工具数爆炸** — 40+ 工具，很多是 CLI 特有的（AgentTool 里嵌套工具系统）
