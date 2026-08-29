package com.campus.autologin.data.remote

/**
 * 硬编码的校园网配置（仅重庆科技大学 cqust 使用）。
 *
 * 基于对 `aaa.cqust.edu.cn/eportal` 的真实抓包（AuthInterFace.js）：
 *  - 登录：POST /eportal/InterFace.do?method=login
 *        body: userId / password / service / queryString / operatorPwd
 *  - 注销：POST /eportal/InterFace.do?method=logout  body: userIndex
 *  - 在线信息：POST /eportal/InterFace.do?method=getOnlineUserInfo body: userIndex
 *  - queryString 为网关重定向 URL 携带的 wlan 参数（wlanuserip 等），由 App 后台自动获取。
 *  - passwordEncrypt=false（明文密码，页面内 `<input name="passwordEncrypt" value="false">`）。
 */
object CampusConfig {
    const val SCHOOL_NAME = "重庆科技大学"
    const val GATEWAY = "http://aaa.cqust.edu.cn"
    const val LOGIN_PATH = "/eportal/InterFace.do?method=login"
    const val LOGOUT_PATH = "/eportal/InterFace.do?method=logout"
    const val ONLINE_INFO_PATH = "/eportal/InterFace.do?method=getOnlineUserInfo"
    const val KEEPALIVE_PATH = "/eportal/InterFace.do?method=keepalive"

    // 登录表单字段名（与 AuthInterFace.login 一致）
    const val USER_FIELD = "userId"
    const val PASS_FIELD = "password"
    const val SERVICE_FIELD = "service"
    const val QUERY_STRING_FIELD = "queryString"
    const val OPERATOR_PWD_FIELD = "operatorPwd"
    const val USER_INDEX_FIELD = "userIndex"

    const val TIMEOUT_MS = 10000L
}
