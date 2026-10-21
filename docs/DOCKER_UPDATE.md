# Docker 更新与回滚

安装目录由运行实例自己保存，默认 Compose 项目名是 `lay4flow-remote`。更新脚本只替换公开运行文件和应用镜像，保留 `.env`、`config/setting.toml`、`data/`、`tmp/` 以及现有卷。脚本不会执行 `docker compose down -v`、卷删除或 `docker system prune`。

## 在线更新

在安装目录执行：

```bash
./scripts/update.sh --install-dir /srv/lay4flow-remote
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update.ps1 -InstallDir $PWD
```

可用 `--version 1.2.0`（PowerShell 使用 `-Version 1.2.0`）固定版本。脚本先下载 GitHub Release 的 `lay4flow-remote-<version>-runtime.tar.gz` 和 `SHA256SUMS`，在候选目录检查 SHA-256、路径穿越和 `VERSION`，再构建候选镜像。下载或校验失败时，当前实例保持原状。

切换前会写入 `data/update-maintenance`。服务端会拒绝新的写入请求，脚本检查运行中的生成和 Profile 导入任务，任务未清空时退出并移除标记。单实例 Compose 重建会有短暂中断；健康检查 `/health` 和版本字段通过后才记录成功。

第一次从没有更新标记的旧版本升级时，旧容器只会提供基础 `/health` 检查；请先等待所有生成、Profile 导入和浏览器登录操作结束，再运行更新。若旧版本没有 `scripts/update_runtime.py`，从发布包复制 `scripts/update.ps1`、`scripts/update.sh` 和 `scripts/update_runtime.py` 到安装目录后再执行。旧版本回滚沿用当前更新器和 `.lay4flow-updates/backups/`，不会依赖已被替换的容器文件。

使用自定义 Compose 项目名时，在 `.env` 设置与实际实例一致的 `COMPOSE_PROJECT_NAME`。配置中的数据库、Profile 或 Vids 路径若放在默认 `data/` 之外，先单独备份这些外部目录。

## 回滚

每次成功切换前，备份会写入 `.lay4flow-updates/backups/`。失败的候选会自动恢复之前的公开文件、镜像和切换前数据快照；随后执行健康检查。手动回滚最近一次备份（应用回滚保留当前 `data/` 和 `tmp/` 写入）：

```bash
./scripts/update.sh --install-dir /srv/lay4flow-remote --rollback
powershell -ExecutionPolicy Bypass -File .\scripts\update.ps1 -InstallDir $PWD -Rollback
```

手动回滚保留当前 `data/` 与 `tmp/`，避免丢掉升级之后产生的新数据；自动失败恢复会使用切换前快照。备份目录包含账号资料、配置和数据库等私密数据，请与现有 `data/` 一样保管，勿上传至 Git 或公共工单。自动恢复会同时保留候选启动后产生的数据目录，供故障排查。

## 离线候选

验收时可以把本地资产传给同一入口，避免依赖网络：

```bash
./scripts/update.sh --install-dir ./deploy \
  --version 1.2.0 \
  --source-archive ./lay4flow-remote-1.2.0-runtime.tar.gz \
  --checksum-file ./SHA256SUMS
```

`scripts/update_runtime.py` 是更新器使用的无网络标准库校验器。它只允许发布清单中的公开路径，拒绝符号链接、重复条目、私密文件和归档越界路径。

## 验证范围

离线校验覆盖 SHA-256 错误、路径穿越、私密文件拒绝、软链接路径、活动任务检查和备份恢复。Windows Docker Desktop 的隔离 Compose 实例已验证旧镜像升级、校验失败保持原实例、并发锁、候选健康失败自动恢复和手动回滚；未执行真实内容生成。Linux Shell 入口已通过语法检查和离线 Docker/curl 事务测试，真实 Linux 主机升级验收另行记录。
