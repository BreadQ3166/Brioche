# Brioche 0.5

轻量模块化远程桌面：局域网和 Tailscale 外网可选，支持客户端及局域网浏览器控制，连接后可以切换远端显示器。

当前已在 Ubuntu 22.04 Xorg 验证。Windows 11 与 macOS 12 为适配目标，尚未真机验收。

## 最简单的使用方法

原生连接建议两台电脑均使用 **Brioche 0.5**；0.2/0.3 原生客户端仍可互通，0.1 协议不兼容。浏览器连接只需在被控电脑安装 0.5，控制设备无需安装 Brioche。

### 局域网

1. 被控端进入“共享这台电脑”，选择“局域网”。
2. 软件自动读取本机地址和显示器。一般保持默认，多张网卡时选择另一台电脑能访问的地址。
3. 点击“开启共享”，再点击“复制连接码”。
4. 控制端点击“粘贴连接码”，再点击“连接远程桌面”。连接方式会根据码中地址自动选择。

局域网不需要 Tailscale 或云服务器。网络隔离和防火墙仍可能阻止互访，默认共享端口是 TCP 47995。

### 内置 Tailscale 外网（0.5 新增）

无需另外安装 Tailscale、配置虚拟网卡或执行管理员命令。

1. 两台电脑安装 Brioche，在软件中点击 **“登录 Tailscale”**。
2. 在系统浏览器打开的 Tailscale 官方页面完成授权，两端使用同一账号、同一 tailnet；若开启设备审批，需要在管理页批准。
3. 被控端进入“共享这台电脑”，选择“外网 · Tailscale”，选择仅查看或允许控制，点击“开启共享”。这会允许同一账号的设备访问该桌面。
4. 控制端选择“外网 · Tailscale”，从下拉框选择对方在线设备，点击“连接远程桌面”。**不必复制连接码**。
5. 停止共享会撤下发现信息、关闭入站连接。点击“账号与登录 → 退出登录”会结束外网共享与连接。

内置的是 Go/tsnet 用户态节点，由 Brioche 自动管理生命周期，网络身份与系统 Tailscale 独立。普通用户不需要安装 Go；Go 只在开发构建时使用。登录状态保存在本用户应用数据目录，重启后自动恢复；不内置共用 auth key，不在软件里收集 Tailscale 密码。

当前只允许同一 Tailscale 用户的无标签设备互通，且仍受 tailnet ACL/grants 限制；同一 tailnet 内的其他用户和标签设备不会因此获得控制权。列表只展示在线的 `brioche-` 节点；对方仍须开启 Brioche 共享。仅网络在线不代表已允许远程控制。

仍依赖 Tailscale 官方的账号、协调和中继服务。优先直连，无法直连时使用中继；工具栏显示实际活动路径，未确认时显示待检测。内置并不会保证全球网络均可直连或保证中继高刷性能。局域网/回环继续免登录。

可保留粘贴连接码的方式，外网共享固定使用 tailnet TCP 47995，发现服务为 tailnet TCP 47994；它们不是系统网卡的监听端口。真正的原生 TLS Host 仅绑定随机本机回环端口。原生证书指纹、临时令牌、输入权限和单活动连接校验保持有效。

**平台范围：**本轮完成 Ubuntu 22.04 的构建与测试。内置 SDK 固定 Tailscale v1.104.7、Go 1.27.4；该 Go 版本的 macOS 最低要求是 13，**本版内置网络不支持 macOS 12**，不能据此声称原 macOS 12 目标已完成。macOS 12 需要单独的兼容后端与安全评估；Windows 11/macOS 的整包仍需真机验收。

验证包括本机测试协调服务器下的两个真实 tsnet 节点、同账号传输及跨账号拒绝，并实测获取官方 HTTPS 登录链接。真实账号授权需要用户完成；真实公网双机穿透与中继性能尚未验收。

### 本机回环

在“共享这台电脑”选择“本机回环测试”，建议先选“仅查看画面”，开启共享并复制连接码，再切换到“连接电脑”粘贴连接。

## 用浏览器远程控制

1. 在被控电脑打开“共享这台电脑”，选择“局域网”。
2. 勾选 **“允许局域网浏览器访问”**，选择访问权限并点击“开启共享”。
3. 点击 **“复制浏览器链接”**，将完整链接通过可信渠道传递到另一台局域网设备。
4. 在浏览器中打开完整的 `https://本机地址:47996/#访问码` 链接。首次访问使用本机自签名证书：先确认地址是被控电脑，可在 Brioche“首次访问说明”中查看证书 SHA256，与浏览器证书详情核对后继续。
5. 点击网页中的 **“开始远程”**。默认先查看；需要操作时点击 **“启用控制”**。
6. 顶部可切换显示器，点击“释放输入”或按 Ctrl+Alt+Shift 退出控制，点击“断开”结束会话。

