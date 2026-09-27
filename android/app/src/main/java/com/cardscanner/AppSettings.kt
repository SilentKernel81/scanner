package com.cardscanner

import android.content.Context
import com.cardscanner.ai.Provider

/** User settings (SharedPreferences, private to the app) */
data class AppSettings(
    val provider: Provider = Provider.GEMINI,
    val models: Map<Provider, String> = emptyMap(),
    val apiKeys: Map<Provider, String> = emptyMap(),
    val localUrl: String = "",
    val detectFoil: Boolean = true,
    /** Don't auto-capture below this sharpness (Laplacian variance, see CardTracker) */
    val minSharpness: Int = 250,
    /** Consecutive still, in-focus frames before an auto-capture */
    val stableFrames: Int = 5,
    /** Extra clockwise rotation of the camera image (0/90/180/270): cards must look upright */
    val rotation: Int = 0,
    /** Fixed area (sleeved / borderless cards): cards judged by the image inside it */
    val fixedAreaEnabled: Boolean = false,
    /** The area: x1, y1, x2, y2 as fractions of the (turned) camera frame */
    val fixedArea: List<Double>? = null,
    /** Add confirmed cards (set + number match) to the inventory without asking */
    val autoAdd: Boolean = true,
    /** Sound effects (a click on each capture) */
    val sounds: Boolean = true,
) {
    /** The fixed area in use, or null (outline mode) */
    val activeArea get() = fixedArea?.takeIf { fixedAreaEnabled }

    fun model(provider: Provider = this.provider) = models[provider]?.takeIf { it.isNotBlank() } ?: provider.models.first()
    fun apiKey(provider: Provider = this.provider) = apiKeys[provider].orEmpty()

    companion object {
        private const val PREFS = "settings"

        fun load(context: Context): AppSettings {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            return AppSettings(
                provider = Provider.of(prefs.getString("provider", null)),
                models = Provider.entries.associateWith { prefs.getString("model_${it.id}", "").orEmpty() },
                apiKeys = Provider.entries.associateWith { prefs.getString("key_${it.id}", "").orEmpty() },
                localUrl = prefs.getString("local_url", "").orEmpty(),
                detectFoil = prefs.getBoolean("detect_foil", true),
                minSharpness = prefs.getInt("min_sharpness", 250),
                stableFrames = prefs.getInt("stable_frames", 5),
                rotation = prefs.getInt("rotation", 0),
                fixedAreaEnabled = prefs.getBoolean("fixed_area_enabled", false),
                autoAdd = prefs.getBoolean("auto_add", true),
                sounds = prefs.getBoolean("sounds", true),
                fixedArea = prefs.getString("fixed_area", null)?.split(",")?.mapNotNull { it.toDoubleOrNull() }?.takeIf { it.size == 4 },
            )
        }
    }

    fun save(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().apply {
            putString("provider", provider.id)
            Provider.entries.forEach {
                putString("model_${it.id}", models[it].orEmpty())
                putString("key_${it.id}", apiKeys[it].orEmpty())
            }
            putString("local_url", localUrl)
            putBoolean("detect_foil", detectFoil)
            putInt("min_sharpness", minSharpness)
            putInt("stable_frames", stableFrames)
            putInt("rotation", rotation)
            putBoolean("fixed_area_enabled", fixedAreaEnabled)
            putBoolean("auto_add", autoAdd)
            putBoolean("sounds", sounds)
            putString("fixed_area", fixedArea?.joinToString(","))
        }.apply()
    }
}
