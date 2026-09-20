# jevcomp — 全 Agent 通用的 Jev 上下文压缩

> 基于 [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction)（MIT）的
> 设计改造，扩展为 **agent 无关**的通用工具，支持 Claude Code / zcode / Codex 等多个 agent：
> **不做有损摘要，只做删除** —— TypeSafe 的 Jev（System One 模型）对每个工具调用回答
> 两个校准的 yes/no 概率（调用本身是否还要、结果是否必须逐字保留），过期的删除或截断，
> **留下的一字不改**。用户与助手文本永远不动。

同一套算法、三种载体，覆盖本机所有 agent：

| 载体 | 适用 | 位置 |
| --- | --- | --- |
| **Python CLI**（零依赖） | zcode / Claude Code / Codex / 任意 agent 的会话文件 | `jevcomp/` |
| **Claude Code 插件**（function hooks，纯 JS） | `/compact` 与自动压缩实时替换 | `hooks/` + `.claude-plugin/` |
| **Skill**（`jev-compaction`） | 所有支持 skills 的 agent 按需调用 | `skill/` → `~/.agents/skills/` |

## 安装

需要：python3 ≥ 3.9；Claude Code ≥ 2.1.274（仅插件需要）。**复制下面整段执行即可**：

```sh
git clone https://github.com/liqunqun07/jevcomp.git && cd jevcomp && bash install.sh
```

装的内容：CLI + skill + zcode/Codex/Claude Code 三端 MCP 注册 + LaunchAgent 定时压缩 +
Claude Code 插件（写入 settings env 前自动备份）。脚本会提示输入 TypeSafe API key
（也可提前 `export TYPESAFE_API_KEY=<key>`）。可选参数：

```sh
bash install.sh --cli-only   # 只要 CLI + skill（不装插件/定时任务）
bash install.sh --no-pip     # 不动 pip
```

或分步手动：

```sh
git clone https://github.com/liqunqun07/jevcomp.git && cd jevcomp
# 1) CLI（任意目录可用 jevcomp 命令）
python3 -m pip install --user .
# 2) skill（Claude Code 读 ~/.claude/skills，其他 agent 读 ~/.agents/skills）
cp -R skill ~/.agents/skills/jev-compaction && ln -sfn ~/.agents/skills/jev-compaction ~/.claude/skills/jev-compaction
# 3) Claude Code 插件（marketplace 直接用 GitHub 仓库）
claude plugin marketplace add liqunqun07/jevcomp && claude plugin install jevcomp@jevcomp
# 并在 ~/.claude/settings.json 的 env 加: CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1, TYPESAFE_API_KEY=<你的key>
```

## 触发方式（自动 vs 手动）

| 通道 | Claude Code | zcode | Codex | 其他 agent |
| --- | --- | --- | --- | --- |
| **压缩内替换**（`/compact` 走 Jev） | ✅ 插件 function hooks，全自动 | —（zcode 无 PreCompact 事件） | —（Codex 无此 API） | — |
| **MCP 工具**（agent 自主调用 `jevcomp_*`） | ✅ `claude mcp add` 已注册 | ✅ `~/.zcode/cli/config.json` | ✅ `config.toml [mcp_servers.jevcomp]` | ✅ 任何支持 MCP 的 agent |
| **定时 autopilot**（后台压缩已结束大会话） | ✅ | ✅ | ✅ | ✅（覆盖所有会话存储） |
| **Skill**（`/jev-compaction`，请求匹配时自主触发） | ✅ | ✅ | 经 AGENTS.md 指引 | ✅ |

- **Claude Code**：装完即全自动 —— 上下文 ≥ `compactAtPercent`(60%) 自动压缩、`/compact` 走 Jev、减幅 <25% 回退内置摘要。
- **zcode / Codex**：两家都不暴露"替换压缩"的钩子，所以自动性由两条腿实现：
  1. **MCP**：重启 zcode/Codex 后 `jevcomp` server 自动连接，agent 可直接调 `jevcomp_compact` / `jevcomp_autopilot`（Codex 端另有 `~/.codex/AGENTS.md` 指引何时调用）；
  2. **LaunchAgent autopilot**（每天 03:00 / 15:00）：自动压缩"已结束 ≥30 分钟且 >300KB"的会话（zcode 仅处理闲置 >24h 的，单行 UPDATE 保守写回、全库先备份），resume 时上下文天然是瘦的。
- 手动随时可用：`jevcomp autopilot --dry-run`、`jevcomp <fmt> <file>`（原文件永不改动）。

## 兼容哪些 agent

