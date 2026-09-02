package com.campus.autologin.ui.screen

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Login
import androidx.compose.material.icons.filled.Logout
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.campus.autologin.R
import com.campus.autologin.data.model.ConnectionState
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
import com.campus.autologin.data.remote.CampusConfig
import com.campus.autologin.util.ErrorText
import com.campus.autologin.ui.viewmodel.MainViewModel
import com.campus.autologin.ui.viewmodel.MainUiState
import com.campus.autologin.ui.viewmodel.greetingForHour
import java.util.Calendar
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(nav: NavController, vm: MainViewModel = viewModel()) {
    val state by vm.uiState.collectAsState()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    // 每次回到主界面（含从设置页返回）都重新读取配置
    LaunchedEffect(Unit) { vm.refresh() }

    // 周期自查：浏览器下线、会话过期、别的设备顶号等"外部下线"系统回调
    // 经常长时间不触发，这里每 30 秒主动刷一次，保证页面状态不滞后、
    // 并让"需登录/认证"自动补登尽快发生（不显示下拉指示器）。
    LaunchedEffect(Unit) {
        while (true) {
            delay(30_000)
            vm.refresh()
        }
    }

    // 监听**所有 WiFi 网络**的变化（而不是默认网络）。
    //
    // 旧版本用 registerDefaultNetworkCallback，它只回调"默认网络"——
    // 只要手机开着移动数据，未认证的校园 WiFi 永远不是默认网络，
    // 于是 WiFi 断开/重连/认证变化全都收不到，界面状态就不刷新。
    // 现在只关心 WiFi transport：断开 → onLost，连上 → onAvailable，
    // 校验通过/失效 → onCapabilitiesChanged。跟移动数据开不开完全无关。
    DisposableEffect(Unit) {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val known = mutableSetOf<Network>()
        var lastValidated: Boolean? = null
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                known += network
                // 断网重连 = 新会话：结束"手动下线"暂停并刷新（自动登录逻辑在
                // doRefresh 里；服务被杀时这里也能放行）
                vm.onNetworkAvailable()
                // WiFi 刚连上时 DHCP/DNS 可能还没就绪，会话查询会失败而误判
                // "需登录/认证"；2 秒后再确认一次，让"已连接"尽快落定
                scope.launch {
                    delay(2000)
                    vm.refresh()
                }
            }

            override fun onLost(network: Network) {
                known -= network
                vm.refresh()
                // WiFi 网络对象从系统移除有延迟：断开瞬间 probeState 可能还读到
                // 残留的 WiFi，先显示"需认证"；1.5 秒后再确认一次，让"未连接"落定
                scope.launch {
                    delay(1500)
                    vm.refresh()
                }
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // 只关心 WiFi 网络本身，避免移动数据的能力变化来打扰
                if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return
                val validated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                if (lastValidated == null || validated != lastValidated) {
                    lastValidated = validated
                    vm.refresh()
                }
            }
        }
        runCatching {
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .build()
            cm.registerNetworkCallback(request, callback)
        }
        onDispose {
            runCatching { cm.unregisterNetworkCallback(callback) }
            known.clear()
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Image(
                            painter = painterResource(id = R.mipmap.ic_launcher),
                            contentDescription = null,
                            modifier = Modifier
                                .size(32.dp)
                                .clip(RoundedCornerShape(8.dp))
                        )
                        Spacer(Modifier.size(10.dp))
                        Text("校园网自动登录", fontWeight = FontWeight.Bold)
                    }
                },
                actions = {
                    IconButton(onClick = { nav.navigate("settings") }) {
                        Icon(Icons.Filled.Settings, contentDescription = "设置")
                    }
                }
            )
        }
    ) { padding ->
        // 下拉刷新：刷新一次 = 重读配置 + 探测状态 + 若需登录则自动登录一次。
        // 指示器只反映"用户主动下拉"，中间的 StatusOrb 才反映整体 loading，
        // 两者解耦后，自动刷新转圈时下拉手势不会被顶掉。
        PullToRefreshBox(
            isRefreshing = state.userRefreshing,
            onRefresh = { vm.refresh(byUser = true) },
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp)
            ) {
                HeroCard(state)

                Spacer(Modifier.height(14.dp))
                // 非在线时不渲染会话信息（IP/MAC 是上一次的残留，会误导）。
                // 免认证网络（OPEN）在网关里没有本账号会话，改显示本机网络信息。
                InfoCard(
                    info = when (state.connection) {
                        ConnectionState.ONLINE -> state.userInfo
                        ConnectionState.OPEN -> state.localNet
                        else -> UserInfo()
                    }
                )

                Spacer(Modifier.height(16.dp))
                // 免认证网络（OPEN）：连上就能上网，网关里并没有本账号会话，
                // 点登录/下线都只会失败报错，所以这里禁用并说明原因；
                // 同时保留"强制认证"入口，万一判断有误，用户仍能手动登录。
                val isOpen = state.connection == ConnectionState.OPEN
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Button(
                        onClick = vm::login,
                        enabled = !state.loading && !isOpen,
                        modifier = Modifier
                            .weight(if (isOpen) 1f else 2f)
                            .height(52.dp),
                        shape = RoundedCornerShape(14.dp)
                    ) {
                        if (state.loading && !isOpen) {
                            CircularProgressIndicator(
                                Modifier.size(18.dp),
                                strokeWidth = 2.dp,
                                color = MaterialTheme.colorScheme.onPrimary
                            )
                        } else {
                            Icon(Icons.Filled.Login, contentDescription = null)
                        }
                        Spacer(Modifier.size(8.dp))
                        Text(
                            when {
                                isOpen -> "无需认证"
                                state.connection == ConnectionState.ONLINE -> "重新连接"
                                else -> "立即登录"
                            }
                        )
                    }
                    // 免认证网络没有会话可注销，下线按钮直接不显示
                    if (!isOpen) {
                        OutlinedButton(
                            onClick = vm::logout,
                            enabled = !state.loading,
                            modifier = Modifier
                                .weight(1f)
                                .height(52.dp),
                            shape = RoundedCornerShape(14.dp)
                        ) {
                            Icon(Icons.Filled.Logout, contentDescription = null)
                            Spacer(Modifier.size(8.dp))
                            Text("下线")
                        }
                    }
                }

                // 兜底入口：万一 App 判断有误（这个网络其实需要认证），
                // 用户仍可以手动发起一次认证，不至于彻底上不了网。
                if (isOpen) {
                    TextButton(
                        onClick = vm::login,
                        enabled = !state.loading,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("判断有误？点此强制认证")
                    }
                }

                Spacer(Modifier.height(14.dp))
                SwitchRow("自动登录", "检测到校园网后自动提交", state.config.autoLogin, vm::toggleAutoLogin)

                state.lastResult?.let { result ->
                    Spacer(Modifier.height(14.dp))
                    ResultCard(result)
                }
            }
        }
    }
}

