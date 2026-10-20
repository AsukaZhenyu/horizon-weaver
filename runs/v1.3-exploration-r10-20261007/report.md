# v1.3 dev10 Profiling 单项复测

回归 216/216，18 项真实 Docker，无跳过；两项真实模型动态解码探针通过。

修改：每次模型请求将 check_ids 绑定到 State IR 中已保存的定义，空集合不提供引用分支，Agent 仍可创建新 checks。

Qwen3.5-9B Q4_K_M，原模型配置、40 步 / 300 秒预算，仅 cprofiling-python 一次。

整项通过：False；外部检查 0/2；结束 no_progress；218.2s；步骤 15。

模型调用 15；协议错误 0；超时 0；临时服务错误 0。

已保存定义引用请求 0；实际执行 0；未知 ID 0。

结构化验证收据 0；行为检查 0；process 0；反例 0；不确定 0；controls 0；最新回复遗漏 0。

初始输入与 dev8/dev9 一致，原任务和评分器未改；事件链、State IR 往返、所有动态 schema 哈希和输出引用属于已记录定义的审计通过。见 audit.json、provenance.json 和 findings.md。

dev8 完整八项是 2/8、18/30；dev9 两项复测是 1/2、4/5，其中 profiling 1/2。各版本单独记录，不择优合成新八项分数；单次开发诊断不证明稳定准确率收益。简单/中等任务目标仍待验收。
