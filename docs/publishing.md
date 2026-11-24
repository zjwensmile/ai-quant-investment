# 上传 GitHub

此文件夹是独立发布副本，源系统和运行数据留在原工作目录中。
上传本文件夹的内容即可，不要回到原项目目录执行全量上传。

## 使用 Git

在 GitHub 创建空仓库，例如 `ai-quant-investment`，先不要让网站额外生成 README 或许可证。
然后在本发布文件夹打开 PowerShell：

```powershell
git init
git branch -M main
git add .
git diff --cached --stat
git status --short
git commit -m "Initial public release"
```

将 GitHub 页面提供的仓库 URL 添加为 `origin`，再推送：

```powershell
git remote add origin https://github.com/YOUR_USERNAME/ai-quant-investment.git
git push -u origin main
```

`YOUR_USERNAME` 必须换成自己的用户名。没有替用户创建仓库、提交或推送。
也可以使用 GitHub Desktop 添加本地仓库后发布。

## 后续维护

默认忽略本机配置、`runtime/`、数据库、日志、虚拟环境、Rust 构建结果、工作材料与 PPT。
`.gitignore` 不能清除已经进入 Git 历史的数据，上传前应查看本次暂存内容。
不要加入 Codex 登录文件、原账户记录、券商截图或带个人账户内容的研究轨迹。

本发布副本保留业务实现，仅删除两处提示词中的个人账户背景，重写公开说明，
将账户示例替换为未确认现金的空持仓模板，并将 AKShare 固定到验证版本 1.18.94。源系统不受这些整理影响。