@Composable
private fun HeroCard(state: MainUiState) {
    // 实时在线只看探测结果；userInfo.online 是旧缓存（浏览器下线后仍为 true），
    // 若参与判定会导致状态永远卡在"已连接"。
    // 免认证网络（OPEN）同样算已连接，只是没有校园网会话信息。
    val online = isConnected(state.connection)
    // null 安全：greeting 有默认值但万一空串时回退到按时段再算一次
    val greeting = state.greeting.ifBlank {
        greetingForHour(Calendar.getInstance().get(Calendar.HOUR_OF_DAY))
    }
    // 只有在**确认在线且拿到本账号会话**时才显示姓名。
    // 旧版本无条件用「网关返回的姓名 → 学号」兜底，于是出现过：
    //   · 还没登录就显示 "XXX 同学"（学号是本地保存的，跟登录状态无关）；
    //   · 断开网络 / 在校外网（只有"能上网"没有校园会话）时左上角仍挂着姓名。
    // userInfo.online 只在网关确认会话后为 true，断网/未认证/校外网都会被清掉。
    // 姓名属于个人信息，没连上校园网就不该露出来。
    val name = if (online && state.userInfo.online) {
        state.userInfo.name
            .takeIf { it.isNotBlank() && !it.equals("null", true) }
    } else null

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(24.dp))
            .background(
                Brush.linearGradient(
                    listOf(Color(0xFF2A2430), Color(0xFF1C1820), Color(0xFF25311F))
                )
            )
            .padding(20.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.Start) {
            Text(
                "$greeting 👋",
                color = Color.White.copy(alpha = 0.92f),
                style = MaterialTheme.typography.bodySmall
            )
            if (name != null) {
                Spacer(Modifier.height(3.dp))
                Text(
                    "$name 同学",
                    color = Color.White,
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold
                )
            }
        }

        Spacer(Modifier.height(14.dp))
        StatusOrb(state)
        Spacer(Modifier.height(12.dp))

        val (title, subtitle) = statusText(state)
        Text(title, color = Color.White, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(2.dp))
        Text(
            subtitle,
            color = Color.White.copy(alpha = 0.8f),
            style = MaterialTheme.typography.bodySmall
        )

        Spacer(Modifier.height(14.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(12.dp))
                .background(Color.White.copy(alpha = 0.10f))
                .padding(horizontal = 12.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                Modifier
                    .size(8.dp)
                    .background(if (online) Color(0xFFA8D49E) else Color(0xFF9CA3AF), CircleShape)
            )
            Spacer(Modifier.size(8.dp))
            Text("当前账号", color = Color.White.copy(alpha = 0.9f), style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.weight(1f))
            Text(
                state.config.username.ifBlank { "未设置" },
                color = Color.White.copy(alpha = 0.9f),
                style = MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Bold
            )
        }
    }
}

