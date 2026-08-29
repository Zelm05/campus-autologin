# 校园网自动登录 Campus Auto-Login

> 掉线自动重连的校园网认证工具，支持 Windows 桌面端与 Android 移动端。
> Auto-relogin campus network authenticator for Windows & Android, built for the Ruijie ePortal gateway (e.g. CQUST).

- 仓库：<https://github.com/Zelm05/campus-autologin>
- 联系：yz050930@gmail.com

## 项目结构 Project layout

```
campus-autologin/
├── app/                    # Android 应用模块（Kotlin + Jetpack Compose）
├── windows/
│   ├── source/             # Windows 桌面版源码（Python + pywebview）
│   │   └── build/          #   打包配置（PyInstaller spec / Inno Setup / 图标）
│   ├── Setup_校园网自动登录_v1.1.0.exe   # Windows 安装包
│   └── 校园网自动登录_v1.1.0_发布包.zip   # Windows 发布包（含源码+打包配置）
├── .github/workflows/      # CI：打 v* 标签自动构建已签名 APK 并发布 Release
└── README.md
```

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
- 详见 `windows/../app` 源码注释

### 本地构建

```bash
# JDK 17+；Android SDK（platform-34 + build-tools 34.0.0）
gradle assembleRelease   # 产物 app/build/outputs/apk/release/app-release.apk
```

### 签名与 CI

- 发布签名通过 `keystore.properties`（gitignore，不提交）引用本地 `campus_release.keystore`
- CI（`.github/workflows/`）从仓库 Secrets 还原密钥，打 `v*` 标签自动出已签名 APK 并发布 Release（含 Windows 发布物）

## Windows 版

Python + pywebview（WebView2）桌面应用，后台守护静默运行、断线自动重连。

- 源码与打包说明见 `windows/source/README.md` 与 `windows/source/重建说明.txt`
- 安装包 `Setup_校园网自动登录_v1.1.0.exe` 免管理员权限，支持覆盖升级

## 隐私 Privacy

- Android：账号密码仅存本机（Android Keystore 加密），唯一联网目标是校园认证网关
- Windows：账号密码仅存本机 `config.json`（混淆），不上传任何服务器
- 本仓库不包含任何用户个人信息与签名私钥

## 许可 License

MIT License. 本工具仅供学习与个人合法使用，请遵守所在学校网络使用规定。
