package com.campus.autologin.data.remote

import android.net.Network
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
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
                    "当前网络已经可以直接上网，无需认证。若手机同时开着移动数据，" +
                        "请先关闭移动数据再点登录。",
                    lastDiagnostics
                )
                else -> LoginResult.Failure(
                    "没能从校园网关取到认证参数。请确认已连上校园 WiFi 后重试；" +
                        "若手机同时开着移动数据，可先关闭移动数据再点登录。",
                    lastDiagnostics
                )
            }
        } catch (e: IOException) {
            LoginResult.Error("网络请求失败：${e.message}", e)
        } catch (t: Throwable) {
            LoginResult.Error("登录过程出错：${t.message}", t)
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
            ?: return LoginResult.Failure("登录响应无法解析", text.take(160))
        return when (json.optString("result")) {
            "success" -> {
                json.optString("userIndex").takeIf { it.isNotBlank() }?.let { userIndex = it }
                LoginResult.Success("登录成功")
            }
            "fail" -> LoginResult.Failure(
                json.optString("message").ifBlank { "登录失败" },
                lastDiagnostics
            )
            else -> LoginResult.Failure(
                "登录失败（${json.optString("message").ifBlank { "未知原因" }}）",
                lastDiagnostics
            )
        }
    }

    // ------------------------------------------------------------------
    // 在线状态 / 用户信息
    // ------------------------------------------------------------------
    suspend fun fetchOnlineInfo(net: Network?): UserInfo? = withContext(Dispatchers.IO) {
        try {
            val http = WifiNet.client(net, CampusConfig.TIMEOUT_MS, followRedirects = true)
            val url = joinUrl(CampusConfig.GATEWAY, CampusConfig.ONLINE_INFO_PATH)
            val body = FormBody.Builder()
                .add(CampusConfig.USER_INDEX_FIELD, userIndex)
                .build()
            val request = Request.Builder().url(url).post(body).build()
            val text = http.newCall(request).execute().use { it.body?.string().orEmpty() }
            val json = runCatching { JSONObject(text) }.getOrNull() ?: return@withContext null
            val name = json.optString("userName")
            val userId = json.optString("userId")
            if (name.isBlank() && userId.isBlank()) return@withContext null
            json.optString("userIndex").takeIf { it.isNotBlank() }?.let { userIndex = it }
            UserInfo(
                online = true,
                name = name,
                userId = userId,
                ip = json.optString("userIp"),
                mac = json.optString("userMac"),
                service = json.optString("service").ifBlank { json.optString("realServiceName") }
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
                return@withContext LoginResult.Failure("无在线会话，无法注销")
            }
            val http = WifiNet.client(net, CampusConfig.TIMEOUT_MS, followRedirects = true)
            val url = joinUrl(CampusConfig.GATEWAY, CampusConfig.LOGOUT_PATH)
            val body = FormBody.Builder()
                .add(CampusConfig.USER_INDEX_FIELD, index)
                .build()
            val request = Request.Builder().url(url).post(body).build()
            val text = http.newCall(request).execute().use { it.body?.string().orEmpty() }
            val result = runCatching { JSONObject(text).optString("result") }.getOrDefault("")
            if (result.equals("success", true)) {
                LoginResult.Success("已注销")
            } else {
                LoginResult.Failure("注销失败：${result.ifBlank { "未知响应" }}")
            }
        } catch (e: IOException) {
            LoginResult.Error("注销请求失败：${e.message}", e)
        } catch (t: Throwable) {
            LoginResult.Error("注销失败：${t.message}", t)
        }
    }

    private fun joinUrl(base: String, path: String): String {
        val b = base.trim().removeSuffix("/")
        return if (path.startsWith("/")) "$b$path" else "$b/$path"
    }
}
