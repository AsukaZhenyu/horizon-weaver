# Docker Host Memory：本机跑 terminal-bench 时 Docker npipe 反复超时的根因与修复

> 日期：2026-08-29
> 环境：Windows 11（15.2GB RAM）/ RTX 4070 Laptop GPU（8GB VRAM）/ Docker Desktop + WSL2
> 场景：HorizonWeaver + terminal-bench，本地 llama-server 跑 Qwen3.5-9B（Q4_K_M）

## 现象

跑任务时 Docker Desktop 反复 npipe 读超时，与模型 / agent 代码无关：

- `docker compose up -d` 挂起 >120s（容器其实已经 up）
- `container.exec_run`（send_keys / capture_pane）读超时 60s
- 严重时 `docker version` 都 hang

表现为任务多轮崩溃（`failure_mode: unknown_agent_error`）。

## 根因（非显而易见）

llama-server 即使 `-ngl 99` 全 GPU offload，进程 **working set 仍占 ~5GB 系统 RAM**。

原因：llama.cpp 用 mmap 加载 GGUF 模型文件（5.28GB）。加载时逐张量从 mmap 读到内存再拷到 GPU，这些 mmap 页是干净的、可回收的，但 Windows 不主动回收，一直驻留在进程 working set。

后果：15.2GB 总内存被压到只剩 ~1.4GB free（91% 占用），Docker 的 WSL2 后端分配不到内存、疯狂 swap，导致 npipe 读超时。

## 定位方法

```powershell
# 内存压力
Get-CimInstance Win32_OperatingSystem | Select FreePhysicalMemory, TotalVisibleMemorySize
# 进程占用（llama-server 的 working set ~5GB 是最大单一消费者）
Get-Process | Sort WorkingSet64 -Descending | Select -First 12 Name, @{N='MB';E={[math]::Round($_.WorkingSet64/1MB,0)}}
```

## 修复：裁剪 llama-server 的 working set

```powershell
Add-Type -Namespace Win32 -Name Memory -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern bool SetProcessWorkingSetSize(IntPtr proc, int min, int max);
'@
$p = Get-Process llama-server
[Win32.Memory]::SetProcessWorkingSetSize($p.Handle, -1, -1)
```

效果：working set 5GB → ~1MB，空闲内存 1.4GB → ~5.1GB。

**裁剪无损**：推理时只换回 ~545MB 真正需要的嵌入层（GPU 权重不碰 mmap），不会回涨到 5GB。

## 完整处置流程

1. 裁剪 llama-server working set（见上）。
2. `wsl --shutdown` + 重启 Docker Desktop，清掉 WSL2 后端。
3. 等 `docker version` 秒回后再跑任务。

## 其他要点

- harness 的 `docker_compose_manager.py` 已内置 `up -d` 超时 fallback：容器 running 就继续。compose 超时是**假阴性**，别误判为任务失败。
- release-b8705 标准版 llama-server 启动参数：
  `--ctx-size 16384 -ngl 99 --cache-type-k bf16 --cache-type-v bf16`
  （Qwen3.5-9B Q4_K_M，全 GPU offload，bf16 KV 省显存；模型路径见 `agent/loop.py`）。
