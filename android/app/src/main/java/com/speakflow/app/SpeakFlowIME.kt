package com.speakflow.app

import android.inputmethodservice.InputMethodService
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.ImageButton
import android.widget.TextView
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread

class SpeakFlowIME : InputMethodService() {

    companion object {
        private const val SAMPLE_RATE = 16000
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var micBtn: ImageButton
    private lateinit var statusText: TextView
    private lateinit var hintText: TextView

    @Volatile private var recording = false
    @Volatile private var processing = false
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    private val audioBuffer = ByteArrayOutputStream()

    override fun onCreateInputView(): View {
        val view = layoutInflater.inflate(R.layout.keyboard_view, null)

        micBtn = view.findViewById(R.id.imeMicBtn)
        statusText = view.findViewById(R.id.imeStatus)
        hintText = view.findViewById(R.id.imeHint)

        val switchBtn = view.findViewById<ImageButton>(R.id.switchKeyboardBtn)
        switchBtn.setOnClickListener { switchToPreviousKeyboard() }

        micBtn.setOnClickListener { onMicTap() }

        // Check API key
        val prefs = getSharedPreferences("speakflow", MODE_PRIVATE)
        if (prefs.getString("api_key", "")?.isEmpty() == true) {
            statusText.text = "Open SpeakFlow app to set API key"
            statusText.setTextColor(getColor(R.color.orange))
            micBtn.isEnabled = false
            micBtn.alpha = 0.4f
        }

        return view
    }

    private fun switchToPreviousKeyboard() {
        val imm = getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager
        imm.switchToLastInputMethod(window.window?.attributes?.token)
    }

    override fun onStartInputView(info: android.view.inputmethod.EditorInfo?, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        // Re-check API key every time keyboard becomes visible
        val prefs = getSharedPreferences("speakflow", MODE_PRIVATE)
        val hasKey = prefs.getString("api_key", "")?.isNotEmpty() == true
        if (::micBtn.isInitialized) {
            micBtn.isEnabled = hasKey
            micBtn.alpha = if (hasKey) 1.0f else 0.4f
            if (hasKey && !recording && !processing) {
                statusText.text = "Tap to record"
                statusText.setTextColor(getColor(R.color.accent))
            } else if (!hasKey) {
                statusText.text = "Open SpeakFlow app to set API key"
                statusText.setTextColor(getColor(R.color.orange))
            }
        }
    }

    private fun onMicTap() {
        if (processing) return
        if (recording) {
            stopRecording()
            processAudio()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        val prefs = getSharedPreferences("speakflow", MODE_PRIVATE)
        if (prefs.getString("api_key", "")?.isEmpty() == true) {
            statusText.text = "Open SpeakFlow app to set API key"
            statusText.setTextColor(getColor(R.color.orange))
            return
        }

        audioBuffer.reset()

        val bufSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (bufSize <= 0) {
            statusText.text = "Microphone error"
            statusText.setTextColor(getColor(R.color.red))
            return
        }

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufSize
            )
        } catch (e: SecurityException) {
            statusText.text = "Microphone permission denied"
            statusText.setTextColor(getColor(R.color.red))
            return
        }

        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            statusText.text = "Could not open microphone"
            statusText.setTextColor(getColor(R.color.red))
            audioRecord?.release(); audioRecord = null
            return
        }

        audioRecord?.startRecording()
        recording = true
        micBtn.setBackgroundResource(R.drawable.fab_bg_recording)
        statusText.text = "Recording..."
        statusText.setTextColor(getColor(R.color.red))
        hintText.text = "Tap to stop"

        recordingThread = thread {
            val buffer = ByteArray(bufSize)
            while (recording) {
                val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
                if (read > 0) synchronized(audioBuffer) { audioBuffer.write(buffer, 0, read) }
            }
        }
    }

    private fun stopRecording() {
        recording = false
        recordingThread?.join(2000)
        try { audioRecord?.stop() } catch (_: Exception) {}
        audioRecord?.release()
        audioRecord = null
    }

    private fun processAudio() {
        val pcmData = synchronized(audioBuffer) { audioBuffer.toByteArray() }
        if (pcmData.size < 6400) {
            resetUI("Too short \u2014 try again", R.color.orange)
            return
        }

        processing = true
        micBtn.setBackgroundResource(R.drawable.fab_bg)
        micBtn.alpha = 0.5f
        val durationSec = String.format("%.1f", pcmData.size.toLong() * 1000.0 / (SAMPLE_RATE * 2) / 1000.0)
        statusText.text = "Transcribing ${durationSec}s..."
        statusText.setTextColor(getColor(R.color.accent))
        hintText.text = ""

        val wavData = pcmToWav(pcmData)
        val prefs = getSharedPreferences("speakflow", MODE_PRIVATE)
        val apiKey = prefs.getString("api_key", "") ?: ""
        val language = prefs.getString("language", "auto") ?: "auto"
        val aiCleanup = prefs.getBoolean("ai_cleanup", true)

        thread {
            try {
                val client = WhisperClient(apiKey)
                var text = client.transcribe(wavData, language)
                if (text.isBlank()) {
                    mainHandler.post { resetUI("No speech detected", R.color.orange) }
                    return@thread
                }
                if (aiCleanup) text = client.cleanup(text, language)

                val finalText = text
                mainHandler.post {
                    // Insert text directly into the active text field
                    currentInputConnection?.commitText(finalText, 1)
                    resetUI("Done!", R.color.green)

                    // Reset to "Tap to record" after 1.5s
                    mainHandler.postDelayed({
                        resetUI("Tap to record", R.color.accent)
                    }, 1500)
                }
            } catch (e: Exception) {
                mainHandler.post { resetUI("Error: ${e.message}", R.color.red) }
            } finally {
                processing = false
                mainHandler.post { micBtn.alpha = 1.0f }
            }
        }
    }

    private fun resetUI(status: String, colorRes: Int) {
        micBtn.setBackgroundResource(R.drawable.fab_bg)
        micBtn.alpha = 1.0f
        statusText.text = status
        statusText.setTextColor(getColor(colorRes))
        hintText.text = ""
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
