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

class RecordingService : Service() {

    companion object {
        @Volatile var isRecording = false
        @Volatile var isRunning = false
        const val ACTION_TOGGLE = "com.speakflow.TOGGLE"
        private const val CHANNEL_ID = "speakflow_rec"
        private const val NOTIFICATION_ID = 20
        private const val RESULT_NOTIFICATION_ID = 21
        private const val SAMPLE_RATE = 16000
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    private val audioBuffer = ByteArrayOutputStream()
    @Volatile private var processing = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        isRunning = true
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_TOGGLE) {
            if (processing) return START_STICKY
            if (isRecording) {
                stopRecording()
                processAudio()
            } else {
                startForeground(NOTIFICATION_ID, buildRecordingNotification())
                startRecording()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        if (isRecording) stopRecording()
        isRunning = false
        isRecording = false
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "SpeakFlow Recording", NotificationManager.IMPORTANCE_LOW
        ).apply { setSound(null, null) }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildRecordingNotification(): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pending = PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("\uD83D\uDD34 SpeakFlow Recording")
            .setContentText("Pull down and tap SpeakFlow tile to stop")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(pending)
            .build()
    }

    private fun startRecording() {
        audioBuffer.reset()
        val bufSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (bufSize <= 0) { toast("Mic error"); finish(); return }

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufSize
            )
        } catch (e: SecurityException) {
            toast("Microphone permission denied"); finish(); return
        }

        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            toast("Could not open microphone"); audioRecord?.release(); audioRecord = null; finish(); return
        }

        audioRecord?.startRecording()
        isRecording = true
        toast("Recording...")

        recordingThread = thread {
            val buffer = ByteArray(bufSize)
            while (isRecording) {
                val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
                if (read > 0) synchronized(audioBuffer) { audioBuffer.write(buffer, 0, read) }
            }
        }
    }

    private fun stopRecording() {
        isRecording = false
        recordingThread?.join(2000)
        try { audioRecord?.stop() } catch (_: Exception) {}
        audioRecord?.release()
        audioRecord = null
    }

    private fun processAudio() {
        val pcmData = synchronized(audioBuffer) { audioBuffer.toByteArray() }
        if (pcmData.size < 6400) { toast("Too short"); finish(); return }

        processing = true
        toast("Transcribing...")

        // Update notification
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakFlow").setContentText("Transcribing...")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now).setOngoing(true).build())

        val wavData = pcmToWav(pcmData)
        val prefs = getSharedPreferences("speakflow", MODE_PRIVATE)
        val apiKey = prefs.getString("api_key", "") ?: ""
        val language = prefs.getString("language", "da") ?: "da"
        val aiCleanup = prefs.getBoolean("ai_cleanup", true)

        thread {
            try {
                val client = WhisperClient(apiKey)
                var text = client.transcribe(wavData, language)
                if (text.isBlank()) { toast("No speech detected"); return@thread }
                if (aiCleanup) text = client.cleanup(text, language)

                mainHandler.post {
                    val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("SpeakFlow", text))
                }

                nm.notify(RESULT_NOTIFICATION_ID, NotificationCompat.Builder(this, CHANNEL_ID)
                    .setContentTitle("SpeakFlow \u2014 Copied!")
                    .setContentText(text)
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                    .setAutoCancel(true).build())

                toast("Copied!")
            } catch (e: Exception) {
                toast("Error: ${e.message}")
            } finally {
                processing = false
                finish()
            }
        }
    }

    private fun finish() {
        isRecording = false
        isRunning = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun toast(msg: String) {
        mainHandler.post { Toast.makeText(this, msg, Toast.LENGTH_SHORT).show() }
    }

    private fun pcmToWav(pcmData: ByteArray): ByteArray {
        val byteRate = SAMPLE_RATE * 2
        val header = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray()); putInt(pcmData.size + 36)
            put("WAVE".toByteArray()); put("fmt ".toByteArray())
            putInt(16); putShort(1); putShort(1); putInt(SAMPLE_RATE)
            putInt(byteRate); putShort(2); putShort(16)
            put("data".toByteArray()); putInt(pcmData.size)
        }
        return header.array() + pcmData
    }
}
