package com.campus.autologin.data.remote

import android.net.Network
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
import com.campus.autologin.util.ErrorText
import com.campus.autologin.util.WifiNet
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException

/**
 * 锐捷 ePortal 登录引擎（cqust 实测协议）。
 *
 * 全链路要点：**每一个请求都用绑定 WiFi 的 client**（WifiNet.client），
 * 否则手机同时开着 5G 时，探测和登录都会走移动数据，永远看不到网关。
 *
 * 流程：
 *  1. 取 queryString（wlan 参数）：门户探测 → 系统门户地址 → 网关 index.jsp → 上次缓存；
 *  2. POST /eportal/InterFace.do?method=login，body userId/password/service/queryString/operatorPwd；
 *  3. 解析 {"result":"success|fail","message":...,"userIndex":...}。
 */
class LoginEngine {

    /** 最近一次登录/查询得到的 userIndex（供注销使用）。 */
    @Volatile
    var userIndex: String = ""

    /** 上次成功用过的 wlan 参数，作为最后兜底（会话可能已过期，仅试一次）。 */
    @Volatile
    private var lastQueryString: String = ""

    /** 最近一次探测的诊断串，登录失败时回显给用户。 */
    @Volatile
    var lastDiagnostics: String = ""
        private set

    /** 最近一次登录用到的 wlanuserip 十六进制（网关重定向 URL 携带）。 */
    @Volatile
    var lastWlanUserIpHex: String = ""

    // ------------------------------------------------------------------
    // 登录
    // ------------------------------------------------------------------
    suspend fun login(
        username: String,
        password: String,
        net: Network?,
        envInfo: String = ""
    ): LoginResult = withContext(Dispatchers.IO) {
        try {
            val outcome = NetworkProbe.detect(net)
            val trace = when (outcome) {
                is ProbeOutcome.Portal -> outcome.trace
                is ProbeOutcome.Online -> outcome.trace
                is ProbeOutcome.Inconclusive -> outcome.trace
            }

            var qs = queryOf((outcome as? ProbeOutcome.Portal)?.url)
            var from = if (qs != null) "探针" else ""

            if (qs == null) {
                qs = queryFromGateway(net)
                if (qs != null) from = "网关"
            }
            if (qs == null && lastQueryString.isNotBlank()) {
                qs = lastQueryString
                from = "缓存"
            }

            lastDiagnostics = buildString {
                append(envInfo)
                if (envInfo.isNotBlank()) append(" · ")
                append("探测:").append(trace.ifBlank { "无" })
                if (from.isNotBlank()) append(" · 参数来源:").append(from)
            }.take(400)

            when {
                qs != null -> doLogin(username, password, qs, net, from)

                outcome is ProbeOutcome.Online -> LoginResult.Failure(
                    "当前网络已经可以直接上网，不需要认证。\n" +
                        "如果你连的确实是校园 WiFi 却上不了网，多半是认证参数已经失效，" +
                        "请关掉 WiFi 再重开、等几秒后重试。",
                    lastDiagnostics
                )

                else -> LoginResult.Failure(
                    "没能从校园网关取到认证参数。请依次确认：\n" +
                        "1）连的是校园 WiFi，不是 4G/5G 或别的 WiFi；\n" +
                        "2）没有开着 VPN / 代理 / 加速器；\n" +
                        "3）若手机同时开着移动数据，可先关掉移动数据再点登录。",
                    lastDiagnostics
                )
            }
        } catch (e: IOException) {
            LoginResult.Error(ErrorText.of(e), e)
        } catch (t: Throwable) {
            LoginResult.Error(ErrorText.of(t), t)
        }
    }

    /** 从门户 URL 中截出 wlan 参数串。 */
    private fun queryOf(url: String?): String? {
        val qs = url?.substringAfter("?", "").orEmpty()
        return qs.takeIf { hasWlanParams(it) }
    }

