# RegNetwork MCP

RegNetwork MCP 是一个**线上优先、只读**的本机 MCP 服务。Agent 在你的电脑上启动它；服务按需访问 RegNetwork 网站，不要求先下载整个数据库，也不会修改上游数据库。

首发目标是 Windows、Python 3.13 和支持本机 stdio MCP 的客户端。首版提供 8 个科研数据工具：名称解析、调控关系查询、邻居查询、子网络提取与汇总、来源证据字段读取、状态查询，以及 TSV/JSON/GraphML 导出。

> 软件工程测试只检查安装、接口和结果处理，不代表 RegNetwork 的生物学内容已由专家复核，也不构成实验或因果验证。

## 安装前准备

需要 Windows 10/11 和网络连接。使用 uv 启动器运行应用，无需克隆项目仓库；如果电脑尚未安装 Python 3.13，uv 会按配置准备运行时。

在 PowerShell 安装 uv：

```powershell
winget install --id=astral-sh.uv -e
```

安装后重新打开 PowerShell，确认 `uv --version` 和 `uvx --version` 都能运行。`uvx` 是 uv 附带的临时运行命令；首次使用时它会下载这个 MCP 包和所需 Python 环境。

可先单独检查包能否启动：

```powershell
uvx --python 3.13 --from regnetwork-mcp==0.1.0 regnetwork-mcp --version
```

## 接入 Codex

确认 Codex 命令行可用后，在 PowerShell 运行：

```powershell
codex mcp add regnetwork -- uvx --python 3.13 --from regnetwork-mcp==0.1.0 regnetwork-mcp
codex mcp list
```

重启 Codex 桌面应用或重新打开会话，然后让 Agent 查询 RegNetwork MCP 的工具。上述命令固定使用 `0.1.0`，可以复现同一版服务。

也可以把 [Codex TOML 示例](examples/codex-config.toml) 中的配置段加入 `%USERPROFILE%\.codex\config.toml` 的 `[mcp_servers.regnetwork]` 配置。

## 接入 Claude Code

在要使用这个 MCP 的项目目录中打开 Claude Code 终端，然后运行：

```powershell
claude mcp add --transport stdio --scope project regnetwork -- uvx --python 3.13 --from regnetwork-mcp==0.1.0 regnetwork-mcp
claude mcp list
```

`--scope project` 会把配置写入当前项目根目录的 `.mcp.json`，方便把配置与项目一起复现。首次打开该项目时，Claude Code 会要求你审查并批准项目 MCP；批准后可运行 `/mcp` 检查连接。`claude mcp list` 若显示 `Pending approval`，表示还没完成这一步。

若希望它在你电脑上的所有项目中都可用，把命令里的 `--scope project` 改为 `--scope user`。也可将 [Claude Code 项目配置示例](examples/mcp-config.json) 复制为项目根目录的 `.mcp.json`，再在 Claude Code 中审查并批准。

其他能启动本机 stdio MCP 的 Agent，也可以参考这两份配置中的 `command` 和 `args`。客户端添加 MCP 的方式不完全相同，请以对应客户端的当前说明为准。

## 更新与卸载

更新时，把客户端配置中的 `regnetwork-mcp==0.1.0` 改成要使用的版本，再重启客户端。固定版本可以让同一分析流程继续使用同一 MCP 版本；不要在尚未确认新版本前删除旧版本记录。

从 Codex 移除：

```powershell
codex mcp remove regnetwork
```

从 Claude Code 移除：

```powershell
claude mcp remove regnetwork
```

如果使用 `.mcp.json`，删除该文件中 `regnetwork` 项即可。uvx 管理运行环境；不需要单独运行卸载程序。

## MCP 能做什么

