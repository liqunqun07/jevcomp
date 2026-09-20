---
name: jev-compaction
description: 用 Jev（TypeSafe System One）对任意 agent 的会话转录做"删除式"上下文压缩 —— 不做有损摘要，只删除/截断过期的工具调用与输出，保留内容逐字不动。当用户要求"压缩会话/清理上下文/compaction/释放 context"，或某个 agent 会话文件过大（>5MB）需要瘦身时使用。支持 Claude Code / zcode / Codex 及任意 agent（通用 JSON 格式）。
---

# jev-compaction：全 Agent 通用的 Jev 上下文压缩

核心：对每个工具调用问 Jev 两个校准概率 —— 调用本身是否还要（keepCall）、结果是否必须逐字保留
（keepResult）。低于阈值（默认 0.5）的删除；调用还要但结果不要的，结果截为头部 300 字符+注记。
**用户与助手文本永远不改写**。首条消息与最近 N 条（默认 6）钉死不动。减幅 <25% 时应放弃并告知用户。

工具获取（任选其一）：
- 已 pip 安装：直接用 `jevcomp` 命令；
- 仓库 clone 在本机：`cd <仓库目录> && /usr/bin/python3 -m jevcomp ...`（或 `export PYTHONPATH=<仓库目录>` 后任意目录可用）。
详见工具仓库 README.md。API key 在 `~/.jevcomp.json`（或环境变量 TYPESAFE_API_KEY）。

## 步骤

1. **永远先 dry-run**（不调 API、不写文件）：

```sh
cd <仓库目录>   # pip 安装过则任意目录
/usr/bin/python3 -m jevcomp claude <session.jsonl> --dry-run
```

2. **压缩**（默认输出 `<file>.jevcomp.jsonl`，原文件绝不动）：

```sh
/usr/bin/python3 -m jevcomp claude  <session.jsonl>          # Claude Code
/usr/bin/python3 -m jevcomp zcode   latest                    # zcode（只读导出；--list 列会话）
/usr/bin/python3 -m jevcomp codex   <rollout.jsonl>           # Codex
/usr/bin/python3 -m jevcomp generic <transcript.json>         # 任意 agent：{"messages":[...]}
```

3. **报告**：向用户转述统计行（`messages X → Y; chars ... (Z% reduction); calls ...`）与
   `wrote <输出文件>`。`--in-place` 原地改写会自动留 `.jev-bak-*` 备份，仅在用户明确要求时使用。

## 红线

- 不删除、不改动用户/助手文本；一切以工具输出统计行为准。
- zcode 的 SQLite 不写回（保守只读）；Claude/Codex 的 `--in-place` 需用户明确要求。
- 会话里若含敏感内容（密钥、病历等），先提醒用户数据会发往 TypeSafe 第三方 API。
- 真正关键的信息（约束、路径、错误原文）建议让主 agent 写入文件，而不是依赖压缩保留。
