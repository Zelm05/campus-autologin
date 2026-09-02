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
| Android | `release/Autologin-v1.0.3_release.apk` | 直接安装，覆盖更新保留账号密码；旧版 v1.0.1 仍保留在本目录 |
| Windows | `release/Autologin_v1.2.0_x64_setup.exe` | 免管理员权限，支持覆盖升级；点「启动后台服务」即自动设为登录后自启，「极速启动」可选实现开机即联网（锁屏也运行） |

> 新版 Android APK 也可通过 GitHub Actions 自动构建：打 `v*` 标签 → Release 页面获取。

## ⚠️ 使用提醒（务必开启自启动）

**本工具依靠自启动常驻后台，未开启则掉线时不会自动重连：**

- **Windows**：打开软件 → 填账号 → 保存 → 点「**▶ 启动后台服务**」（自动开启"登录
  Windows 后自动运行"，免管理员）；若想开机未登录/锁屏时也联网，再勾选「**极速启动**」。
- **Android**：需在**系统设置里手动开启本应用的「自启动 / 后台运行」**才有效
  （各品牌路径不同，一般在「设置 → 应用管理 → 本应用 → 自启动 / 耗电管理」，小米/华为/
  OPPO/vivo 等默认会限制后台），并保持常驻——被系统杀掉后无法自动补登；不介意通知栏
  可关闭「监控中」通知权限。

## 项目结构 Project layout

```
campus-autologin/
├── .github/workflows/      # CI：打 v* 标签自动构建已签名 APK 并发布 Release
├── .gitignore              # 排除签名密钥 / 构建产物 / wheels
├── app/                    # Android 应用模块（Kotlin + Jetpack Compose）
├── windows/
│   └── source/             # Windows 桌面版源码（Python + pywebview）
│       ├── app.py · campus_core.py · daemon.py · gui.py   # config.json 等运行期文件不入库
│       ├── requirements.txt · README.md · 使用说明.txt · 启动设置.bat
│       └── build/          # 打包配置：CampusLogin.spec · icon.ico · setup.iss · build.bat · version_info.txt
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

| 📱 Android v1.0.1 | 💻 Windows v1.1.0 |
| --- | --- |
| <img src="screenshots/android_v1.0.1_main.png" height="295" alt="Android 主界面"> <img src="screenshots/android_v1.0.1_settings.png" height="295" alt="Android 设置页"> | <img src="screenshots/windows_v1.1.0_main.png" height="295" alt="Windows 设置主界面"> |

## 隐私 Privacy

- Android：账号密码仅存本机（Android Keystore 加密），唯一联网目标是校园认证网关
- Windows：账号密码仅存本机 `config.json`，不上传任何服务器
- 本仓库不包含任何用户个人信息与签名私钥

## 免责声明 Disclaimer

1. 本软件按「现状」提供，仅供**学习与个人合法用途**使用，请遵守所在学校的网络使用规定。
2. 校园网账号、密码等数据**仅存储于本机**（Android Keystore / 本地 config.json），
   **不会上传至任何服务器**（唯一联网目标是校园认证网关）。
3. 因使用本软件造成的封号、断网、违规处罚等后果由使用者本人承担，与作者无关。
4. 请勿将本软件用于任何商业用途或非法用途。

## 许可 License

MIT License. See [LICENSE](LICENSE).
