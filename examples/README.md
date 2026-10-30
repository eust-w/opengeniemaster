# 示例

| 文件 | 用途 |
| --- | --- |
| `claude_code.mcp.json` | Claude Code 项目级 MCP 配置，复制为仓库根目录的 `.mcp.json` 即可使用 |
| `codex_config.toml` | Codex 的 MCP 配置片段，追加到 `~/.codex/config.toml` |
| `fleet.json` | 真机机队文件：机器人型号、执行服务端点、令牌环境变量、标定版本、策略卡片 |
| `records.json` | 便利店业务记录样例：商品目录、参考图 URI、货架图、相似品家族 |
| `human_backend.py` | 人在回路的机器人端执行服务：每个请求交给操作员完成并回报结果 |

## 用人在回路后端试跑真实环境

在机器人旁的电脑上启动执行服务：

```bash
python examples/human_backend.py --robot-id g2-01 --host 0.0.0.0 --port 8765 --token "$G2_01_TOKEN"
```

在 Agent 一侧启动 MCP 服务器：

```bash
G2_01_TOKEN=... ogm mcp --fleet examples/fleet.json --state-dir .ogm
```

随后编码 Agent 下发的每个技能请求（含引导：参考图、目标框、约束等）都会显示在操作员终端上。操作员遥操作或手动指挥机器人完成后，输入 `y`；失败时输入 `n <失败码> <说明>`，例如 `n target_not_localized 价签挡住了挂钩`。判据检查同样由操作员确认。

这样无需策略就能检验任务分解、连续性、恢复和记忆是否合理。录下的事件日志（`--state-dir` 下的 `events.jsonl`）可以用 `ogm dataset .ogm/events.jsonl --out episodes.jsonl` 导出；由操作员完成的执行会被标记为人工接管数据（`source: human_takeover`）。接入真实策略时，复制该文件，把每处 `input()` 换成对 VLA 推理服务、控制器和检测器的调用即可。
