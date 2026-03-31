package com.speakflow.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.widget.Toast
import androidx.core.app.NotificationCompat
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread

class OverlayService : Service() {

    companion object {
        @Volatile
        var isRunning = false
        const val ACTION_TOGGLE = "com.speakflow.app.TOGGLE"
        const val ACTION_STOP_SERVICE = "com.speakflow.app.STOP_SERVICE"
        private const val CHANNEL_ID = "speakflow_channel"
        private const val NOTIFICATION_ID = 1
        private const val RESULT_NOTIFICATION_ID = 2
        private const val SAMPLE_RATE = 16000
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile
    private var recording = false

    @Volatile
    private var processing = false
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    private val audioBuffer = ByteArrayOutputStream()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        isRunning = true
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildIdleNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_TOGGLE -> toggleRecording()
            ACTION_STOP_SERVICE -> {
                if (recording) stopRecording()
                isRunning = false
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        isRunning = false
        if (recording) stopRecording()
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "SpeakFlow", NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "SpeakFlow voice transcription"
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun togglePendingIntent(): PendingIntent {
        val intent = Intent(this, OverlayService::class.java).apply {
            action = ACTION_TOGGLE
        }
        return PendingIntent.getService(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun appPendingIntent(): PendingIntent {
        val intent = Intent(this, MainActivity::class.java)
        return PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE)
    }

    private fun buildIdleNotification(): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakFlow")
            .setContentText("Tap to start recording")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(togglePendingIntent())
            .addAction(0, "\uD83C\uDFA4  Record", togglePendingIntent())
            .build()
    }

    private fun buildRecordingNotification(): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakFlow \u2014 Recording...")
            .setContentText("Tap to stop and transcribe")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setColor(getColor(R.color.red))
            .setOngoing(true)
            .setContentIntent(togglePendingIntent())
            .addAction(0, "\u23F9  Stop & Transcribe", togglePendingIntent())
            .build()
    }

    private fun buildProcessingNotification(): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakFlow")
            .setContentText("Transcribing...")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(appPendingIntent())
            .build()
    }

    private fun updateNotification(notification: Notification) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, notification)
    }

    private fun toggleRecording() {
        if (processing) return
        if (recording) {
            stopRecording()
            processAudio()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        audioBuffer.reset()

        val bufSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )

        audioRecord = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufSize
        )

        audioRecord?.startRecording()
        recording = true
        updateNotification(buildRecordingNotification())

        recordingThread = thread {
            val buffer = ByteArray(bufSize)
            while (recording) {
                val read = audioRecord?.read(buffer, 0, buffer.size) ?: 0
                if (read > 0) {
                    synchronized(audioBuffer) {
                        audioBuffer.write(buffer, 0, read)
                    }
                }
            }
        }
    }

    private fun stopRecording() {
        recording = false
        recordingThread?.join(2000)
        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null
    }

    private fun processAudio() {
        val pcmData = synchronized(audioBuffer) { audioBuffer.toByteArray() }
        if (pcmData.size < 3200) {
            toast("Recording too short")
            updateNotification(buildIdleNotification())
            return
        }

        processing = true
        updateNotification(buildProcessingNotification())

        val wavData = pcmToWav(pcmData)
        val prefs = getSharedPreferences("speakflow", MODE_PRIVATE)
        val apiKey = prefs.getString("api_key", "") ?: ""
        val language = prefs.getString("language", "da") ?: "da"
        val aiCleanup = prefs.getBoolean("ai_cleanup", true)

        thread {
            try {
                val client = WhisperClient(apiKey)
                var text = client.transcribe(wavData, language)

                if (text.isBlank()) {
                    toast("No speech detected")
                    return@thread
                }

                if (aiCleanup) {
                    text = client.cleanup(text, language)
                }

                // Copy to clipboard
                mainHandler.post {
                    val clipboard =
                        getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("SpeakFlow", text))
                }

                // Show result notification
                val nm = getSystemService(NotificationManager::class.java)
                nm.notify(RESULT_NOTIFICATION_ID, buildResultNotification(text))

                toast("Copied to clipboard!")
            } catch (e: Exception) {
                toast("Error: ${e.message}")
            } finally {
                processing = false
                updateNotification(buildIdleNotification())
            }
        }
    }

    private fun buildResultNotification(text: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakFlow \u2014 Copied!")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(appPendingIntent())
            .build()
    }

    private fun toast(msg: String) {
        mainHandler.post { Toast.makeText(this, msg, Toast.LENGTH_SHORT).show() }
    }

    private fun pcmToWav(pcmData: ByteArray): ByteArray {
        val byteRate = SAMPLE_RATE * 2

        val header = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray())
            putInt(pcmData.size + 36)
            put("WAVE".toByteArray())
            put("fmt ".toByteArray())
            putInt(16)
            putShort(1)
            putShort(1)
            putInt(SAMPLE_RATE)
            putInt(byteRate)
            putShort(2)
            putShort(16)
            put("data".toByteArray())
            putInt(pcmData.size)
        }

        return header.array() + pcmData
    }
}
