# GitHub 上传指南

基准：2026-10-29。请使用本次 **GitHub 源码包**。其中包含源码、README、部署说明、技术文档、测试与 CI；没有旧运行日志、聊天截图、Git 历史或内置 Python。

## 一、最省事的上传顺序

1. 全部解压缩源码 ZIP，进入 `ai-banking-agent` 文件夹。该层应该直接看到 `README.md`、`部署说明.md`、`banking_agent/`、`docs/`、`tests/`。
2. 登录自己的 GitHub，新建仓库，例如 `ai-banking-agent`。按自己的分享范围选择公开或私有；新空仓库无需另外初始化 README。
3. 在仓库页面选择 **Add file → Upload files**，上传上一步文件夹里面的内容，保留子文件夹结构。README 应位于仓库根目录，不要只把整个 ZIP 当作一个文件上传。
4. 确认 `.github/workflows/tests.yml` 和 `.gitignore` 也存在。Windows 默认可能隐藏点开头的目录/文件；若网页方式遗漏，使用下一节 Git 命令上传。
5. 填写提交说明，例如 `Add local virtual banking agent and technical docs`，按页面提示提交。上传后检查 README、架构图和各文档的链接。
6. 打开 **Actions** 查看自动测试。工作流尚未在你的远程仓库执行；绿色结果必须以真实运行记录为准。

GitHub 网页上传单文件上限 25 MiB，一次最多 100 个文件；实际规则见 [GitHub 官方上传说明](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)。本源码包设计为小体积源码集合，免安装二进制适合另外作为 Release 附件分发。

## 二、用 Git 上传（能保留隐藏文件）

先在 GitHub 建立空仓库，然后在**解压后的 `ai-banking-agent` 目录**打开 PowerShell。下面用户名、仓库名是占位符，需要替换；命令不包含密码或令牌。

```powershell
git init
git add .
git status
git commit -m "Add virtual banking agent and technical documentation"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

提交前检查 `git status`，应为代码/技术资料，不能出现 `data/`、运行日志、`runtime/`、缓存、临时文件或本机截图。Git 按正常流程要求身份和登录时，由仓库所有者自己配置；不要把 GitHub 密码或访问令牌写入项目文件。若远端已有提交，应先克隆远端并把源码复制进去，再正常提交，避免强制推送覆盖既有内容。

## 三、仓库中应看到的内容

| 内容 | 位置 |
| --- | --- |
| 项目首页与运行入口 | `README.md`、`launch_demo.py`、两个 CMD 启动文件 |
| 系统架构与核心算法 | `docs/系统架构.md`、`docs/核心算法.md`、`docs/架构图.svg`/`.png` |
| 安全、权限与风险 | `SECURITY.md`、`docs/安全设计.md`、`docs/权限分级实现.md`、`docs/已知风险清单.md` |
| 部署与测试 | `部署说明.md`、`docs/测试用例.md`、`tests/` |
| 自动化测试 | `.github/workflows/tests.yml` |
| 业务实现与网页 | `banking_agent/`、`banking_agent/web/` |
| 打包与忽略规则 | `tools/`、`.gitignore`、`manifest.json`（构建器生成的文件校验清单） |

## 四、CI 与许可证

CI 使用 Windows/Linux 与 Python 3.11/3.13 组合运行标准库 unittest，只授予仓库内容读取权限，不负责部署、发布或交易。Windows CMD 专属测试在其他平台跳过。Actions 下载官方 Action 与解释器需要 GitHub 网络；本机运行项目不需要联网。Action 使用说明来源：[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)。

本次没有擅自添加开源许可证。公开仓库不等于自动授予开源许可；如需允许他人复制、修改和再分发，可由项目所有者决定许可证后再添加 `LICENSE`。原 Windows 免安装分发中附带的 Python 许可证仍应保留；GitHub 源码包不附带该二进制运行时。

本次交付只准备本地可上传材料，不会创建远程仓库或代替你提交到 GitHub。
