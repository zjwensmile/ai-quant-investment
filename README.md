# AI Quantitative Investment

基于财务数据、公开信息和多 Agent 讨论的本地投资研究工作台。
项目关注公司经营、可解释估值与市场定价之间的偏离，并保存判断依据和复核条件。
价值投资模块以寻找有依据的价格与价值差异为研究目标，不承诺价差一定收敛或预测短期走势。

## 当前功能

| 模块 | 研究内容 | 页面入口 |
| --- | --- | --- |
| 价值投资 | 财报筛选、商业质量、增长兑现、估值情景、交叉质询与条件方案 | 首页“价值投资” |
| 尾盘隔夜 | 分钟量价、隔夜事件、次日退出情景 | 首页“尾盘隔夜” |
| ETF 动态仓位 | 产品与流动性核验、行业及市场配置、已有库存的做 T 条件 | 首页“ETF 动态仓位 / 做 T” |
| 缠论旧版流程 | 本地 Rust 二买/三买候选、两轮研究与组合风控 | `/legacy` |

三个独立模块各采用 **六个角色**：三位专业研究员、交叉质询、组合决策、独立风控。
运行记录、证据、讨论和研究方案保存在本地 SQLite 与 JSON 文件中。
界面区分研究草案、批准状态、历史报价和账户事实。当前版本不连接券商自动下单。

课堂方案中的“总控协调八角色、统一输出买入/等待/卖出”是后续架构方向。
本发布版仍使用固定六角色模块流程，计划状态为 `approve` / `defer`，末端由独立风险角色复核。
课堂中的长江电力八角色短测与股利折现案例属于独立协作研究，未写入本发布版生产流程。
当前价值模块结构化估值合同支持正常化 PE、前瞻 PE、P/FCF 及证据不足状态。
详见 [架构与实现范围](docs/architecture.md)。

## 快速启动

推荐 Python 3.12，以下命令在 **Windows PowerShell、仓库根目录** 中执行。
程序使用当前工作目录读取配置和保存数据。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[data]"
.\.venv\Scripts\python.exe -m ai_quant init
.\.venv\Scripts\python.exe -m ai_quant serve --host 127.0.0.1 --port 8787
```

打开 <http://127.0.0.1:8787>。首次启动没有历史研究和持仓，这是正常的空库状态。
浏览页面无需模型登录。`[data]` 安装 AKShare 等行情依赖，真实扫描需要联网。
发布版固定 AKShare 1.18.94，较新版本已改变本项目使用的 ETF 解析接口，升级前需要做兼容性验证。
按 `Ctrl+C` 停止前台服务。端口被占用时可换成 `--port 8788`。

也可以先用 `conda env create -f environment.yml` 创建 Python 环境，再安装项目。
本项目本次整理以 Windows 为验证平台。其他系统应调整虚拟环境和 Rust 可执行文件路径，
Windows 任务计划功能仅适用于 Windows。

## 正式 Agent 研究

本项目的运行器调用 Codex CLI，并检查 ChatGPT 登录状态，不读取 API Key 来替代会员登录。
请按 [OpenAI Codex CLI 官方说明](https://github.com/openai/codex#readme) 安装并登录自己的账户。

```powershell
codex login
codex login status
.\.venv\Scripts\python.exe -m ai_quant doctor
```

`config/system.json` 由初始化命令根据示例生成。将 `codex_command` 设置为本机能执行的 Codex 路径。
Windows 若命令解析异常，可填写 `codex.exe` 的完整路径。
当前代码保留原环境的 `gpt-5.6-sol` / `gpt-6-astra` 路由，**不保证其他账户能使用这些模型标识**。
开始正式研究前，应根据自己的 Codex 权限核对并修改 `src/ai_quant/module_agents.py` 与
`src/ai_quant/registry.py` 中的模型名称；同步调整相关路由断言。项目没有自动模型回退。
运行器还使用 `exec --ephemeral --json --output-schema` 和 `--enable browser_use`，
需要对应 CLI 版本支持这些参数与功能；本次发布验证不包含真实模型联网研究。

在界面选择模块，先“扫描候选”，再“开始 Agent 讨论”。命令行对应：

```powershell
.\.venv\Scripts\python.exe -m ai_quant module-run --module value --action scan
.\.venv\Scripts\python.exe -m ai_quant module-run --module value --action analyze
```

可将 `value` 换为 `closing` 或 `etf`。扫描与研究依赖行情源、财报源、网络和模型可用性，
首次抓取可能较慢。正式研究可能把相关持仓、规划预算及研究上下文传给模型服务，
请在自行配置账户后按自己的数据使用要求运行。

## 本地配置与账户

- `config/system.example.json`：可公开的系统模板，默认手动触发，关闭真实交易。
- `config/account.example.json`：空持仓、未知现金的虚构模板。示例预算不是已确认入金。
- `config/system.json`、`config/account.json`：本机配置，已通过 `.gitignore` 排除。
- `runtime/`：运行数据库、行情、研究记录和任务日志，已通过 `.gitignore` 排除。

需要自己的股票账户时，复制账户模板为 `config/account.json`，填写确认过的预算、现金、
现金状态与交易权限后执行 `python -m ai_quant account-import`。不要把模板直接当成真实账户导入。
ETF 账户事实通过页面单独维护。规划预算与实际可用现金始终分别处理。
默认 ETF 策略排除债券/货币类，并带有市场与仓位限制，这是策略设定，不代表普适资产配置建议。

## 缠论 Rust 桥接（可选）

仅打开三个模块页面不需要编译 Rust。使用旧版缠论扫描时，需要 Rust 工具链以及对应平台的链接器。

```powershell
cargo build --release --locked --manifest-path rust/chanlun_bridge/Cargo.toml
cargo test --locked --manifest-path rust/chanlun_bridge/Cargo.toml
```

Windows 默认可执行文件为 `rust/chanlun_bridge/target/release/chanlun_bridge.exe`。
可以通过 `AI_QUANT_CHANLUN_BRIDGE_PATH` 指定实际路径，或在本机系统配置中更改。
桥接依赖随仓库保留的 `chanlun-engine-main/chanlun` 源码，无需复制旧环境的工具链或二进制。

## 验证与测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ai_quant run --snapshot fixtures/snapshot.example.json --runner fake
```

`fake` 运行只验证旧版流水线及记录保存，不调用真实模型，不形成投资效果证据，
也不会发布为首页三个模块的正式推荐。测试通过不能证明策略收益。本次发布检查结果见 [验证记录](docs/validation.md)。

## 目录

```text
src/ai_quant/             Python 应用与页面
tests/                   回归测试
config/*.example.json    公开配置模板
fixtures/                明确标记为测试用途的输入
rust/chanlun_bridge/      Python/Rust JSON 桥接
chanlun-engine-main/      必要算法源码及原许可证
docs/                    架构、发布说明
LICENSE                  本项目 MIT 许可证
THIRD_PARTY_NOTICES.md    第三方归属说明
```

本仓库不包含作者的账户、模型登录信息、历史行情库、实盘记录、PPT 或研究过程截图。
上传步骤见 [发布说明](docs/publishing.md)。

## 许可证

本项目原创代码采用 [MIT](LICENSE)，第三方算法库按其原许可证分发。
研究结果依赖输入数据和假设，不构成收益保证。使用者自行判断研究结论及交易风险。
