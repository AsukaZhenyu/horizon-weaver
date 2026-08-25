# LangGraph 源码分析

> 版本：1.2.2 | 语言：Python | 定位：有状态多参与者 LLM 应用编排框架

## 一、项目概述

LangGraph 是 LangChain 推出的低级编排框架，受 Google Pregel 算法和 Apache Beam 启发。它不是应用，而是一个 **框架**——让你用图的方式定义 LLM 应用的执行流程。

## 二、项目结构（Monorepo）

```
langgraph/
├── libs/
│   ├── langgraph/              # 核心框架
│   ├── prebuilt/               # 预构建代理（create_react_agent 等）
│   ├── checkpoint/             # 检查点基类
│   ├── checkpoint-sqlite/      # SQLite 检查点
│   ├── checkpoint-postgres/    # Postgres 检查点
│   ├── cli/                    # LangGraph CLI
│   ├── sdk-py/                 # Python SDK
│   └── sdk-js/                 # JavaScript SDK
└── examples/                   # 15+ 示例
```

## 三、核心概念：StateGraph + Pregel

### 3.1 两种 API 风格

LangGraph 提供两种 API：

1. **Graph API（构建器模式）**：
```python
graph = StateGraph(MyState)
graph.add_node("agent", agent_fn)
graph.add_node("tools", tool_node)
graph.add_edge("agent", "tools")
graph.add_edge("tools", "agent")
compiled = graph.compile()
```

2. **Functional API（装饰器模式）**：
```python
@entrypoint()
def my_workflow(input):
    result = task1(input)
    return task2(result)
```

### 3.2 核心抽象：StateGraph

`graph/state.py:130` — 泛型构建器类：

```python
class StateGraph(Generic[StateT, ContextT, InputT, OutputT])
```

**状态模式**：
- `state_schema`：TypedDict / Pydantic BaseModel / dataclass
- 每个字段可用 `Annotated[type, reducer]` 注解
- 无注解 → `LastValue`（最后的写入保留）
- `Annotated[list, add_messages]` → 追加
- 自定义 reducer → `BinaryOperatorAggregate`

**上下文模式**：
- 可选的 `context_schema`：提供不可变运行时上下文（用户 ID、数据库连接等）
- 通过 `runtime.context` 在节点内访问

### 3.3 StateGraph 方法

| 方法 | 说明 |
|---|---|
| `add_node(name, fn, *, retry_policy, timeout, ...)` | 注册节点 |
| `add_edge(source, target)` | 添加有向边 |
| `add_conditional_edges(source, route_fn, path_map)` | 条件分支 |
| `add_sequence([n1, n2, n3])` | 顺序添加多个节点 |
| `set_entry_point(name)` | 设置入口 |
| `set_finish_point(name)` | 设置出口 |
| `compile(*, checkpointer)` | 编译为可执行图 |

### 3.4 节点函数签名

`graph/_node.py:70` — `StateNode` 类型别名支持多种签名：

```python
# 最简形式
def node(state: MyState) -> dict:
    return {"key": value}

# 带配置
def node(state: MyState, config: RunnableConfig) -> dict:
    ...

# 带写入器（流式输出）
def node(state: MyState, *, writer: StreamWriter) -> dict:
    ...

# 带运行时（访问 context）
def node(state: MyState, *, runtime: ToolRuntime) -> dict:
    ...
```

**关键**：节点返回 `dict`（部分状态更新），由 reducer 负责合并。

## 四、Pregel 执行引擎

### 4.1 BSP 模型

`pregel/main.py:448` — Pregel 类实现了 **Bulk Synchronous Parallel** 模型。

每个步骤包含 3 个阶段：

```
┌─────────────────────────────────────────────────┐
│  步骤 N                                          │
│                                                  │
│  1. PLAN（计划）                                  │
│     确定哪些节点需要在此步骤执行                   │
│     └── 检查哪些通道发生了变化                     │
│     └── 哪些节点订阅了这些通道                     │
│                                                  │
│  2. EXECUTE（执行）                               │
│     并行运行所有计划中的节点                       │
│     └── 每个节点读取当前状态                       │
│     └── 每个节点返回部分更新                       │
│     └── 更新对当前步骤不可见（隔离）               │
│                                                  │
│  3. UPDATE（更新）                                │
│     将所有写入合并到通道                          │
│     └── 保存检查点                                │
│     └── 检查中断条件                              │
└─────────────────────────────────────────────────┘
```

### 4.2 PregelLoop：核心循环

`pregel/_loop.py:156` — 管理每次图运行的状态：

```
__enter__ → _first() → [tick() → runner.tick() → after_tick()] × N → __exit__
```

