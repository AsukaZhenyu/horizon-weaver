# DeepSeek Harness / Cordis 源码分析

> 来源：`book-reference/A Programming Paradigm for Spatiotemporal Composability.pdf`（论文，83 页）+
> `source-code-reference/deepseek-harness`（代码，TypeScript / Node）| 定位：**基于「时空可组合性」范式的插件化 Agent 运行时**

---

## 零、一句话定位

DeepSeek Harness（代码库内叫 `dsh`）不是一个 Agent，而是一个 **meta-framework**——它把「插件框架 Cordis」的成果拿来做 Agent 运行时：**agent 循环、模型适配器、工具注册表、会话日志、沙箱、权限——每一个都是可替换的插件**。它声称（并有论文背书）解决了其他 Agent 架构都没有真正解决的问题：**在运行时安全地装卸、替换、自我修改组件，且保证副作用可完整回退**。

这份分析分两部分：前半讲论文的**形式化基础**（这才是它区别于所有同类的地方），后半讲代码库的**工程落地**，最后回到 keyan-automation 该怎么借。

---

## 一、论文：两个维度 + 一个范式 + 一个演算

论文标题《A Programming Paradigm for Spatiotemporal Composability》直接点题：**时空可组合性**。作者把「动态组合」（运行时加载/卸载/重配组件）拆成两个正交维度：

| 维度 | 问的是什么 | 形式化 | 直觉类比 |
|---|---|---|---|
| **时间可组合性** (temporal) | 移除一个组件时，它对共享环境做的修改能否**完全安全地回退** | 可回退效应 (revertible effects) | RAII / `useEffect` cleanup，但升级到「可组合、可异步、框架自动执行」 |
| **空间可组合性** (spatial) | 组件之间如何**声明、发现、解析依赖**，依赖变化时如何响应 | 反应式 coeffects (reactive coeffects) | OSGi 的服务依赖，但把「手写同步 deactivate 回调」变成「自动回退 + 异步惯性」 |

### 1.1 可回退效应（revertible effects）

核心：**每一次 context 变换都携带一个逆变换（inverse），由运行时持有**。移除组件时，按 **LIFO** 顺序执行逆变换，把 context 恢复到组件加载前的状态。

关键点在于它比现有机制都更彻底：

- **React `useEffect`** 最接近「效应↔逆」的结构化配对，但它只能顶层调用、不能异步、不能组合，所以得不到「复合逆」。
- **OSGi / IntelliJ / VSCode 的 unload 回调** 把逆变换的职责丢给开发者手写，忘记一个就静默泄漏资源（论文用 VSCode 数据佐证：top 100 扩展里 87 个含可执行代码，卸载需要重启 host）。
- **Cordis 的做法**：原子效应由作者提供一个逆，**复合效应的逆由组合自动推导**。作者写原子逆，系统算复合逆——这是「把正确性从『每个开发者的勤奋』上移到『抽象层一次性保证』」的关键。

### 1.2 反应式 coeffects（reactive coeffects）

组件声明它需要的依赖（coeffect 规格说明 `inject`），**每一次 context 变化都会被分类为「激活 / 去激活 / 中性」**，驱动组件生命周期的自动切换。依赖出现→组件激活；依赖消失→组件去激活（去激活 = 回退其全部效应）；依赖只是值变了→中性，不打扰。

### 1.3 Context 范式（the context paradigm）

把「效应 context」和「coeffect context」**统一成一种 context 类型**，所有效应和 coeffect 都通过它中介。这个中介引入一个**观测等价（observational equivalence）**：不同组件的效应交错执行，互不干扰。

一句话：**Context 是唯一的中介。效应 = 你对 context 做了什么（可回退），coeffect = 你需要 context 提供什么（可反应）。**

### 1.4 动态组合演算（calculus）+ 元理论

把上面两者组合成「组件（component）」概念，给出操作语义，然后证明了一串性质：**保型 (preservation)、时间可组合性、空间可组合性、进度 (progress)、合流 (confluence)**。合流定理尤其重要——它保证「无论装载器以什么顺序、怎么折腾，系统最终静止的状态只由最终配置决定」，这正是热插拔「增量重配」的正确性基石。

### 1.5 诚实的分寸

论文的案例研究是 **Koishi**（一个 4000+ 插件的聊天机器人框架），**不是** Agent。论文结论自己写得很清楚：*"self-evolving agent harnesses… is future work"*。也就是说——**「这个范式对 Agent 更优」目前是论文的论点，不是已跑通 benchmark 的实证**。这一点在第六节会展开。

---

