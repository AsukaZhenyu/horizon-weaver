"""test 套件的共享 runner 与 sys.path 引导。

每个 test_*.py 第一句 `from _runner import run`，副作用是把 agent/ 加进 sys.path，
使 `from schema / tools / engine / runtime / plugins / node_agent import ...` 可用；
再调用 run(globals()) 发现并运行模块内所有 test_* 函数（免 pytest，直接跑）。

运行任一单套件：
    python test/test_schema.py           # 在 agent/ 目录
    cd test && python test_schema.py     # 在 test/ 目录
一次跑全部：
    python test/run_all.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# 无论从 agent/ 还是 agent/test/ 启动，都把 agent/ 加进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(module_globals: dict) -> int:
    """发现并运行 module_globals 里所有 test_* 函数，返回失败数。"""
    tests = [
        v for k, v in sorted(module_globals.items())
        if k.startswith("test_") and callable(v)
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return failed