同机浏览器测试时选择“本机回环测试”，勾选浏览器访问并打开复制的链接即可。浏览器只支持访问所选接口的准确地址，请使用软件复制的链接，不要改成其他主机名，也不要遗漏 `#` 后的访问码。页面载入后会移除地址栏中的访问码；需要给另一台设备分享时应重新从 Brioche 复制链接。

浏览器页及所有脚本样式随软件提供，无外部 CDN，不需要云服务器或 Tailscale。防火墙需允许所选的 HTTPS 端口（默认 TCP 47996）；被控电脑必须保持开启并运行共享。

浏览器默认采用 HTTPS + WebSocket + H.264 / WebCodecs，与原生客户端共用 H.264 采集编码模块，支持 720p/1080p/2K，帧率可选 30/60 FPS。网页会探测解码能力；不支持 H.264 或实际解码失败时，自动回退独立 JPEG 帧（最高 30 FPS），也可以手动选择模式。帧率上限不代表实际达到；90/144 FPS 高刷目标仍属于原生客户端。Chrome 已做真实浏览器测试，Edge/Firefox/Safari 与移动设备尚需分别验证。

访问码与原生连接码分别生成，停止共享后失效。HTTPS 使用本机证书，不会自动向操作系统安装受信根证书。网页默认要求主动启用控制；只读共享不能通过修改网页提升为可控制。切屏、页面失焦和断开会释放按键。

网页和原生客户端共用一次一个活动连接的限制，不支持多人同时查看。浏览器入口当前仅在局域网和回环模式提供；Tailscale 外网继续使用原生客户端。网页首版未实现音频、文件传输、剪贴板、输入法组合输入、相对鼠标或全部系统快捷键。

## 多显示器

- 共享端可选择默认显示器。
- 连接后在远程窗口顶部直接选择显示器，不需要断开、重新配对或复制新码。
- 一次只采集和编码选中的一个屏幕，减少 CPU 与带宽占用。
- 切换时暂停输入、释放按键并重建视频编解码流，更新鼠标坐标；旧屏幕的延迟输入不作用于新屏幕。
- 横屏、竖屏自动适配：2K 档上限分别为 2560×1440 和 1440×2560，保持比例，不放大较小屏幕。
- 无法打开新屏幕时保留原画面；显示器插拔后请停止并重新开启共享以刷新列表。
- 不支持多个远程屏幕同时拼接显示，也没有虚拟显示驱动。

## 简洁设置

默认只显示连接方式、连接码和画质预设：均衡 2K60、高刷 2K90、极致 2K144。

分辨率、帧率、码率、端口和编码器位于“高级设置”。远程工具栏的“性能”按钮展开详细统计；Ctrl+Alt+Shift 释放键鼠，F11 切换全屏。

连接码包含访问令牌，仅通过可信渠道传递。停止共享会结束会话，再次开启会生成新令牌。软件仅监听所选网络地址，不默认同时开放所有网卡。

## GitHub 自动构建

仓库的 **Actions → Build Brioche** 会在云端 Windows 和 Ubuntu 分别测试、打包。
成功运行的 **Artifacts** 中可下载 Windows EXE 便携 ZIP、Ubuntu DEB 和校验文件。
Windows 必须解压完整目录后运行 `Brioche.exe`，不能只复制 EXE。
操作步骤与测试边界见 [GitHub 构建说明](docs/GITHUB_BUILDS.md)。

## 启动与安装

源码启动：

```bash
cd Brioche
./scripts/run.sh
```

已安装的用户级版本：

```bash
~/.local/bin/brioche
```

源码环境使用 Python 3.10–3.12：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Ubuntu 22.04 系统依赖通常包括 `python3-venv libxcb-cursor0 libxcb-xinerama0 libxkbcommon-x11-0`。被控端从已登录的 Xorg 桌面启动，不能用 root 绕过桌面授权。

Windows 使用 Python 3.11 创建 `.venv` 后，执行 `.venv\Scripts\python.exe -m pip install -e .` 和 `.venv\Scripts\python.exe -m brioche`。

macOS 使用兼容 macOS 12 的 Python 3.11，按系统要求授予屏幕录制与辅助功能权限。Intel 与 Apple Silicon 尚需分别实机验证，锁定 Qt 版本不等于完成整个依赖链验收。

构建产物已包含 Python、Qt、视频运行库和内置网络模块，普通启动不需要额外安装 Python、FFmpeg、Sunshine 或 Moonlight。硬件编码需要媒体库与显卡驱动共同支持，当前环境只有软件编码可用。

## 开发与验证