@Composable
private fun StatusOrb(state: MainUiState) {
    val online = isConnected(state.connection)
    val loading = state.loading

    val ringColor = when {
        loading -> Color(0xFFF59E0B)
        online -> Color(0xFFA8C4A3)
        else -> Color(0xFF9CA3AF)
    }
    val innerColor = when {
        loading -> Color(0xFFFDE68A)
        online -> Color(0xFFEAF2E8)
        else -> Color(0xFFE5E7EB)
    }
    val iconColor = when {
        loading -> Color(0xFFB45309)
        online -> Color(0xFF4F7A48)
        else -> Color(0xFF6B7280)
    }

    val transition = rememberInfiniteTransition(label = "orb")
    val pulse by transition.animateFloat(
        initialValue = 1f,
        targetValue = 1.28f,
        animationSpec = infiniteRepeatable(tween(1400), RepeatMode.Restart),
        label = "orbPulse"
    )

    Box(contentAlignment = Alignment.Center, modifier = Modifier.size(124.dp)) {
        Box(
            Modifier
                .size(124.dp)
                .scale(pulse)
                .background(ringColor.copy(alpha = 0.30f), CircleShape)
        )
        Box(
            Modifier
                .size(112.dp)
                .clip(CircleShape)
                .background(ringColor.copy(alpha = 0.16f))
                .padding(6.dp),
            contentAlignment = Alignment.Center
        ) {
            Box(
                Modifier
                    .size(56.dp)
                    .background(innerColor, CircleShape),
                contentAlignment = Alignment.Center
            ) {
                if (loading) {
                    CircularProgressIndicator(
                        Modifier.size(30.dp),
                        strokeWidth = 3.dp,
                        color = iconColor
                    )
                } else {
                    Icon(
                        if (online) Icons.Filled.Check else Icons.Filled.Error,
                        contentDescription = null,
                        tint = iconColor,
                        modifier = Modifier.size(30.dp)
                    )
                }
            }
        }
    }
}

/** ONLINE（认证过的校园网）与 OPEN（免认证网络）在界面上都算"已连接"。 */
private fun isConnected(state: ConnectionState): Boolean =
    state == ConnectionState.ONLINE || state == ConnectionState.OPEN

