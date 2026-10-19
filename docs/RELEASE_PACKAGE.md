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
```

运行包使用固定文件清单与公开源码/静态资源扩展名白名单。包含 `src/`、`static/`、启动脚本、示例配置、运行文档、README 引用的演示截图及许可证；前端源码与完整开发测试留在源码仓库。截图仅含合成数据，逐文件加入公开清单。压缩包不包含私有 TOML、`.env`、`data/`、`tmp/`、本地账号 JSON、日志、证据目录、密钥或 Python 缓存。

打包脚本输出版本、文件清单与 SHA-256；发布者应保留清单并核对压缩包内容。CI 在 Linux 与 Windows 运行离线检查，在 Linux 构建镜像、断网烟测和生成运行包。未上传到 GitHub 的本地准备结果不等于已公开发布。

## 实际部署验收

1. 从空数据目录启动，确认管理登录和远程服务设置可保存。
2. 用 Cloak 直登一个账号，确认身份、会话与固定代理正确。
3. 关闭浏览器、执行正常缓存整理后重开，确认原会话仍有效。
4. 用独立部署的远程有头服务提交一张最小图片，确认验证码、生成和下载成功。
5. 导出会话 JSON，在另一份空数据部署中导入并校验，再确认原身份和会话。

真实生成消耗上游额度；离线测试只证明本地契约与生命周期行为。新的出口、镜像或物理服务器需要重新验收。发布时保留原始 MIT 版权与 NOTICE，产品名称统一为 `lay4flow-remote`。
