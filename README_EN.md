# Campus Auto-Login 校园网自动登录

[中文](README.md) | **English**

> An auto-relogin campus network authenticator for Windows & Android, built for the Ruijie ePortal gateway (e.g. CQUST).

- Repository: <https://github.com/Zelm05/campus-autologin>
- Contact: yz050930@gmail.com

## Download

Installers are in [`release/`](release/):

| Platform | File | Notes |
| --- | --- | --- |
| Android | `release/Autologin-v1.0.3_release.apk` | Install directly; overwrite keeps your credentials. Older v1.0.1 is kept in this folder |
| Windows | `release/Autologin_v1.2.0_x64_setup.exe` | No admin required; in-place upgrade supported. v1.2.0 adds "Turbo Start": runs at boot, even on the lock screen |

> New Android APKs are also built automatically via GitHub Actions: push a `v*` tag to get a signed APK from the Release page.

## Project layout

```
campus-autologin/
├── .github/workflows/      # CI: builds signed APK & publishes Release on v* tags
├── .gitignore              # excludes signing keys / build outputs / wheels
├── app/                    # Android app (Kotlin + Jetpack Compose)
├── windows/
│   └── source/             # Windows desktop app (Python + pywebview)
│       ├── app.py · campus_core.py · daemon.py · gui.py · config.json
│       ├── requirements.txt · README.md · 使用说明.txt · 启动设置.bat
│       └── build/          # packaging: CampusLogin.spec · icon.ico · setup.iss · version_info.txt
├── release/                # downloadable installers (APK / EXE)
├── screenshots/            # UI screenshots for the README
├── build.gradle.kts        # root build script
├── settings.gradle.kts     # Gradle settings
├── gradle.properties       # Gradle global properties
├── LICENSE                 # MIT license
├── CHANGELOG.md            # version history
├── README.md               # Chinese readme
└── README_EN.md            # this file
```

## Screenshots

| 📱 Android v1.0.1 | 💻 Windows v1.1.0 |
| --- | --- |
| <img src="screenshots/android_v1.0.1_main.png" height="295" alt="Android 主界面"> <img src="screenshots/android_v1.0.1_settings.png" height="295" alt="Android 设置页"> | <img src="screenshots/windows_v1.1.0_main.png" height="295" alt="Windows 设置主界面"> |

## Privacy

- Android: credentials stay on-device (Android Keystore encryption); the only network target is the campus gateway
- Windows: credentials stay in the local `config.json` , never uploaded
- This repository contains no personal data and no signing private key
- Note: All of them require auto-start to be enabled, otherwise they won't take effect. On Android, if you don't want the "Monitoring" notification to show, you can disable the notification permission

## Disclaimer

1. This software is provided "as is" for **personal, legal use only**; please follow your school's network usage policy.
2. Credentials are stored **only on your device** (Android Keystore / local config.json) and are **never uploaded to any server** (the only network target is the campus auth gateway).
3. Any account suspension, network bans or policy violations resulting from use of this software are the user's own responsibility.
4. Do not use this software for commercial or illegal purposes.

## License

MIT License. See [LICENSE](LICENSE).