**`_first()`**（第 836 行）：
- 从检查点加载状态
- 处理输入：映射到通道写入
- 设置 `versions_seen`（哪个节点看到了哪个通道版本）

**`tick()`**（第 592 行）：
- 调用 `prepare_next_tasks()` 确定要运行的节点
- 检查中断条件

**`after_tick()`**（第 676 行）：
- `apply_writes()` 将所有任务写入提交到通道
- 保存检查点
- 检查中断

### 4.3 计划引擎：prepare_next_tasks()

`pregel/_algo.py:392` — 决定哪些节点运行：

1. 检查 `TASKS` 通道的 `Send` 对象（动态扇出）
2. 检查哪些通道在此步骤中更新
3. 用 `trigger_to_nodes` 映射找订阅了这些通道的节点
4. 对每个候选节点调用 `prepare_single_task()`：
   - 检查节点是否看到了最新版本
   - 如果没看到 → 节点需要运行
   - 如果看到了 → 跳过

### 4.4 入口点：Pregel.stream()

`pregel/main.py:2626` — 主要入口：

```python
while loop.tick():              # Plan
    for _ in runner.tick(...):  # Execute
        yield from _output(...) # Stream
    loop.after_tick()           # Update
```

## 五、条件边（Branch）

### 5.1 BranchSpec

`graph/_branch.py:83`：

```python
class BranchSpec(NamedTuple):
    path: Runnable[Any, Hashable | list[Hashable]]   # 路由函数
    ends: dict[Hashable, str] | None                  # 返回值→节点名
```

### 5.2 路由函数签名

```python
def router(state: MyState) -> Literal["node_a", "node_b", "__end__"]:
    if condition:
        return "node_a"
    return "node_b"
```

### 5.3 `_route()` 方法

1. 从通道读取当前状态
2. 调用路由函数
3. 返回值通过 `ends` 映射到目标节点名
4. 写入 `branch:to:target_node` 通道触发目标节点执行

### 5.4 典型用法

```python
graph.add_conditional_edges(
    "agent",
    lambda s: "tools" if s["messages"][-1].tool_calls else "__end__",
    {"tools": "tools", "__end__": END}
)
```

## 六、检查点系统

### 6.1 Checkpoint 数据结构

`checkpoint/base/__init__.py:92`：

```python
class Checkpoint(TypedDict):
    v: int                          # 格式版本
    id: str                         # 唯一 ID（单调递增）
    ts: str                         # ISO 8601 时间戳
    channel_values: dict[str, Any]  # 每个通道的当前值
    channel_versions: ChannelVersions # 每个通道的版本号
    versions_seen: dict[str, ChannelVersions]  # 每个节点看到了哪些版本
```

**`versions_seen` 是计划的关键**：引擎通过比较通道版本与节点此前看到的版本，知道自上次运行以来哪些通道发生了变化。

### 6.2 检查点创建

`pregel/_checkpoint.py:61` — `create_checkpoint()`：
- 从上一个检查点 + 实时通道状态构建新检查点
- 支持 `_DeltaSnapshot` 增量通道

### 6.3 检查点恢复

`_loop.py:_first()`：当给定显式 `checkpoint_id`（时间旅行）或恢复中断时：
1. 从 `versions_seen` 确定 `INTERRUPT` 节点有哪些新通道版本
2. 恢复时输入被映射到通道写入
3. `prepare_next_tasks()` 安排节点重新执行

### 6.4 使用方式

```python
graph = graph.compile(checkpointer=MemorySaver())

# 每个步骤自动保存
for chunk in graph.stream(input, config):
    ...

# 获取状态
state = graph.get_state(config)

# 手动更新状态
graph.update_state(config, {"key": value})

# 从检查点恢复
for chunk in graph.stream(None, config):
    ...
```

## 七、ToolNode：预构建工具执行节点

### 7.1 ToolNode 类

`prebuilt/tool_node.py:622`：

```python
class ToolNode(RunnableCallable):
    def __init__(self, tools, *, name="tools",
                 handle_tool_errors=..., messages_key="messages"):
```

### 7.2 输入解析

`_parse_input()` 处理三种格式：
1. 字典状态（含 `messages` 键） → 从最后 `AIMessage` 提取 `tool_calls`
2. 消息列表 → 同上
3. 原始 ToolCall 字典 → 直接使用

### 7.3 工具执行

`_func()` / `_afunc()`：
1. 解析工具调用
2. 为每个调用构建 `ToolRuntime` 实例
3. 用 `executor.map()` 或 `asyncio.gather()` 并行调用
4. 合并输出

### 7.4 参数注入

