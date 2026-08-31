"""tools.py：ToolsPlugin —— 提供 ctx.tools（bash/write/read 三个终端工具）。

注意：本文件是**插件**（ToolsPlugin），只做「把纯函数工具挂到 ctx.tools」这一件事；
终端工具的具体实现（run_bash / write_file / read_file）在 agent 顶层的 tools.py
（纯函数层）。二者同名但职责不同——插件层认领装配，纯函数层做具体实现。

未来工具插件（websearch / localfilesearch）往 ctx.tools 这个 dict 加 key，循环零改动。
"""
from __future__ import annotations

from runtime import Plugin
from tools import read_file, run_bash, write_file


class ToolsPlugin(Plugin):
    name = "tools"
    provides = ["tools"]
    requires = []

    def setup(self, ctx) -> None:
        ctx.provide("tools", {"bash": run_bash, "write": write_file, "read": read_file})
