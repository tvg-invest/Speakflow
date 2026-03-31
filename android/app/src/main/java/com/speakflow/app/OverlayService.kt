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
import android.graphics.PixelFormat
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.view.Gravity
import android.view.MotionEvent
import android.view.WindowManager
import android.widget.ImageView
import android.widget.Toast
import androidx.core.app.NotificationCompat
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread
import kotlin.math.abs

class OverlayService : Service() {

    companion object {
        @Volatile
        var isRunning = false
        private const val CHANNEL_ID = "speakflow_float"
        private const val NOTIFICATION_ID = 10
        private const val RESULT_NOTIFICATION_ID = 11
        private const val SAMPLE_RATE = 16000
    }

    private lateinit var windowManager: WindowManager
    private lateinit var overlayButton: ImageView
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
        startForeground(NOTIFICATION_ID, buildNotification("Floating button active"))
        createOverlayButton()
    }

    override fun onDestroy() {
        isRunning = false
        if (recording) stopRecording()
        try {
            windowManager.removeView(overlayButton)
        } catch (_: Exception) {
        }
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "SpeakFlow Floating Button", NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Required for the floating record button"
            setSound(null, null)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pending = PendingIntent.getActivity(
            this, 0, intent, PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakFlow")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pending)
            .setOngoing(true)
            .build()
    }

    private fun createOverlayButton() {
        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager
        val density = resources.displayMetrics.density
        val size = (56 * density).toInt()
        val padding = (14 * density).toInt()

        overlayButton = ImageView(this).apply {
            setImageResource(R.drawable.ic_mic)
            setBackgroundResource(R.drawable.fab_bg)
            scaleType = ImageView.ScaleType.CENTER_INSIDE
            setPadding(padding, padding, padding, padding)
        }

        val params = WindowManager.LayoutParams(
            size, size,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
            x = (8 * density).toInt()
        }

        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f
        var moved = false

        overlayButton.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = params.x
                    initialY = params.y
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    moved = false
                    true
                }

                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - initialTouchX).toInt()
                    val dy = (event.rawY - initialTouchY).toInt()
                    if (abs(dx) > 10 || abs(dy) > 10) {
                        moved = true
                        params.x = initialX - dx
                        params.y = initialY + dy
                        windowManager.updateViewLayout(overlayButton, params)
                    }
                    true
                }

                MotionEvent.ACTION_UP -> {
                    if (!moved) onButtonTap()
                    true
                }

                else -> false
            }
        }

        windowManager.addView(overlayButton, params)
    }

    private fun onButtonTap() {
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

        if (bufSize <= 0) {
            toast("Mic error")
            return
        }

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufSize
            )
        } catch (e: SecurityException) {
            toast("Microphone permission denied")
            return
        }

        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            toast("Could not open microphone")
            audioRecord?.release()
            audioRecord = null
            return
        }

        audioRecord?.startRecording()
        recording = true
        overlayButton.setBackgroundResource(R.drawable.fab_bg_recording)
        toast("Recording...")

        recordingThread = thread {
            val buffer = ByteArray(bufSize)
            while (recording) {
                val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
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
        try {
            audioRecord?.stop()
        } catch (_: Exception) {
        }
        audioRecord?.release()
        audioRecord = null
        mainHandler.post { overlayButton.setBackgroundResource(R.drawable.fab_bg) }
    }

    private fun processAudio() {
        val pcmData = synchronized(audioBuffer) { audioBuffer.toByteArray() }

        if (pcmData.size < 6400) {
            toast("Too short")
            return
        }

        processing = true
        toast("Transcribing...")

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

                mainHandler.post {
                    val clipboard =
                        getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("SpeakFlow", text))
                }

                val nm = getSystemService(NotificationManager::class.java)
                val notif = NotificationCompat.Builder(this, CHANNEL_ID)
                    .setContentTitle("SpeakFlow \u2014 Copied!")
                    .setContentText(text)
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                    .setAutoCancel(true)
                    .build()
                nm.notify(RESULT_NOTIFICATION_ID, notif)

                toast("Copied!")
            } catch (e: Exception) {
                toast("Error: ${e.message}")
            } finally {
                processing = false
            }
        }
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