`_inject_tool_args()` — 将图运行时数据注入工具调用，无需模型控制：
- `InjectedState(field)` → 从图状态提取特定字段
- `InjectedStore()` → 注入 BaseStore
- `ToolRuntime` → 注入完整运行时上下文
- 系统注入的参数从模型的工具 schema 中自动排除

### 7.5 中间件模式

`wrap_tool_call` / `awrap_tool_call`：
```python
def my_middleware(request: ToolCallRequest, execute):
    # 可以在调用前修改 request
    result = execute(request)
    # 可以在调用后修改 result
    return result
```

用途：重试、缓存、请求修改、日志。

### 7.6 tools_condition 辅助函数

```python
def tools_condition(state, messages_key="messages"):
    if state["messages"][-1].tool_calls:
        return "tools"
    return "__end__"
```

标准 ReAct 循环的现成路由函数。

## 八、通道类型

`graph/channels.py`（内部）：

| 通道类型 | 行为 |
|---|---|
| `LastValue` | 保留最后写入的值（默认） |
| `Topic` | 累积列表，支持去重和限制 |
| `Context` | 引用外部上下文对象 |
| `BinaryOperatorAggregate` | 用自定义函数累积 |
| `AnyValue` | 宽松验证，接受任何值 |

## 九、典型使用模式

### 9.1 标准 ReAct Agent

```python
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition

graph = StateGraph(AgentState)
graph.add_node("agent", agent_llm_node)
graph.add_node("tools", ToolNode([tool1, tool2]))
graph.set_entry_point("agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")
compiled = graph.compile()
```

### 9.2 多 Agent 协作

```python
graph.add_node("researcher", research_node)
graph.add_node("writer", write_node)
graph.add_node("reviewer", review_node)
graph.add_edge("researcher", "writer")
graph.add_edge("writer", "reviewer")
graph.add_conditional_edges("reviewer", lambda s: "writer" if s["needs_revision"] else END)
```

### 9.3 人机交互

```python
graph = graph.compile(
    checkpointer=MemorySaver(),
    interrupt_before=["human_review"]
)
# 运行到 human_review 节点前暂停
# 人工确认后继续
graph.update_state(config, {"approved": True})
graph.stream(None, config)
```

### 9.4 并行扇出

```python
# 用 Send API 动态创建并行任务
def continue_to_jokes(state):
    return [Send("tell_joke", {"topic": t}) for t in state["topics"]]
```

## 十、总结：LangGraph 的优缺点

### 值得借鉴

1. **BSP 执行模型** — Plan → Execute → Update 循环，语义清晰、可预测
2. **通道抽象** — 不同类型的状态合并策略（LastValue、append、自定义 reducer）
3. **versions_seen 机制** — 精确知道哪些节点需要重新执行
4. **检查点透明化** — 每个步骤自动保存，恢复、时间旅行、人机交互天然支持
5. **条件边** — 函数式路由，灵活的分支逻辑
6. **ToolNode** — 开箱即用的工具执行节点，中间件模式强大
7. **参数注入** — InjectedState 等机制让工具能访问图上下文而不需要模型传入

### 作为 Code Agent 使用时的问题

1. **通用框架的复杂度** — 不是为 Code Agent 场景裁剪的，很多概念我们不需要
2. **执行粒度** — Code Agent 的工具执行需要流式输出、progress 回调、abort 信号，这些超出标准 ToolNode 的范围
3. **无内置权限系统** — LangGraph 的设计目标不包含安全/权限
4. **依赖重** — 依赖 langchain-core，而我们只需要图编排部分
5. **无文件感知** — 没有 file read cache、过期检测机制

## 十一、我们如何借鉴

我们自研的 StateGraph 引擎借鉴 LangGraph 的核心模式但大幅简化：

| LangGraph 概念 | 我们的实现 | 简化理由 |
|---|---|---|
| StateGraph 构建器 | ✅ 保留（builder.py） | 核心价值 |
| Pregel BSP 循环 | ✅ 保留（pregel.py） | 执行语义清晰 |
| 通道（Channel） | ✅ 简化到 3 种 | LastValue、AppendReducer、MergeReducer 足够 |
| 条件边 | ✅ 保留 | 路由决策必需 |
| 检查点 | ✅ 保留 | 恢复/持久化必需 |
| ToolNode | ❌ 自建 tool_node | 需要 progress、abort、权限钩子 |
| 参数注入 | ❌ 不需要 | 用 runtime config 字典替代 |
| Send API（动态扇出） | ❌ 不需要 | Code Agent 场景不需要 |
| SubGraph | ❌ 不需要 | 子代理走 AgentTool |