## 二、代码库：dsh 的工程落地

### 2.1 结构：一个 monorepo，全是插件

```
deepseek-harness/
├── vendor/cordis        # vendored 的 Cordis 插件框架源码（论文 §5 的实现）
├── packages/<group>/<pkg>/   # 45+ 分组，全是 @deepseek-ai/dsh-*
│   ├── core/            # 产品 API 脊柱：session / system-prompt / tools / agent / agent-loop
│   ├── shell/           # bash capability：Service Def + local/pwsh 提供者 + 工具消费者
│   ├── fs/  lsp/  web/  skill/  subagent/  terminal/  compaction/  sandbox/ ...
│   ├── guard/           # loop-hygiene + tool-timeout 插件
│   └── self-modification/# agent 检查/挂载它自己的插件
├── bundle/              # dsh-base / dsh-web-app / dsh-headless（profile 层）
├── sdk/  acp/  workflow/  e2b/  ...
└── python/  native/     # Python SDK + Landlock 原生沙箱
```

`dsh` 的每一样东西都是插件：模型适配器、工具注册表、会话日志、**甚至 agent 循环本身**。`architecture.md` 第一句话就点明：*"There is no privileged core to patch: you extend dsh by mounting a plugin beside the others."*

### 2.2 核心抽象：capability seam（能力接缝）

这是 dsh 工程层面最重要的概念。一个 **seam** 有三个角色：

- **Service Definition** — 声明接口（一个 `ctx.<key>` + 类型词汇，通常是抽象类）
- **Service Provider** — 实现接口（一个或多个，可换）
- **Consumer** — 注入并使用接口（常是 model-facing 工具）

换一个 provider，**整个产品跟着变**，而且消费方不用改。论文里最漂亮的例子：

> 文件系统（`ctx.fs`）和子进程（`ctx.subprocess`）共享同一个执行世界。把它们指向远程沙箱，Bash、PTY、LSP 就**一起**搬过去，没有任何 provider fork。

这就是「灵活性」的真正来源：不是「多写几个 if」，而是「能力可以在配置层被整体替换」。

### 2.3 事件系统：四态派发

Cordis 的通信全靠**类型化事件**，四种派发模式：

| 模式 | 是否 await | 顺序 | 有返回值 | 用途 |
|---|---|---|---|---|
| `emit` | 否 | 注册序观察 | 无 | 观察/广播 |
| `waterfall` | 否 | 注册序观察 | 有 | **中间件**（`next()` 委派，不调 `next()` 即短路） |
| `parallel` | 是 | 并行 | 无 | 扇出 |
| `serial` | 是 | 注册序 | 有 | 串行决策 |

`waterfall` 就是「around-middleware」：`agent/pre-step`、`agent/request`、`tools/pre-execute` 都是 waterfall，任何插件都能在模型看到前改写/拦截输入、在工具执行前审批。

### 2.4 会话日志是唯一真相：**model-visible ⟺ logged**

这是 dsh 最硬的一条不变式：

> 任何到达模型请求的东西，都必须能从会话日志重建；新增一个「模型可见的输入」就必须新增一个 session event。

由此派生：fork、resume、transcript、telemetry、persistence **全部**从这条流派生。`deriveMessages()` 从日志投影出模型历史，`assistant/chunk` 原始事件保留回放和 UI 保真。

### 2.5 Turn / Step 模型

- **step** = 一次模型请求 + 它引发的工具调用
- **turn** = 0 或多个 step（从第一个输入被认领，到什么都不欠了关闭）

配合 `agent/pre-step`（waterfall，可改写或拒绝）→ `agent/request` → `llm/stream` → `assistant/chunk*` → `tool/call*` → `tools/pre-execute → execute → post-execute` → `tool/result*` → `step/end` → `turn/end` 的完整流水线。

### 2.6 一个特别值得注意的分组：self-modification

`packages/self-modification/` 的描述是 *"the agent inspects/mounts its own plugins"*，还有 `demo:cordis` 命令（*"the agent modifies its own runtime"*）。这直接对应论文 §1.2.2 的愿景：**agent 自己生成、替换自己的 harness 组件**。这是「自我进化」的最小实现——而它之所以可行，正是因为有可回退效应兜底（换错了能完整回退）。

---

## 三、核心算法（论文 §5，值得读原文）

论文把形式化模型落到 5 个算法上，这是整个机制的精髓：

