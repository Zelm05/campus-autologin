package com.campus.autologin.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// 取自用户插画：祖母绿 #739e6b（眼睛）、深绿 #4f7a48、暗夜 #211d24（发色）、冷白 #edf1f7（雪景）
private val LightColorScheme = lightColorScheme(
    primary = Color(0xFF4F7A48),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFD9E8D5),
    secondary = Color(0xFF739E6B),
    onSecondary = Color(0xFFFFFFFF),
    tertiary = Color(0xFF5B7A99),
    background = Color(0xFFEDF1F7),
    onBackground = Color(0xFF211D24),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF211D24),
    surfaceVariant = Color(0xFFE3EAE1),
    onSurfaceVariant = Color(0xFF5B5566),
    outline = Color(0xFFD8DEE8),
    error = Color(0xFFDC2626)
)

private val DarkColorScheme = darkColorScheme(
    primary = Color(0xFFA8C4A3),
    onPrimary = Color(0xFF1E2A1C),
    primaryContainer = Color(0xFF3A4A36),
    secondary = Color(0xFF739E6B),
    tertiary = Color(0xFFB9C7DD),
    background = Color(0xFF14171C),
    onBackground = Color(0xFFEDF1F7),
    surface = Color(0xFF1D2128),
    onSurface = Color(0xFFEDF1F7),
    surfaceVariant = Color(0xFF2A2F38),
    error = Color(0xFFEF9A9A)
)

@Composable
fun CampusTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme,
        content = content
    )
}
