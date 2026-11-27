# Brioche Android Client

原生 Kotlin 编写的 Brioche 安卓远程桌面客户端（支持 Android 8.0+ / API 26–34），与 Ubuntu / Windows / macOS 桌面端完全协议互通。

## 核心能力
- **零拷贝硬件解码**：Android `MediaCodec`（H.264 / HEVC + 低延迟标志）直连 `SurfaceView` 渲染，省电低延迟。
- **局域网自动发现与六位码配对**：监听组播 `239.255.47.95:47993` 自动发现局域网内开启共享的 Brioche 电脑；支持 SHA-256 证书钉扎与六位配对码首次配对。
- **指定局域网 IP 直连 & `brioche://` 连接码**：既可直接输入内网 IP（或 Tailscale IP）连接，也支持直接打开/粘贴 `brioche://` 连接凭据。
- **移动端双触控操作模式**：
  - **触控板模式（默认）**：单指滑动精准控光标、单指轻点左键、双指轻点右键、双指上下滚动；
  - **直触模式**：指哪点哪；
  - 内置悬浮工具栏、多屏切换、虚拟键盘桥接、快捷组合键（`Esc`、`Tab`、`Ctrl+C/V`、`Alt+Tab`、`Win/Cmd`、`Ctrl+Alt+Del`）、剪贴板同步与断线自动重连。

## 本地编译
```bash
gradle -p android testDebugUnitTest assembleRelease
# 输出：android/app/build/outputs/apk/release/app-release.apk
```
