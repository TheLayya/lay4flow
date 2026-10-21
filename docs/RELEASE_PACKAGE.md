# lay4flow-remote 发布与验收

开源源码包含 Python 服务、前端源码及构建产物、离线检查、Docker 配置、文档和 MIT 归属文件。仓库中没有远程验证码服务实现、真实账号、运行数据库或私有部署配置。

## 发布前检查

```sh
python -m pip install -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
python scripts/check.py
docker build -t lay4flow-remote:ci .
docker run --rm --network none lay4flow-remote:ci python scripts/smoke_package.py
python scripts/package_release.py /path/to/lay4flow-remote-runtime.tar.gz
python scripts/build_release.py dist/1.2.0
```

运行包使用固定文件清单与公开源码/静态资源扩展名白名单。包含 `src/`、`static/`、启动脚本、示例配置、运行文档、README 引用的演示截图及许可证；前端源码与完整开发测试留在源码仓库。截图仅含合成数据，逐文件加入公开清单。压缩包不包含私有 TOML、`.env`、`data/`、`tmp/`、本地账号 JSON、日志、证据目录、密钥或 Python 缓存。

打包脚本输出版本、文件清单与 SHA-256；发布者应保留清单并核对压缩包内容。CI 在 Linux 与 Windows 运行离线检查，在 Linux 构建镜像、断网烟测和生成运行包。未上传到 GitHub 的本地准备结果不等于已公开发布。

## 版本与安装资产

根目录 `VERSION` 是运行版本来源；API、`/health`、后台更新记录和运行包使用该版本。每版先更新 `docs/CHANGELOG.md`，再运行 `scripts/build_release.py`，在全新输出目录生成：

- `lay4flow-remote-版本-runtime.tar.gz`：Docker 更新用公开运行文件。
- `lay4flow-remote-版本-windows-x64.zip`：双击 `install.cmd` 的 Windows / Docker Desktop 安装包。
- `lay4flow-remote-版本-windows-x64-setup.exe`：使用同一运行包的 Inno Setup 安装 EXE，安装文件和桌面快捷方式沿用 TkCRM 头像图标。
- `SHA256SUMS`：两份最终资产的 SHA-256。
- `release-notes.md`：从本版更新记录提取的 Release 正文。
- `build-manifest.json`：本地来源提交、文件清单、大小和摘要证据；脏工作区候选不作为正式发布来源。

Windows 安装包使用原生 CMD / PowerShell 脚本，当前未做 Authenticode 签名；这是需要 Docker Desktop 的启动包。更新使用 GitHub 原生 Releases API 与版本资产，不依赖额外 feed 服务。只在全部检查和安装/更新验收通过后发布版本，既有版本资产保持不变。

日常安装与升级步骤见 [Windows 安装指引](WINDOWS_INSTALL.md) 和 [Docker 更新指引](DOCKER_UPDATE.md)。

## 实际部署验收

1. 从空数据目录启动，确认管理登录和远程服务设置可保存。
2. 用 Cloak 直登一个账号，确认身份、会话与固定代理正确。
3. 关闭浏览器、执行正常缓存整理后重开，确认原会话仍有效。
4. 用独立部署的远程有头服务提交一张最小图片，确认验证码、生成和下载成功。
5. 导出会话 JSON，在另一份空数据部署中导入并校验，再确认原身份和会话。

真实生成消耗上游额度；离线测试只证明本地契约与生命周期行为。新的出口、镜像或物理服务器需要重新验收。发布时保留原始 MIT 版权与 NOTICE，产品名称统一为 `lay4flow-remote`。
