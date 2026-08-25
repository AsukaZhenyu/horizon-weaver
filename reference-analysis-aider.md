# Aider 源码分析

> 版本：0.86.0 | 语言：Python | 定位：终端 AI 结对编程助手

## 一、项目概述

Aider 是一个终端内的 AI 结对编程工具，允许用户通过自然语言与 LLM（Claude、GPT-4o、DeepSeek 等）交互来编辑代码。它的核心思路是 **"基于提示格式的代码编辑"**，而不是传统的工具调用（function calling）。

## 二、入口与启动

- **CLI 入口**：`aider/main.py:main()`
- **模块入口**：`aider/__main__.py` → `from .main import main`

启动流程：
1. 解析 CLI 参数（`args.py`）
2. 设置 Git 仓库（`main.py:setup_git()`）
3. 选择模型和编辑器格式
4. 创建 Coder 实例并启动 REPL 循环

## 三、核心架构：Coder 多态

### 3.1 Coder 工厂

`Coder.create()`（`base_coder.py:124-201`）根据 `edit_format` 选择对应的 Coder 子类：

| Coder 子类 | edit_format | 说明 |
|---|---|---|
| `EditBlockCoder` | `diff` | 搜索/替换块（默认） |
| `WholeFileCoder` | `whole` | 全文件重写 |
| `UnifiedDiffCoder` | `udiff` | 统一 diff 格式 |
| `ArchitectCoder` | `architect` | 架构师模式（规划+编辑分离） |

### 3.2 代理循环：`send_message()` 流程

`base_coder.py:1419-1623`，这是 Aider 的核心：

```
1. 用户输入追加到 cur_messages
2. format_messages() 组装完整消息列表
3. 令牌数检查（超过 max_input_tokens 警告）
4. 缓存预热（后台线程定期发送 1-token 请求保持 prompt cache）
5. 调用 LLM（带重试逻辑）：
   - ContextWindowExceededError → 标记 exhausted
   - 其他异常 → 指数退避重试（0.125s 起，最大 60s）
   - FinishReasonLength → 尝试继续生成
   - KeyboardInterrupt → 干净退出
6. 处理响应（流式或批量）
7. 后处理：
   - 检测响应中提到的文件，建议加入对话
   - 应用编辑（apply_updates）
   - 自动 Git 提交
   - Lint 检查 + 自动修复循环
   - 运行 shell 命令（如果 LLM 建议）
   - 运行测试（如果 auto_test 开启）
```

### 3.3 反射循环

`run_one()`（`base_coder.py:924-944`）实现了反射机制：如果 LLM 的响应触发了 lint 错误或测试失败，自动发送反馈消息让 LLM 修正，最多重试 3 次。这是一种 **"无工具调用的错误恢复"** 策略。

## 四、模型抽象（models.py，~45KB）

### 4.1 ModelSettings 数据类（第 108-131 行）

每个模型的行为由 20+ 个字段控制：
```
name, edit_format, weak_model_name, use_repo_map, send_undo_reply,
lazy, overeager, reminder, examples_as_sys_msg, extra_params,
cache_control, caches_by_default, use_system_prompt, use_temperature,
streaming, editor_model_name, editor_edit_format, reasoning_tag,
remove_reasoning, system_prompt_prefix, accepts_settings
```

### 4.2 模型配置来源

1. **内置 YAML**：`aider/resources/model-settings.yml` → 启动时加载
2. **用户覆盖**：`register_models()` 从用户 YAML 文件覆盖
3. **Generic 匹配**：`apply_generic_model_settings()` 通过模型名字符串模式匹配推断设置（如含 "claude" → 用 diff 格式、支持缓存等）

### 4.3 litellm 集成

- **懒加载**：`aider/llm.py` 中的 `LazyLiteLLM` 代理对象，延迟 ~1.5 秒的 import 到首次使用
- **调用**：`model.send_completion()` → `litellm.completion(**kwargs)`
- **令牌计数**：`litellm.encode()` / `litellm.token_counter()`
- **成本计算**：`litellm.completion_cost()`

## 五、提示管理

### 5.1 提示类层次

