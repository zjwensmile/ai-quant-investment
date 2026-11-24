# 第三方代码与依赖

## 缠论算法库

`chanlun-engine-main/chanlun/` 是原项目附带的 Chanlun Engine Rust 算法库。
原版权：Copyright (c) 2026 缠论密码-M321。
原许可证全文保留于 [chanlun-engine-main/LICENSE](chanlun-engine-main/LICENSE)。
本发布目录仅携带 `chanlun` crate 的源码、测试与 Cargo 元数据，未修改算法源码，
未分发原项目的 TDX/MT4 DLL、截图、宣传资料或构建产物。
`rust/chanlun_bridge` 通过 Cargo 本地路径依赖此库，因此保留原目录层级。

## 其他依赖

Python 依赖声明于 `pyproject.toml`，Rust 依赖声明于各 `Cargo.toml` 与 `Cargo.lock`。
依赖通过包管理器安装，其许可证分别由上游维护。本仓库 MIT 不替代第三方许可证。

原工作目录中的 `TradingAgents-main` 是独立参考项目，本发布目录不包含其代码和压缩包，
本项目的运行代码不导入 `tradingagents` 包。该参考项目不作为安装依赖。