| Agent | 接入方式 | 自动化程度 |
| --- | --- | --- |
| **Claude Code** ≥ 2.1.274 | 插件（function hooks）+ MCP + skill + CLI | ⭐⭐⭐ 全自动（/compact 与自动压缩走 Jev） |
| **zcode** | MCP（user config）+ skill + CLI + autopilot | ⭐⭐ MCP 自主调用 + 定时后台压缩 |
| **OpenAI Codex CLI** | MCP（config.toml）+ AGENTS.md 指引 + CLI + autopilot | ⭐⭐ 同上 |
| **Gemini CLI / Continue / Cline / Roo Code / Cursor / Windsurf 等** | 标准 MCP server + CLI | ⭐⭐ agent 自主调用 |
| **任何其他 agent** | 通用 JSON 管道（`jevcomp generic`）+ 手动 CLI | ⭐ 手动 |

唯一硬性要求：一个 TypeSafe（Jev）API key。Claude Code 插件另需 ≥ 2.1.274（function hooks 为 early access）。

## 逐端配置（全部可直接复制）

> 把 `<你的key>` 换成你的 TypeSafe API key；`/path/to/jevcomp` 换成你 clone 的目录
> （若已 `pip install`，MCP 配置里的 `command` 可直接写 `jevcomp`，无需 PYTHONPATH）。

### Claude Code

插件（`/compact` 与自动压缩走 Jev）：

```sh
git clone https://github.com/liqunqun07/jevcomp.git && cd jevcomp
claude plugin marketplace add liqunqun07/jevcomp && claude plugin install jevcomp@jevcomp
```

在 `~/.claude/settings.json` 的 `env` 对象里加两个键（文件里已有其他键就不用动）：

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1",
    "TYPESAFE_API_KEY": "<你的key>"
  }
}
```

MCP（agent 自主调用 `jevcomp_*` 工具）：

```sh
claude mcp add jevcomp --scope user -- python3 -m jevcomp mcp
# 或指定仓库路径（未 pip 安装时）：
claude mcp add jevcomp --scope user --env PYTHONPATH=/path/to/jevcomp -- /usr/bin/python3 -m jevcomp mcp
```

skill：

```sh
cp -R skill ~/.agents/skills/jev-compaction && ln -sfn ~/.agents/skills/jev-compaction ~/.claude/skills/jev-compaction
```

### zcode

编辑 `~/.zcode/cli/config.json`，合并进 `mcp.servers`（保留原有内容）：

```json
{
  "mcp": {
    "servers": {
      "jevcomp": {
        "type": "stdio",
        "command": "/usr/bin/python3",
        "args": ["-m", "jevcomp", "mcp"],
        "env": { "PYTHONPATH": "/path/to/jevcomp" }
      }
    }
  }
}
```

skill 复制到 `~/.agents/skills/jev-compaction/`（同上）。重启 zcode 后 Settings → MCP 里应看到 `jevcomp` 已连接。

### OpenAI Codex CLI

编辑 `~/.codex/config.toml`，追加：

```toml
[mcp_servers.jevcomp]
command = "/usr/bin/python3"
args = ["-m", "jevcomp", "mcp"]
env = { "PYTHONPATH" = "/path/to/jevcomp" }
```

（可选）在 `~/.codex/AGENTS.md` 追加使用指引，让 Codex 知道何时自主调用：

```markdown
## Context compaction (jevcomp)

The `jevcomp` MCP server provides jevcomp_list_sessions, jevcomp_compact and
jevcomp_autopilot. Use them when a session feels heavy, the user asks to
compact/clean up context, or before resuming a large old rollout: prefer
dry_run=true first, show the stats, then run with dry_run=false.
```

### 其他 MCP 客户端（Gemini CLI / Continue / Cline / Cursor / Windsurf…）

通用 stdio 配置（按各客户端格式填写同样内容）：

```json
{
  "command": "/usr/bin/python3",
  "args": ["-m", "jevcomp", "mcp"],
  "env": { "PYTHONPATH": "/path/to/jevcomp" }
}
```

### 定时后台压缩（macOS，可选）

```sh
sed "s#__JEVCOMP_HOME__#$PWD#g" launchd/com.jevcomp.autopilot.plist \
  > ~/Library/LaunchAgents/com.jevcomp.autopilot.plist
