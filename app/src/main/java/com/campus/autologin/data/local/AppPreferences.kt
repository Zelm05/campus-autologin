package com.campus.autologin.data.local

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.campus.autologin.data.model.LoginConfig
import com.campus.autologin.util.Const

/**
 * Encrypted, on-device storage for the login credentials and user-facing toggles.
 * Everything written here is encrypted at rest via the Android Keystore.
 */
class AppPreferences(context: Context) {

    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val prefs = EncryptedSharedPreferences.create(
        context,
        Const.PREFS_FILE,
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
    )

    fun loadConfig(): LoginConfig = with(prefs) {
        LoginConfig(
            username = getString(Const.KEY_USERNAME, "") ?: "",
            autoLogin = getBoolean(Const.KEY_AUTO_LOGIN, true),
            autoStartOnBoot = getBoolean(Const.KEY_AUTO_START, true),
            savePassword = getBoolean(Const.KEY_SAVE_PASSWORD, true),
            notifyOnFailure = getBoolean(Const.KEY_NOTIFY_FAILURE, false),
            showMonitorNotification = getBoolean(Const.KEY_SHOW_NOTIFICATION, true),
            alwaysRunInBackground = getBoolean(Const.KEY_ALWAYS_BACKGROUND, false)
        )
    }

    fun saveConfig(cfg: LoginConfig) = with(prefs.edit()) {
        putString(Const.KEY_USERNAME, cfg.username)
        putBoolean(Const.KEY_AUTO_LOGIN, cfg.autoLogin)
        putBoolean(Const.KEY_AUTO_START, cfg.autoStartOnBoot)
        putBoolean(Const.KEY_SAVE_PASSWORD, cfg.savePassword)
        putBoolean(Const.KEY_NOTIFY_FAILURE, cfg.notifyOnFailure)
        putBoolean(Const.KEY_SHOW_NOTIFICATION, cfg.showMonitorNotification)
        putBoolean(Const.KEY_ALWAYS_BACKGROUND, cfg.alwaysRunInBackground)
        apply()
    }

    fun getPassword(): String = prefs.getString(Const.KEY_PASSWORD, "") ?: ""

    fun setPassword(value: String) = prefs.edit().putString(Const.KEY_PASSWORD, value).apply()

    fun clearPassword() = prefs.edit().remove(Const.KEY_PASSWORD).apply()

    // ---- 手动下线冷却（App 内点"下线"后暂停自动补登一段时间） ----
    fun getManualLogoutAt(): Long = prefs.getLong(Const.KEY_MANUAL_LOGOUT_AT, 0L)
    fun setManualLogoutAt(time: Long) =
        prefs.edit().putLong(Const.KEY_MANUAL_LOGOUT_AT, time).apply()
    fun clearManualLogoutAt() = prefs.edit().remove(Const.KEY_MANUAL_LOGOUT_AT).apply()

    // ---- 登录会话信息（供注销 / 状态展示） ----
    fun getUserIndex(): String = prefs.getString(Const.KEY_USER_INDEX, "") ?: ""
    fun setUserIndex(value: String) = prefs.edit().putString(Const.KEY_USER_INDEX, value).apply()

    fun getUserInfoName(): String = prefs.getString(Const.KEY_USER_INFO_NAME, "") ?: ""
    fun getUserInfoIp(): String = prefs.getString(Const.KEY_USER_INFO_IP, "") ?: ""
    fun getUserInfoMac(): String = prefs.getString(Const.KEY_USER_INFO_MAC, "") ?: ""
    fun getUserInfoService(): String = prefs.getString(Const.KEY_USER_INFO_SERVICE, "") ?: ""
    fun getUserInfoWlanHex(): String = prefs.getString(Const.KEY_USER_INFO_WLAN_HEX, "") ?: ""

    fun setUserInfo(name: String, ip: String, mac: String, service: String) = prefs.edit()
        .putString(Const.KEY_USER_INFO_NAME, name)
        .putString(Const.KEY_USER_INFO_IP, ip)
        .putString(Const.KEY_USER_INFO_MAC, mac)
        .putString(Const.KEY_USER_INFO_SERVICE, service)
        .apply()

    fun setWlanUserIpHex(value: String) =
        prefs.edit().putString(Const.KEY_USER_INFO_WLAN_HEX, value).apply()

    fun clearUserInfo() = prefs.edit()
        .remove(Const.KEY_USER_INDEX)
        .remove(Const.KEY_USER_INFO_NAME)
        .remove(Const.KEY_USER_INFO_IP)
        .remove(Const.KEY_USER_INFO_MAC)
        .remove(Const.KEY_USER_INFO_SERVICE)
        .remove(Const.KEY_USER_INFO_WLAN_HEX)
        .apply()

    // ---- 免认证网络名单 ----
    // 这些网络连上就能上网、不需要 portal 认证。存的是网络指纹（见 WifiNet.networkKey）。
    fun getOpenNetworks(): Set<String> =
        prefs.getStringSet(Const.KEY_OPEN_NETWORKS, emptySet()) ?: emptySet()

    fun isOpenNetwork(key: String?): Boolean = key != null && getOpenNetworks().contains(key)

    /** 记住一个免认证网络：下次连上直接判定，跳过探针。 */
    fun addOpenNetwork(key: String) {
        if (key.isBlank()) return
        prefs.edit().putStringSet(Const.KEY_OPEN_NETWORKS, getOpenNetworks() + key).apply()
    }

    /**
     * 从免认证名单里移除。
     *
     * 登录成功时必须调用：既然在这个网络里能登录成功，就说明它是**需要认证**的，
     * 不能再留在免认证名单里，否则下次连上会被直接判成"已连接"而不再登录。
     * 这样就实现了双向修正：判错了一次，登录成功立刻纠正回来。
     */
    fun removeOpenNetwork(key: String) {
        if (key.isBlank()) return
        prefs.edit().putStringSet(Const.KEY_OPEN_NETWORKS, getOpenNetworks() - key).apply()
    }
}
