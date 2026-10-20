# HorizonWeaver 运行入口（唯一）

v0.5.1 起收敛为**一条**运行入口：所有入口默认 spec 关（`build_default_plugins()` /
裸 `NodeAgent()` / `tb_test.TbTestAgent` 三者一致），要复现 v0.4 spec-first 一律显式
`enable_spec=True`。依赖收敛到 `requirements.txt`（`pydantic` + `httpx`）。

## 1. 安装依赖

```bash
pip install -r requirements.txt
# 或（本机实测：默认 Python 缺 pydantic，须用 Conda 环境）
conda activate <你的 env> && pip install -r requirements.txt
```

## 2. 单测（免 pytest，直接用标准库跑）

```bash
cd agent
python test/run_all.py            # 一次跑全部套件
python test/test_schema.py        # 单套件
```

`test/_runner.py` 自己把 `agent/` 加进 `sys.path`，无需设 PYTHONPATH。

## 3. Terminal-Bench 冒烟（8 任务）

前置：`llama-server` 已在 `http://127.0.0.1:8080/v1` 起好（`--reasoning off`），
并在 terminal-bench 仓库目录内（本文件 `agent/tb_test.py` 所在目录需在 sys.path 上）。

```bash
cd <terminal-bench 仓库>
uv run tb run --task-id <TASK_ID> \
    --agent-import-path "tb_test:TbTestAgent" \
    --model qwen3.5-9b \
    --agent-kwarg api_base=http://127.0.0.1:8080/v1
```

8 个冒烟任务：`jq-data-processing`、`broken-python`、`fix-git`、`countdown-game`、
`pandas-etl`、`npm-conflict-resolution`、`fix-permissions`、`cprofiling-python`。
