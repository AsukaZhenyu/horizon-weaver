"""run_all.py：一次跑完 test/ 下所有套件。

运行（在 agent/ 目录）：
    python test/run_all.py
"""
from __future__ import annotations

from _runner import run

import test_engine
import test_node_agent
import test_plugins
import test_runtime
import test_schema
import test_tools


def main() -> int:
    modules = [
        test_schema,
        test_tools,
        test_engine,
        test_runtime,
        test_plugins,
        test_node_agent,
    ]
    failed = 0
    for mod in modules:
        failed += run(vars(mod))
    print(f"\n==== 全部套件：{failed} 个失败 ====")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
