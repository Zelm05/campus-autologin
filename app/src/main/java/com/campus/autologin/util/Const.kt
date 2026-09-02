package com.campus.autologin.util

/**
 * App-wide constants. 校园网协议参数在 CampusConfig，探测地址在 NetworkProbe，
 * 这里只保留存储键。
 */
object Const {
    const val PREFS_FILE = "campus_secure_prefs"

    // SharedPreferences keys (values are encrypted by EncryptedSharedPreferences).
    const val KEY_USERNAME = "username"
    const val KEY_PASSWORD = "password"
    const val KEY_AUTO_LOGIN = "auto_login"
    const val KEY_AUTO_START = "auto_start"
    const val KEY_SAVE_PASSWORD = "save_password"
    const val KEY_NOTIFY_FAILURE = "notify_failure"
    const val KEY_SHOW_NOTIFICATION = "show_monitor_notification"
    const val KEY_ALWAYS_BACKGROUND = "always_run_background"
    const val KEY_USER_INDEX = "user_index"
    const val KEY_USER_INFO_NAME = "user_info_name"
    const val KEY_USER_INFO_IP = "user_info_ip"
    const val KEY_USER_INFO_MAC = "user_info_mac"
    const val KEY_USER_INFO_SERVICE = "user_info_service"
    const val KEY_USER_INFO_WLAN_HEX = "user_info_wlan_hex"
    const val KEY_MANUAL_LOGOUT_AT = "manual_logout_at"
    // 免认证网络名单：这些网络连上就能上网，不需要 portal 认证。
    // 值为网络指纹集合（优先 WiFi 名称，取不到时退回网关 IP，见 WifiNet.networkKey）。
    const val KEY_OPEN_NETWORKS = "open_networks"
}
