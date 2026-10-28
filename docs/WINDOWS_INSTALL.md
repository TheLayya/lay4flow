# Windows 一键安装

下载 `lay4flow-remote-版本-windows-x64-setup.exe` 后双击安装，完成后打开桌面上的 **lay4flow-remote**。安装包自带后台服务、Python 运行时、依赖和 Cloak 浏览器，用户无需安装 Docker、Python、Node、Git 或 .NET。ZIP 包也包含同样组件，解压后运行 `install.cmd`。

桌面界面使用 Microsoft Edge WebView2；Windows 10/11 通常已包含，缺失时安装器会引导安装微软官方组件。安装器、桌面程序和快捷方式沿用 TkCRM 同款图标。管理员账号和密码默认均为 `admin`，安装后在设置内修改。

默认安装目录为 `%LOCALAPPDATA%\lay4flow-remote`，服务只监听本机，默认端口 `38000`。端口已被占用时安装器自动选择空闲端口，结果保存在安装目录 `.env`。可指定目录和端口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -InstallDir "D:\lay4flow-remote" -Port 38001
```

安装过程不下载应用依赖，不运行 Docker Compose。应用联网访问 Google 和远程打码服务仍需可用网络；生成 Flow 内容前，在 [打码服务](https://cap.tk218.top/) 申请 API Key 并在工坊设置中填写。

## 本机网络

启动时自动解析 Windows 系统代理和 PAC；显式 PAC 对 Google 登录地址选择 `DIRECT` 时遵循直连。随后探测正在运行的 Clash / Mihomo / sing-box 等代理程序的实际监听端口，通过 HTTP CONNECT 或 SOCKS5 完整握手后接入。端口任意，不要求 TUN。

Windows 原生后台直接连接 `127.0.0.1:实际端口`，并以账号原绑定 SOCKS5 作为最终出口。可在安装目录 `.env` 设置：

```dotenv
LAY4FLOW_HOST_PROXY_MODE=auto
```

`direct` 使用直连；`manual` 配合 `LAY4FLOW_HOST_PROXY=http://127.0.0.1:实际端口` 或 `socks5://127.0.0.1:实际端口`。修改后重新打开桌面端。探测结果存放在 `data/host-network.json`，工坊状态可查看实际端口连通性。

## 账号浏览器

Windows 中的 Cloak 登录直接打开本机原生浏览器窗口。在账号页即可显示窗口、刷新凭据、关闭窗口和完成账号添加。每个账号继续保留独立 Profile、固定代理和身份校验；服务器 Docker 部署继续使用网页账号画面。

使用自己的指纹浏览器登录时，在软件的「账号管理 → 扩展登录」复制当前后台管理地址，在指纹浏览器粘贴打开并登录管理员账号，然后下载该地址专用扩展并完成 Google Flow 登录。地址包含软件实际使用的端口，以软件显示的地址为准。软件内嵌窗口不加载 Chrome 扩展，无需在里面安装或检测扩展；登录结果保存在同一个本机后台，回到软件刷新账号即可看到。Google 登录使用指纹浏览器自己的网络配置，Cloak 新建账号仍可选择代理。

网页容器的更新不会自动更新已安装的 Windows 程序。需要覆盖安装包含这次功能的新安装包，原有配置、账号和 Profile 按既有升级流程保留；无需另外导出账号再导入。

1.5.6 修复旧 Docker 后台与残留原生文件共存时的覆盖升级：安装器按安装目录校验归属后停止原服务，保留其他 Docker 项目；外部程序占用原端口时自动改用空闲端口。若 1.5.5 安装时报本地端口冲突，请使用 1.5.6 安装器覆盖原目录，保留账号与配置。

自 1.5.3 起，正常生成、素材上传、轮询及媒体查询使用已保存的账号 HTTP 会话，不启动本机 Cloak。登录态刷新优先使用 HTTP；登录态缺失或失效时以无头方式恢复。手动登录和主动点击「浏览器」仍打开可见窗口，登录验证及保存完成后自动关闭。

1.5.4 修复覆盖安装时 Cloak Profile 备份超过 Windows 260 字符路径限制导致的校验失败，无需修改系统长路径设置。安装失败的日志会记录具体执行阶段、Python 输出和退出码。

## 启动、停止与更新

桌面快捷方式或 `start.cmd` 启动原生后台和桌面；关闭桌面窗口后已提交后台任务继续处理。`stop.cmd` 正常停止后台并关闭 Profile，`uninstall.cmd` 停止本机后台并移除所属快捷方式，保留 `.env`、`config`、`data`、`tmp`。自定义目录需带 `-InstallDir` 参数。

左下角版本号打开「更新日志」，在同一抽屉检查版本与执行更新。更新包先下载并校验；退出桌面后安装完整应用，后台和资料先备份，通过健康与版本检查后重新打开桌面。失败时恢复之前的运行文件和资料。更新前等待生成、导入和账号登录操作结束。

再次运行同版安装包刷新程序文件；新版安装包升级后台和桌面，保留现有配置与账号资料；旧版安装包退出并保留现有安装。旧 Docker 桌面版覆盖升级时会沿用原安装目录的数据，先停止其所属容器再启动原生后台，其他 Docker 项目保持原位。

安装日志：`%TEMP%\lay4flow-remote-install.log`。本机后台日志：安装目录 `data\native-service.log` 及 `.error`；桌面菜单「应用 → 启动日志」可查看启动详情。

## 维护者打包

先构建前端并通过项目检查，再构建后端和桌面运行时：

```powershell
python scripts/build_native_runtime.py desktop/runtime/backend/windows-x64 --cloak-binary "已验证的Cloak目录/chrome.exe"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_desktop.ps1
python scripts/build_release.py dist/1.5.1 --desktop-runtime desktop/runtime/windows-x64 --backend-runtime desktop/runtime/backend/windows-x64
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_setup.ps1 -BundleZip dist/1.5.1/lay4flow-remote-1.5.1-windows-x64.zip -OutputDir dist/1.5.1
```

应用运行包、Python 和 Cloak 只包含公开组件，账号、密钥、Profile、数据库、日志和生成内容均排除在发布包之外。ZIP、运行包与 EXE 的 SHA-256 随构建清单保存。安装器当前未做 Authenticode 签名。