- **Algorithm 1 (Effect tracking)**：`execute(callback, guard)` 把回调当效应迭代器驱动，每一步 yield 一个逆，折叠成单一复合逆；guard 在迭代边界中止。`ctx.effect` 是它的薄包装，加了**自我处置**（armed 标志，逆只触发一次）和**父组合**（子效应的逆被 prepend 到父 context 的累计逆上）。
- **Algorithm 2 (Coeffect ops)**：`ctx.get`/`ctx.set` 走两层解析 `key → realm → value`；`set` 本身是 `ctx.effect`，所以**提供 coeffect 也是可回退的**。
- **Algorithm 3 (Reactive notification)**：每次绑定变化，遍历所有 fiber，检查变化 key 是否在某 fiber 的 `inject` 里且解析到同一 realm，是则 `refresh(fiber)` 重新评估。这就是「激活/去激活/中性」分类的运行时形式。
- **Algorithm 4 (Component instantiation)**：`ctx.use` 把组件实例化为 fiber，实例化本身是父 context 的一个**被跟踪的效应**——所以卸载父组件会**级联**到子组件。
- **Algorithm 5 (Component lifecycle)**：fiber 的**惯性状态机**。`reload`/`unload` 一旦进入就跑到完成才响应下一个目标变化；`refresh` 先标记 `UNLOADING`（停止提供）再排逆变换，`unload` 先等所有被通知的依赖方到达 `INACTIVE` 再回退——这就是论文 Theorem 70 的 coeffect 顺序保证。

**核心洞察**：fiber 的身份用 provider 的 `uid`（每次新取、永不重用）而不是值来标识，所以「替换了 provider」不会被误认成「同一个 provider 更新了值」。一次比较就够。

---

## 四、与其他 Agent 架构的对比（回答「更优更灵活」）

把 dsh 和前面分析过的三个对照：

| 维度 | Claude Code | Aider | LangGraph | **DeepSeek Harness** |
|---|---|---|---|---|
| Agent 循环 | **硬编码线性 while** | 硬编码 REPL | 图定义的固定形状 | **循环本身是插件** (`ctx.agentLoop`)，可整体替换 |
| 模型适配 | 绑定 Anthropic | litellm 多模型 | 任意可调用 | `ctx.llm` seam，多提供者 |
| 工具系统 | 40+ 内置 + MCP | 基于提示的编辑 | ToolNode 预构建 | `ctx.tools` seam + 完整 pre/execute/post 管线 |
| 运行时装卸能力 | ❌ 工具/技能无法热卸载 | ❌ | ❌ 图编译后固定 | ✅ **可回退效应 + HMR + 自我修改** |
| 依赖反应 | ❌ | ❌ | 版本号机制（versions_seen） | ✅ **反应式 coeffects，依赖变化自动激活/去激活** |
| 状态真相源 | React 内存态 | Git + 内存 | 检查点 | **append-only 会话日志**（model-visible ⟺ logged） |
| 形式化基础 | 无 | 无 | Pregel/BSP（工程语义） | **演算 + 元理论**（合流/进度/保型） |
| 可组合单元 | 工具/MCP | Coder 多态 | 节点 | **组件（效应+coeffect 一体）** |

**结论：在「可扩展性 / 可替换性 / 运行时安全自修改」这一轴上，dsh 确实比另外三个都强，而且强得不是一点点——因为另外三个根本没把「运行时装卸 + 副作用回退 + 依赖反应」当作一等公民来解决。**

但要守住分寸，这个「更优更灵活」有四个限定：

1. **「灵活」是框架级灵活，不是「任务得分更高」**。dsh 是 harness，不是带 accuracy 的 benchmark 结果。它让你能换掉一切，但**不替你决定换什么**。Claude Code 的默认 ReAct 循环是「够用」的，dsh 把「什么循环最好」留给了使用者。
2. **论文自己承认 Agent 应用是 future work**（§8 结论）。「对 Agent 更优」目前是论点 + 代码库方向，不是实证。
3. **代价是抽象陡峭**。effect/coeffect/纤维/惯性——这套范畴论词汇的学习成本不低；组件粒度更细会带来更多配置、命名、认知负担（论文 §6.5 直说 mutual dependency 拆解会让集成组件数量二次增长）。
4. **纯名义依赖**（§6.6）：依赖匹配靠 key 名，无版本/结构类型，key 冲突和接口漂移是开放问题。

---

## 五、总结：值得借鉴 vs 不足

### 值得借鉴（对我们有直接价值的部分）

1. **可回退效应 = 回滚的正确形式化**。keyan-automation 现在用 git 当「回滚」的粗粒度替身（`loop.py` 状态=git）。Cordis 给的是细粒度版本：**每一次 act（跑命令/写文件/起进程）都带逆，回滚 = LIFO 处置效应**。这比 git revert 更贴合「探索失败回退到上一节点」的语义。

