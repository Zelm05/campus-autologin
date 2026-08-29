# 校园网自动登录 Campus Auto-Login

**中文** | [English](README_EN.md)

> 掉线自动重连的校园网认证工具，支持 Windows 桌面端与 Android 移动端。
> Auto-relogin campus network authenticator for Windows & Android, built for the Ruijie ePortal gateway (e.g. CQUST).

- 仓库：<https://github.com/Zelm05/campus-autologin>
- 联系：yz050930@gmail.com

## 下载 Download

安装包在 [`release/`](release/) 目录，可直接下载安装：

| 平台 | 文件 | 说明 |
| --- | --- | --- |
| Android | `release/Autologin-v1.0.1_release.apk` | 直接安装，覆盖更新保留账号密码 |
| Windows | `release/Autologin_v1.1.0_x64_setup.exe` | 免管理员权限，支持覆盖升级 |

> 新版 Android APK 也可通过 GitHub Actions 自动构建：打 `v*` 标签 → Release 页面获取。

## 项目结构 Project layout

```
campus-autologin/
├── .github/workflows/      # CI：打 v* 标签自动构建已签名 APK 并发布 Release
├── .gitignore              # 排除签名密钥 / 构建产物 / wheels
├── app/                    # Android 应用模块（Kotlin + Jetpack Compose）
├── windows/
│   └── source/             # Windows 桌面版源码（Python + pywebview）
│       ├── app.py · campus_core.py · daemon.py · gui.py · config.json
│       ├── requirements.txt · README.md · 使用说明.txt · 启动设置.bat · 重建说明.txt
│       └── build/          # 打包配置：CampusLogin.spec · icon.ico · setup.iss · version_info.txt
├── release/                # 可下载的安装包（APK / EXE）
├── screenshots/            # README 界面截图
├── build.gradle.kts        # 根构建脚本
├── settings.gradle.kts     # Gradle 设置
├── gradle.properties       # Gradle 全局属性
├── LICENSE                 # MIT 许可证
├── CHANGELOG.md            # 版本记录
├── README.md               # 本文件（中文）
└── README_EN.md            # English version
```

## 截图 Screenshots

| 📱 Android v1.0.1 · 主界面 | 📱 Android v1.0.1 · 设置页 | 💻 Windows v1.1.0 |
| --- | --- | --- |
| <img src="screenshots/android_v1.0.1_main.png" height="300" alt="Android 主界面"> | <img src="screenshots/android_v1.0.1_settings.png" height="300" alt="Android 设置页"> | <img src="screenshots/windows_v1.1.0_main.png" height="300" alt="Windows 设置主界面"> |

## 功能 Features

| 功能 | Android | Windows |
| --- | :---: | :---: |
| 一键登录 / 注销 | 是 | 是 |
| 掉线自动重连 | 是 | 是（后台守护） |
| 开机自启 | 可选 | 可选 |
| 账号密码本机加密存储 | Keystore 加密 | 本机 config.json 混淆 |
| 双网卡（WiFi+5G）正常 | 是（socket 级绑定） | — |
| 后台常驻监控通知 | 可开关 | 后台守护 |

## Android 版

适配重庆科技大学（锐捷 ePortal）认证网关，校园网参数已内置、无需配置。

- 最低 Android 7.0（API 24），Kotlin + Jetpack Compose + OkHttp
- 下拉刷新 / 网络状态自动刷新 / 浏览器下线自动补登 / 手动下线 5 分钟冷却
- 详见 `app/` 源码注释

### 本地构建

```bash
# JDK 17+；Android SDK（platform-34 + build-tools 34.0.0）
gradle assembleRelease   # 产物 app/build/outputs/apk/release/app-release.apk
```

### 签名与 CI

- 发布签名通过 `keystore.properties`（gitignore，不提交）引用本地 `campus_release.keystore`
- CI（`.github/workflows/`）从仓库 Secrets 还原密钥，打 `v*` 标签自动出已签名 APK 并发布 Release

## Windows 版

Python + pywebview（WebView2）桌面应用，后台守护静默运行、断线自动重连。

- 源码与打包说明见 `windows/source/README.md` 与 `windows/source/重建说明.txt`
- 依赖：`windows/source/requirements.txt`（`pip install -r` 即可）
- 安装包免管理员权限，支持覆盖升级

## 隐私 Privacy

- Android：账号密码仅存本机（Android Keystore 加密），唯一联网目标是校园认证网关
- Windows：账号密码仅存本机 `config.json`（混淆），不上传任何服务器
- 本仓库不包含任何用户个人信息与签名私钥

## 免责声明 Disclaimer

1. 本软件按「现状」提供，仅供**学习与个人合法用途**使用，请遵守所在学校的网络使用规定。
2. 校园网账号、密码等数据**仅存储于本机**（Android Keystore / 本地 config.json），
   **不会上传至任何服务器**（唯一联网目标是校园认证网关）。
3. 因使用本软件造成的封号、断网、违规处罚等后果由使用者本人承担，与作者无关。
4. 请勿将本软件用于任何商业用途或非法用途。

## 许可 License

MIT License. See [LICENSE](LICENSE).