| 工具 | 用途 |
|---|---|
| `get_database_status` | 检查线上统计接口、来源统计一致性、缓存与能力 |
| `resolve_entities` | 根据物种和名称查找来源实体候选，明确标出歧义 |
| `search_interactions` | 按调控关系两端及类型、集合、来源分数筛选 |
| `get_neighbors` | 查询一个实体的一跳上游、下游或两者 |
| `extract_subnetwork` | 提取输入基因集合内部的有向关系 |
| `summarize_network` | 汇总节点、边、类型、自环、度数和弱连通分量 |
| `get_interaction_evidence` | 查看上游已有的来源、score、PMID、方法及缺失原因 |
| `export_network` | 导出 TSV、JSON 或 GraphML，并生成可复现清单和校验值 |

一个常见分析顺序是：先用 `resolve_entities` 明确身份，再查询关系或提取子网络，查看结构与证据，最后导出结果。

物种参数为 `human` 或 `mouse`；集合参数为 `core` 或 `complete`（对应网站的 all）。每次查询都应明确指定物种和集合。陌生 accession 需要先用名称解析取得本 MCP 的来源实体 ID。

完整的参数和返回结构见 [MCP 工具 Schema](docs/tool-schemas.json)。

## 怎样理解结果

- `ok` 表示这次操作成功；还需查看 `coverage`、`warnings` 和来源记录。
- `empty` 表示本次查询没有观察到匹配项，不证明生物学上不存在关系。
- `partial` 表示分页、输入映射、证据或来源统计存在不完整情况；按返回说明判断能否继续使用。
- `error` 或 `unavailable` 表示操作失败，不能解释为“零关系”。
- 来源记录包含访问时间、响应 hash 和 online/cache 标记。多个分页结果可能来自不同时间，不代表同一个原子数据库快照。

工具保留物种、namespace/accession 和复合 ID；不会只凭相同符号合并 miRNA 前体与成熟体。来源缺少的分数、PMID 或类型不会被补造。来源 score 不是效应大小、激活/抑制方向或疾病因果证据。当前 MCP 不抓取论文全文、不修改源数据库、不提供全库镜像、富集、PCA-CMI 或本地数据库服务器管理。

## 数据来源与使用边界

查询直接访问 [RegNetwork 官网](https://www.zpliulab.cn/RegNetwork/home) 的公开查询页面。来源版本标记和字段说明以其当时的响应为准。背景论文为 [RegNetwork 2025 论文](https://pmc.ncbi.nlm.nih.gov/articles/PMC12807656/)（DOI: 10.1093/nar/gkaf779；PMID: 40804431）。MCP 不下载整库，因此在线页面或服务变化可能影响查询。

RegNetwork 官网说明其数据用于学术研究，商业使用需联系维护者。本项目代码按 MIT 许可证发布，但这不授予上游数据库超出其自身条件的使用权。使用者应单独遵守上游数据条款。

## 缓存和导出位置

服务只在本机保存小型查询缓存、已提取分析结果和用户请求的导出文件，不保存整库。默认位置：

- Windows：`%LOCALAPPDATA%\RegNetworkMCP`
- macOS/Linux：`$XDG_DATA_HOME/regnetwork-mcp`；未设置时为 `~/.local/share/regnetwork-mcp`
- SQLite 缓存：数据目录下 `.runtime/results.sqlite`
- 导出：数据目录下 `exports/`

如需换位置，在客户端的 MCP 环境变量中设置 `REGNETWORK_MCP_ROOT`。例如 Codex 配置：

```toml
[mcp_servers.regnetwork]
command = "uvx"
args = ["--python", "3.13", "--from", "regnetwork-mcp==0.1.0", "regnetwork-mcp"]
env = { REGNETWORK_MCP_ROOT = "D:\\RegNetworkMCP" }
```

缓存默认有效 24 小时，最多保留 7 天，缓存载荷上限为 50 MiB。`refresh: true` 会重新访问线上；访问失败不会静默返回过期缓存。清理缓存不会删除导出文件。

## 开发与验收

开发者需要 Python 3.13 和 uv。在项目目录运行：

```powershell
uv sync --locked
uv run python -m pytest -q
uv build --no-sources
```

使用 MCP Inspector 的方式见 [连接示例](examples/mcp-config.json)。本地开发和首发流程见 [发行说明](docs/RELEASING.md)；可公开的协议验收摘要见 `docs/verification/`。