2. **反应式 coeffect = 依赖感知的图节点**。keyan-automation 的图（节点 + 探索状态）可以建模为 coeffect：`retrieve` 节点声明对某 `source` 节点的依赖，source 被 refute / 变化时，依赖节点**自动失效**。这正是 v0.1 死掉的「loop-closure 该强制换路」——把它从「模型层的指令」变成「框架层的自动失效」。

3. **会话日志是唯一真相（model-visible ⟺ logged）**。keyan-automation 有 `graph/nodes.jsonl`，但缺一条硬不变式：**模型看到的每样东西都必须能从节点日志重建**。这条纪律能根治「过早 done / 幻觉完成」——因为完成声明必须有日志可验证的产出证据。

4. **capability seam 的三角（Def/Provider/Consumer）**。把 tool 从「硬编码 bash/file」解耦成「可换 provider」，本地→沙箱→远程一次切换。

5. **事件四态派发 + waterfall 中间件**。`tools/pre-execute` 这种拦截点，天然是「done 门控」「命令白名单」「回环跳过」该挂的地方——比在循环里写 if 干净得多。

### 不足 / 不适合照搬的

1. **它不解决「好循环长什么样」**——它让循环可换，但循环设计本身仍是我们的活。keyan-automation 的六步循环（observe→decide→act→sense→update→loop-closure）的价值判断不会从 dsh 自动获得。

2. **内存态不跨 reload 存活**（§7.3）。Cordis 回退旧效应、从干净 slate 重放新组件，组件自己的内存态不保留，除非放进更长命的依赖。DSU 式「状态向前迁移」是 future work。对需要长命状态的探索进程要小心。

3. **TS/Node 实现 + 陡峭抽象**。范式语言无关，但落地是 TS；我们栈是 Python，直接 vendor 不现实，只能**吸收思想重实现**（就像之前 LangGraph 分析里「我们如何借鉴」的结论）。

4. **纯名义依赖**。依赖靠 key 名匹配，无结构/版本类型。小项目无感，开放生态会撞 key 冲突。

---

## 六、我们如何借鉴（映射到 keyan-automation）

借鉴的不是「换成 Cordis」，而是把它的**三条铁律**吸收进自研引擎：

| Cordis / dsh 概念 | 我们吸收成什么 | 解决 v0.1 的哪个问题 |
|---|---|---|
| 可回退效应（LIFO 逆变换） | `act` 返回一个逆/disposer，`rollback` = 处置效应链 | 让「回滚」从 git 粗替身变成真正的节点级回退 |
| 反应式 coeffect（依赖失效自动去激活） | 图节点声明依赖，source 被 refute 时依赖节点**自动置 INVALID** | **loop-closure 空转**（`_state_key` 只标 REFUTED 不阻止重执行 → 改成框架级失效） |
| 会话日志不变式（model-visible ⟺ logged） | 节点日志是唯一真相，`done` 必须有可验证产出 | **过早 done**（无产出证据就报完成） |
| 事件 waterfall 拦截点 | `tools/pre-execute` 处做 done 门控 + 回环跳过 + 命令白名单 | 把治理逻辑从循环体里挪到拦截层 |
| capability seam（Def/Provider/Consumer） | 工具（bash/fs/terminal）解耦成可换 provider | 本地→Docker→远程沙箱一次切换（呼应 terminal-bench 的 Docker 环境） |

**最该拿走的一句话**：keyan-automation 的核心痛点「左右脑互博、反复横跳」——本质是**缺乏框架级的『效应可回退 + 依赖可反应』保证，只能靠模型自律**。dsh 证明了这两个性质可以被形式化、被框架自动执行。我们把「回滚」和「换路」从模型提示词的软约束，升级成引擎的硬保证，方向就对了。

---

## 七、一句话结论

DeepSeek Harness 确实**在「可扩展性 / 可替换性 / 运行时安全自修改」这一轴上是目前见过的 Agent 架构里最强的**——因为它把别人当工程补丁处理的「装卸 + 回退 + 依赖反应」上升成了有元理论背书的编程范式。但它是**框架而非 agent**，灵活≠得分更高，且 Agent 应用仍被论文自己标为 future work。对 keyan-automation 的价值不是「迁移」，而是**偷走它的三条铁律**：效应可回退、依赖可反应、日志是唯一真相——这三条恰好是 v0.1 暴露的三个 P0 缺陷（回环空转、过早 done、多行命令）的正解。
