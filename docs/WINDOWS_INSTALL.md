# Windows 一键安装

下载 `lay4flow-remote-版本-windows-x64-setup.exe`，双击安装；也可解压 `lay4flow-remote-版本-windows-x64.zip` 后双击 `install.cmd`。EXE 安装程序和桌面启动、更新快捷方式使用与 TkCRM 相同的头像图标。安装器会校验内置运行包、构建 Docker 镜像、启动服务，等待健康检查通过后打开管理页，并创建桌面启动与更新快捷方式。默认管理员账号和密码均为 `admin`，登录后在后台修改。

首次使用先安装 [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)，启用 WSL 2 和 Linux containers，完成 Docker Desktop 的初始设置。安装器会尝试启动已安装的 Docker Desktop；安装包依赖 Docker，不要求主机安装 Python、Node 或 Git。首次构建需要联网下载基础镜像和依赖，后续启动直接复用已有镜像。

默认安装目录为 `%LOCALAPPDATA%\lay4flow-remote`，本机访问地址为 `http://127.0.0.1:38000`。服务仅监听本机，安装器也会检查实际运行版本。端口占用时可在 PowerShell 指定目录和端口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -InstallDir "D:\lay4flow-remote" -Port 38001
```

生成内容前，在管理后台填写远程打码 API Key；在 [打码服务](https://cap.tk218.top/) 申请。Docker 容器拥有独立网络；需要代理时使用容器可访问的代理地址，例如 `host.docker.internal:7890`，再在代理池绑定账号。

## 启动、停止与卸载

双击解压目录中的 `start.cmd` 启动并打开管理页，`stop.cmd` 停止服务，`uninstall.cmd` 移除容器和桌面快捷方式。卸载保留安装目录及 `.env`、`config`、`data`、`tmp`；确认备份后自行删除目录即可完整清理。上述入口默认管理默认安装目录，自定义目录需带参数，例如：

```powershell
.\stop.cmd -InstallDir "D:\lay4flow-remote"
.\uninstall.cmd -InstallDir "D:\lay4flow-remote"
```

再次运行安装器会保留现有安装并启动原版本。升级使用桌面的 `lay4flow-remote update` 快捷方式，或 `update.cmd -InstallDir "D:\lay4flow-remote"`。更新先下载并校验固定版本源码、构建候选镜像，再备份数据并切换容器，健康或版本检查失败则回滚。切换期间会短暂中断服务，进行中的生成任务应先完成。

## 打包

维护者先构建前端并通过项目检查，再运行：

```powershell
python scripts/package_windows.py dist/lay4flow-remote-1.2.0-windows-x64.zip
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_setup.ps1 -BundleZip dist/lay4flow-remote-1.2.0-windows-x64.zip -OutputDir dist/setup
```

ZIP 使用公开运行时白名单，包含 MIT 和 NOTICE，不包含本机账号、密码、数据库、浏览器 Profile、日志或生成内容。`runtime/SHA256SUMS` 校验内置运行包；整个 ZIP 的 SHA-256 由打包命令输出，并随 Release 发布。EXE 使用 Inno Setup 6 包装同一份运行包，依赖 Docker Desktop，当前未做 Authenticode 签名。
