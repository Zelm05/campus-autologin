package com.campus.autologin.data.model

/**
 * 门户返回的在线用户信息（getOnlineUserInfo）。
 */
data class UserInfo(
    val online: Boolean = false,
    val name: String = "",
    val userId: String = "",
    val ip: String = "",
    val mac: String = "",
    val service: String = "",
    val wlanHex: String = ""   // 网关重定向 URL 里的 wlanuserip 十六进制
)
