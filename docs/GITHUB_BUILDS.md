# 在 Ubuntu 上发起 Windows / Ubuntu 自动构建

本地继续使用 Ubuntu。GitHub Actions 分别在云端 Windows Server 2022 x64 和
Ubuntu 22.04 x64 执行测试及打包，不要求本机安装 Windows。

## 下载 Windows 程序

1. 打开仓库的 **Actions → Build Brioche**。
2. 选择最近一次成功运行（绿色勾）。
3. 在 **Artifacts** 下载 `Brioche-Windows-x64`，下载通常需要登录 GitHub。
4. 解开下载的构建产物，再解开其中的 `Brioche-<版本>-windows-x64.zip`。
5. 保留完整 `Brioche` 文件夹，双击 **Brioche.exe**。

这是便携包，不是单文件安装器。`_internal` 包含 Qt、视频库和内置 Tailscale，
不能只复制 EXE。最终用户不需要安装 Python、Go 或 Tailscale。
当前构建未做 Windows 代码签名；构建成功不代表已完成真实桌面兼容验收。

## 下载 Ubuntu 程序

同一运行中的 `Brioche-Ubuntu-x64` 提供 `brioche_<版本>_amd64.deb`。
两个平台的构建产物均带源码 ZIP 和 `SHA256SUMS` 校验文件。

## 发起构建

- 推送代码到 `main` 时自动构建。
- 在 **Actions → Build Brioche → Run workflow** 可手动构建。
- Pull Request 也运行检查。

工作流使用只读仓库权限，不自动创建公开 Release；下载包保留30天，
测试报告保留14天。需要新包时可以再次手动构建。未配置收费服务或自托管运行器。

## 验证范围

Linux 在隔离 Xvfb 桌面运行现有远程桌面测试，并验证打包后的 TLS/H.264、
多屏、输入释放及网页 H.264/JPEG。Windows 运行平台无关测试、真实 tsnet
本地双节点测试，以及打包后的 Qt/视频/TLS/网页资源/网络模块加载检查。
这些检查不会将云端构建机当作无人值守被控电脑，也不会登录用户的 Tailscale 账号。

**Windows 11 真实桌面采集、鼠标键盘、显卡硬件编码和外网双机连接仍需实机测试。**
macOS12 不包含在本工作流中；当前内置网络 SDK 不支持该系统。

## 在开发机自行构建

使用 Python3.11 和 Go1.27.4+，在对应系统运行：

```text
python -m pip install -e . pyinstaller==6.11.1
python scripts/build_network.py
python scripts/build.py
```

Windows：`python scripts/package_windows.py`；Ubuntu：
`python scripts/package_linux.py`。构建配置位于 `.github/workflows/build.yml`。
