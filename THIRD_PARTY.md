# 来源与依赖

Brioche 0.5 的新增应用代码为本项目独立实现，没有提取 Sunshine、Moonlight 源文件；网络模块新增链接 Tailscale tsnet 开源库。设计阶段参考这些项目的职责划分，当前不实现 Sunshine/Moonlight 的互通协议。

运行时依赖包括：

| 组件 | 用途 | 许可说明 |
|---|---|---|
| PySide6 Essentials / Qt / Shiboken | 原生桌面界面 | 上游提供 LGPL/GPL/商业许可选项，保留实际包许可 |
| PyAV | FFmpeg Python 绑定 | BSD 类许可，底层 FFmpeg 及各编解码器另有许可 |
| FFmpeg / libx264 | 视频编解码 | 实际 wheel 构建包含的库及 GPL/LGPL 条件需逐项保留 |
| Tailscale tsnet v1.104.7 及其链接依赖 | 内置用户态网络、穿透与设备身份 | 主库 BSD-3-Clause，其他链接库按实际模块许可；见 network/licenses 和 DEPENDENCIES.txt |
| python-mss | 屏幕采集 | MIT |
| python-xlib | Linux 输入 | LGPL |
| pynput（非 Linux） | Windows/macOS 输入 | LGPL |
| cryptography / cffi | 证书与密码库绑定 | 按上游包中许可文件 |
| aiohttp 及其依赖 | 本机 HTTPS / WebSocket 浏览器入口 | 按上游 Apache/BSD/MIT 等实际包许可文件 |
| libxcb-cursor0（Linux 附带） | Qt X11 依赖 | 保留 Ubuntu 包 copyright |
| PyInstaller | 开发打包工具 | GPL，带引导器分发例外，见工具许可 |

构建脚本收集到的依赖文件不自动等于完成所有许可义务；公开分发前需提供实际二进制对应的许可、构建来源及适用的源码获取方式。开发构建允许检查与替换独立共享库，未做加密封装。

原型当前使用固定的 Qt/PyAV/MSS 版本验证兼容性。后续正式发行需更新依赖并重新做平台、漏洞与许可证审计，不把原型锁定版本视为长期安全维护策略。

Playwright 和系统 Google Chrome 仅用于开发时的浏览器自动化验证，不属于 Brioche 应用运行依赖，也不随应用包分发。


内置网络源码及 go.mod/go.sum 随源码包提供。官方云服务的账号与服务条款独立于开源客户端许可；本软件没有内置账号、共享 auth key 或第三方 OAuth secret。