    /**
     * 兜底：直接请求网关 index.jsp。部分网关会 302 回自己并补齐 wlan 参数，
     * 页面正文里偶尔也直接内嵌了带参地址。
     */
    private fun queryFromGateway(net: Network?): String? {
        val probe = WifiNet.client(net, timeoutMs = 6_000L, followRedirects = false)
        val url = joinUrl(CampusConfig.GATEWAY, "/eportal/index.jsp")
        return runCatching {
            probe.newCall(Request.Builder().url(url).get().build()).execute().use { resp ->
                queryOf(resp.header("Location"))?.let { return@runCatching it }
                val body = runCatching { resp.peekBody(8_192L).string() }.getOrDefault("")
                Regex("""https?://[^'"\s>)]*index\.jsp\?[^'"\s>)]+""", RegexOption.IGNORE_CASE)
                    .find(body)?.value?.let { queryOf(it) }
            }
        }.getOrNull()
    }

    private fun hasWlanParams(qs: String): Boolean =
        qs.contains("wlanuserip") || qs.contains("wlanacname") || qs.contains("nasid")

    private fun doLogin(
        user: String,
        pass: String,
        queryString: String,
        net: Network?,
        from: String
    ): LoginResult {
        val http = WifiNet.client(net, CampusConfig.TIMEOUT_MS, followRedirects = true)
        // 记下本次会话的 wlanuserip 十六进制，供主界面展示
        lastWlanUserIpHex = Regex("wlanuserip=([0-9a-fA-F]+)")
            .find(queryString)?.groupValues?.get(1).orEmpty()
        val url = joinUrl(CampusConfig.GATEWAY, CampusConfig.LOGIN_PATH) +
            "&time=" + (System.currentTimeMillis() / 1000)
        val body = FormBody.Builder()
            .add(CampusConfig.USER_FIELD, user)
            .add(CampusConfig.PASS_FIELD, pass)
            .add(CampusConfig.SERVICE_FIELD, "")
            .add(CampusConfig.QUERY_STRING_FIELD, queryString)
            .add(CampusConfig.OPERATOR_PWD_FIELD, "")
            .build()
        val request = Request.Builder().url(url).post(body)
            .header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
            .header("X-Requested-With", "XMLHttpRequest")
            .build()
        http.newCall(request).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            val result = classifyLogin(text)
            if (result is LoginResult.Success) lastQueryString = queryString
            // 缓存参数登录失败时，清掉缓存避免一直用过期会话
            if (result !is LoginResult.Success && from == "缓存") lastQueryString = ""
            return result
        }
    }

    private fun classifyLogin(text: String): LoginResult {
        val json = runCatching { JSONObject(text) }.getOrNull()
            ?: return LoginResult.Failure(ErrorText.unparsable(text), text.take(160))
        return when (val result = json.optString("result")) {
            "success" -> {
                json.optString("userIndex").takeIf { it.isNotBlank() }?.let { userIndex = it }
                LoginResult.Success("登录成功")
            }
            "fail" -> LoginResult.Failure(
                ErrorText.gateway(json.optString("message")),
                lastDiagnostics
            )
            else -> LoginResult.Failure(
                // result 既不是 success 也不是 fail：把网关原话带上，避免"未知原因"这种空话
                if (result.isBlank()) {
                    "网关返回了没有 result 字段的响应，无法判断是否成功。"
                } else {
                    "网关返回了意外的 result = \"$result\"。" +
                        ErrorText.gateway(json.optString("message"))
                },
                lastDiagnostics
            )
        }
    }

    // ------------------------------------------------------------------
    // 在线状态 / 用户信息
    // ------------------------------------------------------------------
    suspend fun fetchOnlineInfo(net: Network?): UserInfo? = withContext(Dispatchers.IO) {
        try {
            // 用比登录更短的超时：它每次刷新都要跑一次，不在校园网时应当快速失败
            val http = WifiNet.client(net, INFO_TIMEOUT_MS, followRedirects = true)
            val url = joinUrl(CampusConfig.GATEWAY, CampusConfig.ONLINE_INFO_PATH)
            val bodyBuilder = FormBody.Builder()
            // userIndex 为空时也照常发查询：部分网关支持"按当前 IP 查会话"，
            // 而"下线后重新登录"场景网关经常不回 userIndex，旧逻辑一空就永远查不到
            if (userIndex.isNotBlank()) bodyBuilder.add(CampusConfig.USER_INDEX_FIELD, userIndex)
            val request = Request.Builder().url(url).post(bodyBuilder.build()).build()
            val text = http.newCall(request).execute().use { it.body?.string().orEmpty() }
            val json = runCatching { JSONObject(text) }.getOrNull() ?: return@withContext null
            // org.json 的著名陷阱：JSON 字段是 null 时，optString 返回的是
            // **字面量字符串 "null"**（不是 Kotlin null），所以 isBlank() 拦不住——
            // 这里统一把 "null" 当作空值
            fun String.clean(): String? =
                this.takeIf { it.isNotBlank() && !it.equals("null", true) }
            // 兼容两种字段命名（不同网关版本 userName / name / user_name）
            val name = json.optString("userName").clean()
                ?: json.optString("name").clean()
                ?: json.optString("user_name").clean()
            val userId = json.optString("userId").clean()
                ?: json.optString("user_id").clean()
            val ip = json.optString("userIp").clean()
                ?: json.optString("user_ip").clean()
            val mac = json.optString("userMac").clean()
                ?: json.optString("user_mac").clean()
            val service = json.optString("service").clean()
                ?: json.optString("realServiceName").clean()
            if (name == null && userId == null) return@withContext null
            json.optString("userIndex").takeIf { it.isNotBlank() }?.let { userIndex = it }
            // 查询拿到了真实 IP，把它同时记为 wlanuserip 十六进制，供离线时兜底展示
            if (ip != null) {
                runCatching {
                    lastWlanUserIpHex = ip.split('.')
                        .mapNotNull { it.toIntOrNull() }
                        .take(4)
                        .joinToString("") { it.toString(16).padStart(2, '0') }
                }
            }
            UserInfo(
                online = true,
                name = name.orEmpty(),
                userId = userId.orEmpty(),
                ip = ip.orEmpty(),
                mac = mac.orEmpty(),
                service = service.orEmpty()
            )
        } catch (e: Exception) {
            null
        }
    }

    // ------------------------------------------------------------------
    // 注销
    // ------------------------------------------------------------------
    suspend fun logout(index: String, net: Network?): LoginResult = withContext(Dispatchers.IO) {
        try {
            if (index.isBlank()) {
                return@withContext LoginResult.Failure(
                    "当前没有记录到在线会话（可能是 App 重装过，或会话已过期），无法下线。\n" +
                        "如果需要下线，请在浏览器打开网关自助下线，或直接断开 WiFi。"
                )
            }
            val http = WifiNet.client(net, CampusConfig.TIMEOUT_MS, followRedirects = true)
            val url = joinUrl(CampusConfig.GATEWAY, CampusConfig.LOGOUT_PATH)
            val body = FormBody.Builder()
                .add(CampusConfig.USER_INDEX_FIELD, index)
                .build()
            val request = Request.Builder().url(url).post(body).build()
            val text = http.newCall(request).execute().use { it.body?.string().orEmpty() }
            val json = runCatching { JSONObject(text) }.getOrNull()
            if (json == null) {
                LoginResult.Failure(ErrorText.unparsable(text))
            } else if (json.optString("result").equals("success", true)) {
                LoginResult.Success("已下线")
            } else {
                // 常见：会话已过期/根本没在线 → 网关回 fail
                val msg = ErrorText.gateway(json.optString("message").ifBlank { "该会话已不存在" })
                LoginResult.Failure(msg)
            }
        } catch (e: IOException) {
            LoginResult.Error(ErrorText.of(e), e)
        } catch (t: Throwable) {
            LoginResult.Error(ErrorText.of(t), t)
        }
    }

    private fun joinUrl(base: String, path: String): String {
        val b = base.trim().removeSuffix("/")
        return if (path.startsWith("/")) "$b$path" else "$b/$path"
    }

    private companion object {
        /** 查询在线信息的超时：每次刷新都要跑，必须短，避免界面长时间转圈。 */
        const val INFO_TIMEOUT_MS = 3_000L
    }
}
