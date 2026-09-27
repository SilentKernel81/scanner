package com.cardscanner

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.PI
import kotlin.math.sin

/**
 * Sound effects, the web UI's (static/js/audio.js): beeps with a 10 ms attack, decay to 30% and
 * release, at 30% volume. Generated once as PCM and played from memory.
 */
object Sounds {
    private const val RATE = 44100
    private const val VOLUME = 0.3

    private data class Beep(val startMs: Int, val frequency: Double, val durationMs: Int, val square: Boolean)

    /** playCapture: 1200 Hz then 1000 Hz, square - a camera shutter click */
    private val capture by lazy { render(listOf(Beep(0, 1200.0, 100, true), Beep(100, 1000.0, 80, true))) }

    fun capture() = play(capture)

    private fun render(beeps: List<Beep>): ShortArray {
        val totalMs = beeps.maxOf { it.startMs + it.durationMs }
        val samples = DoubleArray(RATE * totalMs / 1000)
        for (beep in beeps) {
            val start = RATE * beep.startMs / 1000
            val length = RATE * beep.durationMs / 1000
            val attack = RATE / 100  // 10 ms
            for (i in 0 until length) {
                val t = i.toDouble() / RATE
                val wave = sin(2 * PI * beep.frequency * t).let { if (beep.square) (if (it >= 0) 1.0 else -1.0) else it }
                // Envelope as in playBeep: 0 -> volume (10 ms) -> 30% (until 10 ms before the end) -> 0
                val gain = when {
                    i < attack -> i.toDouble() / attack
                    i < length - attack -> 1.0 - 0.7 * (i - attack).toDouble() / (length - 2 * attack)
                    else -> 0.3 * (length - i).toDouble() / attack
                } * VOLUME
                val index = start + i
                if (index < samples.size) samples[index] += wave * gain
            }
        }
        return ShortArray(samples.size) { (samples[it].coerceIn(-1.0, 1.0) * Short.MAX_VALUE).toInt().toShort() }
    }

    private fun play(pcm: ShortArray) {
        try {
            val track = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setTransferMode(AudioTrack.MODE_STATIC)
                .setBufferSizeInBytes(pcm.size * 2)
                .build()
            track.write(pcm, 0, pcm.size)
            // Released once played (a static track stops by itself at the end)
            track.notificationMarkerPosition = pcm.size
            track.setPlaybackPositionUpdateListener(object : AudioTrack.OnPlaybackPositionUpdateListener {
                override fun onMarkerReached(t: AudioTrack) = t.release()
                override fun onPeriodicNotification(t: AudioTrack) {}
            })
            track.play()
        } catch (e: Exception) {
            android.util.Log.w("Sounds", "Could not play a sound: $e")
        }
    }
}