```
CoderPrompts (base_prompts.py)         ← 基类，定义所有属性
  └── EditBlockPrompts (editblock_prompts.py)  ← 覆盖 main_system, system_reminder
```

### 5.2 提示模板模式

使用 Python `.format()` 字符串模板：
```python
main_system = """
You are an expert software engineer...
{final_reminders}
{language}
{shell_cmd_prompt}
{fence}
"""
```

### 5.3 消息组装流程

`format_chat_chunks()` → `ChatChunks` 数据类：
```
system + examples + readonly_files + repo + done + chat_files + cur + reminder
```

每个部分由 Coder 基类的对应方法生成（`get_chat_files_messages()`、`get_repo_messages()` 等）。

## 六、RepoMap：仓库映射（repomap.py）

这是 Aider 最精妙的设计之一。

### 6.1 核心思路

用 **PageRank** 对文件重要性排序，在令牌预算内选出最有价值的文件上下文。

### 6.2 实现步骤

1. **Tag 提取**：用 tree-sitter 解析每个文件，提取标识符定义（`def`）和引用（`ref`）
2. **图构建**：`networkx.MultiDiGraph`
   - 节点 = 文件
   - 边 = 引用 → 定义（权重加成：对话中提到的标识符 ×10，复合名称 ×10，已在对话中的文件 ×50）
3. **PageRank 排序**：个性化权重偏向对话中的文件
4. **令牌预算截断**：二分搜索找到适合 `max_map_tokens` 的最多文件数
5. **树形渲染**：`to_tree()` → `render_tree()` 输出结构化代码大纲

### 6.3 缓存

Tag 缓存到 SQLite（`.aider.tags.cache.v{version}`），避免重复解析。

## 七、Git 集成（repo.py）

### 7.1 自动提交

`commit()` 方法（第 131 行）：
1. 检查是否有脏文件
2. 生成 diff + 提交信息（用弱模型生成）
3. 应用归属（`--attribute-author`、`--attribute-committer`、`--attribute-co-authored-by`）
4. 执行 `git commit`

### 7.2 撤销

`/undo` 命令：验证上次提交是 Aider 创建的 → `git checkout HEAD~1 <files>` → `git reset --soft HEAD~1`

### 7.3 脏提交

编辑前如果文件有未提交的修改，先自动提交保存现场（`dirty_commits` 功能）。

## 八、命令系统（commands.py）

### 8.1 发现机制

反射：任何 `cmd_xxx` 方法自动成为 `/xxx` 命令。

### 8.2 核心命令

| 命令 | 功能 |
|---|---|
| `/add` | 添加文件到对话 |
| `/model` | 切换模型 |
| `/chat-mode` | 切换编辑格式 |
| `/clear` | 清除对话历史 |
| `/commit` | 手动提交 |
| `/diff` | 查看差异 |
| `/drop` | 移除文件 |
| `/map` | 显示仓库地图 |
| `/run` | 执行 shell 命令 |
| `/tokens` | 查看令牌用量 |
| `/undo` | 撤销上次修改 |
| `/web` | 抓取网页内容 |

### 8.3 SwitchCoder 机制

`/model`、`/chat-mode` 等命令抛出 `SwitchCoder` 异常，被上层捕获后用新参数重建 Coder，不中断 REPL。

## 九、总结：Aider 的优缺点

### 值得借鉴

1. **外部 YAML + 代码覆盖** 的模型配置方式 — 灵活且可维护
2. **编辑格式多态** — 不同 LLM 用不同编辑策略
3. **令牌预算 PageRank** — 优雅解决"该给 LLM 看哪些文件"
4. **懒加载** — 避免启动时的 1.5 秒 litellm 导入
5. **归属系统** — 精细的 Git 提交归属控制

### 不足之处

1. **基于提示的编辑** — SEARCH/REPLACE 块容易因空格不匹配而失败
2. **无真正的工具调用** — 依赖 LLM 输出特定格式，不如 function calling 可靠
3. **反射循环是补偿机制** — 用多次 LLM 调用来修复编辑错误，效率低
4. **字符串模板提示** — `.format()` 比代码构建的提示更难组合和维护
5. **缓存预热是 Hack** — 后台线程发空请求保持缓存，不如 API 级别处理优雅