```bash
.venv/bin/python -m unittest discover -s tests -v
# 安装 xvfb 后，在隔离桌面运行输入、切屏、Qt 与打包程序测试
.venv/bin/python scripts/check_linux.py --source-only
# 开发环境可选：需安装 playwright，使用系统 Google Chrome，隔离桌面测试网页操作
.venv/bin/python scripts/check_linux.py --browser-only
.venv/bin/python scripts/build_network.py  # 开发机需要 Go 1.27.4+
.venv/bin/python scripts/build.py
.venv/bin/python scripts/check_linux.py
# 真实物理显示器的只读切屏回环，不注入输入
.venv/bin/python scripts/test_real_displays.py
```

完整测试包含：网络模式绑定、Tailscale 缺失/未登录/用户态模式、状态解析、横竖屏切换、键鼠释放、旧坐标丢弃、失败回退、证书与令牌校验，以及原有 TLS/H.264 桌面链路。

```bash
.venv/bin/python -m pip install pyinstaller==6.11.1
.venv/bin/python scripts/build_network.py
.venv/bin/python scripts/build.py
.venv/bin/python scripts/package_linux.py
.venv/bin/python scripts/package_source.py
```

在 Windows/macOS 上分别构建，不能从 Linux 交叉生成可用的签名安装包。

用户级安装脚本先验证 releases/SHA256SUMS 中对应 DEB 的校验值，再安装到 `~/.local/opt/brioche/0.5.0` 并切换启动入口；保留旧版本目录用于回退：

```bash
.venv/bin/python scripts/install_user.py
```

## 模块与边界

- `network.py`：局域网接口、旧系统 Tailscale 状态解析（兼容工具）。
- `embedded_network.py` / `network-helper/`：内置 tsnet、私有 IPC、登录、同账号设备发现和流转发。
- `web_gateway.py` / `web/`：HTTPS/WebSocket 适配、临时网页访问码及浏览器客户端。
- `main_window.py`：共享与连接流程、后台环境检查。
- `ui.py`：远程画布、显示器切换和性能工具栏。
- `session.py`：认证、会话、切屏、输入流序号与清理。
- `protocol.py`：有界协议及版本化消息。
- `capture.py` / `media.py` / `h264.py`：采集、统一 H.264 编码、封装与解码后端。
- `web_video.py`：网页流控、解码代次和 IDR 恢复。
- `platform_input.py` / `security.py`：平台输入与 TLS 身份。

当前为 Python/Qt 开发原型，使用 TLS/TCP；CPU 帧转换和软件解码仍限制高刷表现。已有 0.1 本机短时软件编解码测试约 57–59 FPS，不能将 90/144 的设置上限视为实际性能保证。

浏览器适配复用同一个 Host 的采集、编码、输入、权限与切屏，H.264 数据无需转成 JPEG。`h264.py` 统一 Annex B / AVCC 封装；`web_video.py` 管理浏览器帧确认与关键帧恢复；`web/video.js` 负责 WebCodecs 解码和兼容模式。H.264 最多四个未确认帧，拥塞跳过参考帧后等待完整 IDR 并重建解码器，避免花屏；JPEG 最多一个未确认帧。兼容模式直接从采集帧编码 JPEG，没有 H.264 二次转码。

H.264 是当前兼容性、码率和实现复杂度的默认取舍，不代表所有场景最优。原生 TLS/TCP 与网页 WSS 仍分别适配传输和呈现，未引入 WebRTC；弱网络中的 TCP 队头阻塞仍可能增加延迟。浏览器是否实际使用硬件解码由设备和浏览器决定，不能仅凭 WebCodecs 可用就认定启用硬件加速。

本机 0.4 短测结果和测量范围见 `artifacts/browser-performance-04.json`；这不替代动态画面、双机和长时间测试。

命令行可通过明确地址开启浏览器入口（终端会输出含访问权限的链接，不要把它提交或放入共享日志）：

```bash
./scripts/run.sh host --bind 127.0.0.1 --web --web-port 47996
```

尚未实现音频、剪贴板、文件传输、网络断线自动重连、系统登录前控制、GPU 零拷贝、原生硬件解码、自动更新和签名发行。Tailscale 真实跨网尚未验证；状态解析测试不代替真实跨网测试。

许可说明见 THIRD_PARTY.md。Brioche 没有复制 Sunshine/Moonlight 源码，也不与其桌面串流协议互通。0.5 新增链接 Tailscale 开源库，相关许可随内置网络模块提供。

开发机磁盘紧张时，可用 `scripts/build.py --output-dir /dev/shm/brioche-build/dist --work-dir /dev/shm/brioche-build/work` 构建；测试后执行 `scripts/install_user.py --bundle /dev/shm/brioche-build/dist/Brioche --reuse-libraries`。该模式安装本地已验证构建，只复用上一版本字节完全相同的只读原生库；新代码和配置独立复制，不删除旧版本。状态始终写入用户数据目录。
