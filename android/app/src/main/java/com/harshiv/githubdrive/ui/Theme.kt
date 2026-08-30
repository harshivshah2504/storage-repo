package com.harshiv.githubdrive.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * A near-monochrome palette, to match a logo that is a black mark and nothing else.
 *
 * Ink is the accent: buttons are black on white and white on black, so the one coloured thing on
 * screen is the person's own content. Dynamic colour is deliberately not used - letting the
 * wallpaper repaint the app was what put grey cards behind black text.
 */
private val LightColors = lightColorScheme(
    primary = Color(0xFF14161A),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFF14161A),
    onPrimaryContainer = Color(0xFFFFFFFF),
    secondary = Color(0xFF4B5563),
    onSecondary = Color(0xFFFFFFFF),
    background = Color(0xFFF7F8FA),
    onBackground = Color(0xFF14161A),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF14161A),
    surfaceVariant = Color(0xFFEEF0F4),
    onSurfaceVariant = Color(0xFF5A6472),
    outline = Color(0xFFD8DCE3),
    outlineVariant = Color(0xFFE6E9EE),
    error = Color(0xFFB3261E),
    onError = Color(0xFFFFFFFF)
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFFF2F4F8),
    onPrimary = Color(0xFF14161A),
    primaryContainer = Color(0xFFF2F4F8),
    onPrimaryContainer = Color(0xFF14161A),
    secondary = Color(0xFF9BA3AF),
    onSecondary = Color(0xFF14161A),
    background = Color(0xFF0B0D10),
    onBackground = Color(0xFFE9ECF1),
    surface = Color(0xFF14171C),
    onSurface = Color(0xFFE9ECF1),
    surfaceVariant = Color(0xFF1E222A),
    onSurfaceVariant = Color(0xFF9BA3AF),
    outline = Color(0xFF2A2F39),
    outlineVariant = Color(0xFF232830),
    error = Color(0xFFF2B8B5),
    onError = Color(0xFF14161A)
)

@Composable
fun MemVaultTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    MaterialTheme(colorScheme = if (darkTheme) DarkColors else LightColors, content = content)
}