private fun statusText(state: MainUiState): Pair<String, String> {
    // 服务名来自网关会话，可能是空串或字面量 "null"，一律兜底成"互联网"
    val svc = state.userInfo.service
        .takeIf { it.isNotBlank() && !it.equals("null", true) }
        ?: "互联网"
    return when {
        state.loading -> "登录中…" to "正在提交认证"
        // "在线"的唯一标准是网关确认了本账号会话（有 IP），
        // 因此 ONLINE 必然显示"校园网已连接"，不存在"已连接+空 IP/MAC"
        state.connection == ConnectionState.ONLINE -> "校园网已连接" to "服务：$svc"
        // 免认证网络：连上就能上网，网关里没有本账号会话（它本就不需要认证）。
        // 同样按"已连接"展示，并说明为什么看不到会话信息。
        state.connection == ConnectionState.OPEN -> "已连接" to "当前网络无需认证"
        state.connection == ConnectionState.CAPTIVE ->
            "需登录/认证" to "已连上校园网，等待认证"
        state.connection == ConnectionState.OFFLINE ->
            "未连接" to "没有可用的 WiFi，请先连接校园网"
        else -> "检测中" to "正在检测网络状态…"
    }
}

@Composable
private fun InfoCard(info: UserInfo) {
    // 防御 org.json 陷阱：JSON 字段为 null 时 optString 返回字面量 "null"，
    // 这里统一清洗成空串（再交由 InfoRow 显示"—"）
    fun s(raw: String): String =
        raw.takeIf { it.isNotBlank() && !it.equals("null", true) }.orEmpty()
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(Modifier.fillMaxWidth().padding(16.dp)) {
            InfoRow("校园", CampusConfig.SCHOOL_NAME.orEmpty().ifBlank { "校园" }, "🏫")
            InfoRow("网关", CampusConfig.GATEWAY.removePrefix("http://").orEmpty(), "📡")
            InfoRow("本机 IP", s(info.ip).ifBlank { "—" }, "💻", s(info.wlanHex).ifBlank { "" })
            val macDisplay = s(info.mac).ifBlank { null }
            if (macDisplay != null) InfoRow("MAC 地址", macDisplay, "🔗")
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String, emoji: String, tail: String = "") {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            Modifier
                .size(36.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(MaterialTheme.colorScheme.primaryContainer),
            contentAlignment = Alignment.Center
        ) {
            Text(emoji, style = MaterialTheme.typography.titleSmall)
        }
        Spacer(Modifier.size(12.dp))
        Column {
            Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(
                value.orEmpty().ifBlank { "—" },
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold
            )
        }
        if (tail.isNotBlank()) {
            Spacer(Modifier.weight(1f))
            Text(tail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun SwitchRow(title: String, desc: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 14.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.SemiBold)
                Text(desc, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Switch(checked = checked, onCheckedChange = onChange)
        }
    }
}

@Composable
private fun ResultCard(result: LoginResult) {
    val (title, color) = when (result) {
        is LoginResult.Success -> "登录成功" to Color(0xFF4F7A48)
        is LoginResult.Failure -> "登录失败" to Color(0xFFDC2626)
        is LoginResult.Error -> "操作错误" to Color(0xFFDC2626)
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(
            containerColor = color.copy(alpha = 0.10f)
        )
    ) {
        Column(Modifier.fillMaxWidth().padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (result is LoginResult.Failure || result is LoginResult.Error) {
                    Icon(Icons.Filled.Error, null, tint = color, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.size(8.dp))
                }
                Text(title, color = color, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.height(4.dp))
            // 结果文案统一走 ErrorText，保证不会把 null / 英文错误码直接甩给用户
            val body = result.message
                .takeIf { it.isNotBlank() && !it.equals("null", true) }
                ?: "（网关没有返回详细信息）"
            Text(body, style = MaterialTheme.typography.bodyMedium)
            val detail = when (result) {
                is LoginResult.Failure -> result.preview
                is LoginResult.Success -> result.preview
                is LoginResult.Error -> ErrorText.of(result.cause)
            }.takeIf { it.isNotBlank() && !it.equals("null", true) }
            if (detail != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    detail,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}