launchctl load ~/Library/LaunchAgents/com.jevcomp.autopilot.plist
# 每天 03:00 / 15:00 自动压缩已结束的大会话；手动试跑: python3 -m jevcomp autopilot --dry-run
```

## 实测数据（2026-09-20，真实 API）

| 场景 | 结果 |
| --- | --- |
| Claude Code 会话 927 条消息（11MB） | 927→503 条，字符 **-81%**，212 个调用删除 |
| zcode 会话（SQLite） | 109→100 条，字符 **-91%**，103 个调用删除 |
| Codex rollout（20MB） | 255→27 条，字符 **-99%**，114 个调用删除 |
| Claude Code 实机 `/compact`（插件） | `kept 3/15 messages, no summary (82% reduction)` |
| autopilot 自动批处理 | 33MB Claude **-90%**；Codex 21/20/19/17MB → **-99% / -94% / -98% / -98%** |

## 算法（详见 `jevcomp/decide.py` / `state.py`）

1. `tool_use` 与 `tool_result` 按 id 配对；首条消息与最近 `preserveRecentMessages` 条**钉死不动**。
2. 发给 Jev 的 **state** = 完整对话（工具输出替换为 `ok, 4213 chars (omitted)` 一行注记），
   逐级收缩塞进 `maxStateTokens`（输入截断 1000→200→60 → 长文掐头去尾 → 旧文塌缩 → 旧调用压成一行 →
   丢无调用的旧条目 → 折叠相邻调用行；都塞不进就报错，绝不悄悄过度截断）。
3. 每个非钉死调用两个 `noul` 问题，按 `maxRequestTokens` 分批并发请求，答案合并。
4. 决策：`keepResult ≥ 0.5` → 全留；`keepCall ≥ 0.5` → 留调用、结果截为头部 300 字符+注记；
   否则调用连同结果整删。
5. Jev 失败/答案非法/减幅 < 25% → 回退原机制（插件）或保持原样（CLI）。

## 手动安装（不用脚本时）

```sh
git clone https://github.com/liqunqun07/jevcomp.git && cd jevcomp
python3 -m pip install --user .        # 得到全局 jevcomp 命令
# 或不安装: export PYTHONPATH=$PWD 后用 python3 -m jevcomp（在 jevcomp 目录内）
```

API key 解析顺序：`--api-key` > 环境变量 `TYPESAFE_API_KEY` > `~/.jevcomp.json`。

> **网络注意**：部分地区直连 `api.typesafe.ai` 的 TLS 会被重置。CLI 已内置自动探测：
> 本地 7890 端口有代理（Mihomo/Clash）就走代理；也可 `JEVCOMP_PROXY=http://127.0.0.1:7890` 显式指定。

## CLI 用法

```sh
cd jevcomp   # git clone 后的仓库目录
jevcomp=("/usr/bin/python3 -m jevcomp")             # 下文简写 jevcomp

jevcomp claude  <session.jsonl>                 # 输出 <file>.jevcomp.jsonl（原文件不动）
jevcomp claude  <session.jsonl> --in-place      # 原地改写（自动留 .jev-bak-* 备份）
jevcomp zcode   latest                          # zcode 会话（只读，导出通用 JSON）
jevcomp zcode   --list                          # 列出 zcode 会话
jevcomp codex   <rollout.jsonl>                 # Codex 会话
jevcomp generic <transcript.json>               # 通用格式 {"messages":[...]}
jevcomp <fmt> ... --dry-run                     # 只做规划（state 分级/批次数），不调 API
jevcomp <fmt> ... --json                        # 机器可读统计
jevcomp <fmt> ... --stdout                      # 压缩结果打到 stdout
# 常用参数: --keep-threshold --preserve-recent --truncate-head --goal --max-state-tokens
```

**默认绝不改原文件**；`--in-place` 才写回且必留备份。zcode 是 SQLite、有运行时引用，
写回采取保守策略（不删行，清空输入+注记替换输出），且当前仅建议对已结束会话手动执行。

## Claude Code 插件

```sh
# 需 Claude Code ≥ 2.1.274（function hooks, early access）
# ~/.claude/settings.json → env: CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1, TYPESAFE_API_KEY=<key>
claude plugin marketplace add liqunqun07/jevcomp   # 或本地路径
claude plugin install jevcomp@jevcomp
```

或开发模式直接加载：`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir <本目录>`。

行为：`/compact`（manual）与自动压缩（auto）都经 Jev 重新打分；
减幅 ≥ `minReductionRatio`(25%) 时用「删除后的原消息」替换摘要（toast 显示
`jevcomp: kept N/M messages, no summary (…)`），否则回退内置摘要。
`turn.complete` 在上下文 ≥ `compactAtPercent`(60%) 时自动触发压缩。
沙箱无 Node API，`hooks/jevcomp-hook.js` 为同一算法的自包含 JS 实现，HTTP 走 `$.http.fetch`。

## 测试

```sh
cd jevcomp
/usr/bin/python3 -m unittest discover -s tests     # 核心算法（fake asker，不打网络）
/usr/local/bin/node tests/test_hook.mjs            # 插件 hook（本地 mock Jev）
~/.local/bin/claude plugin validate .claude-plugin/plugin.json
```

## 致谢

算法设计源自 [tamaratran/fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction)（MIT），jevcomp 在其基础上改造并扩展支持多个 agent。
