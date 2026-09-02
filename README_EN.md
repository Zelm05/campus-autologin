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
| Windows | `release/Autologin_v1.2.0_x64_setup.exe` | No admin required; in-place upgrade supported. Starting the background service also enables auto-run after login; optional "Turbo Start" runs at boot, even on the lock screen |

> New Android APKs are also built automatically via GitHub Actions: push a `v*` tag to get a signed APK from the Release page.

## ⚠️ Important — enable auto-start

**The app relies on running in the background; without auto-start it will NOT relogin after a drop:**

- **Windows**: open the app → fill in credentials → Save → press "**▶ Start background service**"
  (also enables auto-run after login, no admin needed). To also run before login / on the lock
  screen, tick "**Turbo Start**".
- **Android**: you must **manually enable "Auto-start / Run in background" for the app in
  System Settings** for it to take effect (paths differ by vendor — usually
  Settings → Apps → this app → Auto-start / Battery management; Xiaomi / Huawei / OPPO /
  vivo restrict background apps by default). Keep it resident — if the system kills it,
  it cannot auto-relogin. You may disable the "Monitoring" notification if you don't want it.

## Project layout

```
campus-autologin/
├── .github/workflows/      # CI: builds signed APK & publishes Release on v* tags
├── .gitignore              # excludes signing keys / build outputs / wheels
├── app/                    # Android app (Kotlin + Jetpack Compose)
├── windows/
│   └── source/             # Windows desktop app (Python + pywebview)
│       ├── app.py · campus_core.py · daemon.py · gui.py   # runtime files (config.json etc.) are not committed
│       ├── requirements.txt · README.md · 使用说明.txt · 启动设置.bat
│       └── build/          # packaging: CampusLogin.spec · icon.ico · setup.iss · build.bat · version_info.txt
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

## Disclaimer

1. This software is provided "as is" for **personal, legal use only**; please follow your school's network usage policy.
2. Credentials are stored **only on your device** (Android Keystore / local config.json) and are **never uploaded to any server** (the only network target is the campus auth gateway).
3. Any account suspension, network bans or policy violations resulting from use of this software are the user's own responsibility.
4. Do not use this software for commercial or illegal purposes.

## License

MIT License. See [LICENSE](LICENSE).
