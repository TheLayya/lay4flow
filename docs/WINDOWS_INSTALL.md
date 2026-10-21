# Windows 一键安装

下载 `lay4flow-remote-版本-windows-x64-setup.exe`，双击安装；也可解压 `lay4flow-remote-版本-windows-x64.zip` 后双击 `install.cmd`。安装完成后启动 **Lay4Flow 桌面应用**，在独立窗口内使用创作工作台。EXE、桌面启动和更新快捷方式使用与 TkCRM 相同的头像图标。默认管理员账号和密码均为 `admin`，登录后在后台修改。

首次使用先安装 [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)，启用 WSL 2 和 Linux containers，完成 Docker Desktop 的初始设置。桌面窗口使用 Microsoft Edge WebView2 Runtime；安装器检查并引导安装缺失组件。桌面程序自带 .NET 运行时，不要求主机安装 .NET、Python、Node 或 Git。首次构建需要联网下载基础镜像和依赖，后续启动直接复用已有镜像。

桌面菜单的「更新日志」可直接打开应用内抽屉；侧栏和用户菜单也有相同入口。抽屉显示当前版本与逐版变更，「检查更新」手动查询 GitHub 稳定版。关闭窗口后 Docker 后台任务继续执行，再次打开可查看已有任务。

正式桌面包由 Windows 构建机生成，内含 `Lay4Flow.Desktop.exe` 和自包含 .NET 运行时；ZIP 源码启动包不含该二进制。

默认安装目录为 `%LOCALAPPDATA%\lay4flow-remote`，本机地址默认 `http://127.0.0.1:38000`。EXE 安装时若已有同名 Docker 项目，会为新目录选择独立项目；默认端口被占用时选择下一个可用端口，结果保存在安装目录 `.env` 和日志中。服务仅监听本机。也可在 PowerShell 指定目录和端口：

安装失败时，安装器会显示具体原因并打开完整 PowerShell/Docker 日志：`%TEMP%\lay4flow-remote-install.log`。已创建安装目录时也保留 `.lay4flow-install.log`。手工指定的项目名或端口发生冲突时会退出并提示，原实例与数据保持原位。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -InstallDir "D:\lay4flow-remote" -Port 38001
```

生成内容前，在管理后台填写远程打码 API Key；在 [打码服务](https://cap.tk218.top/) 申请。Docker 容器拥有独立网络；需要代理时使用容器可访问的代理地址，例如 `host.docker.internal:7890`，再在代理池绑定账号。

## 启动、停止与卸载

双击解压目录中的 `start.cmd` 启动桌面应用，`stop.cmd` 停止服务，`uninstall.cmd` 移除容器和桌面快捷方式。卸载保留安装目录及 `.env`、`config`、`data`、`tmp`；确认备份后自行删除目录即可完整清理。上述入口默认管理默认安装目录，自定义目录需带参数，例如：

```powershell
.\stop.cmd -InstallDir "D:\lay4flow-remote"
.\uninstall.cmd -InstallDir "D:\lay4flow-remote"
```

再次运行安装器会保留现有安装并启动原版本。升级使用桌面的 `lay4flow-remote update` 快捷方式，或 `update.cmd -InstallDir "D:\lay4flow-remote"`。更新先下载并校验固定版本源码、构建候选镜像，再备份数据并切换容器，健康或版本检查失败则回滚。切换期间会短暂中断服务，进行中的生成任务应先完成。

## 打包

维护者先构建前端并通过项目检查，再运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_desktop.ps1
python scripts/package_windows.py dist/lay4flow-remote-1.3.0-windows-x64.zip --desktop-runtime desktop/runtime/windows-x64
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_setup.ps1 -BundleZip dist/lay4flow-remote-1.3.0-windows-x64.zip -OutputDir dist/setup
```

ZIP 使用公开运行时白名单，包含 MIT 和 NOTICE，不包含本机账号、密码、数据库、浏览器 Profile、日志或生成内容。`runtime/SHA256SUMS` 校验内置运行包；整个 ZIP 的 SHA-256 由打包命令输出，并随 Release 发布。EXE 使用 Inno Setup 6 包装同一份运行包，依赖 Docker Desktop，当前未做 Authenticode 签名。
