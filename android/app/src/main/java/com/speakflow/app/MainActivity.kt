package com.speakflow.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var apiKeyField: EditText
    private lateinit var recordButton: Button
    private lateinit var statusText: TextView
    private lateinit var resultText: TextView
    private lateinit var languageSpinner: Spinner
    private lateinit var cleanupCheck: CheckBox

    private val prefs by lazy { getSharedPreferences("speakflow", MODE_PRIVATE) }

    @Volatile
    private var recording = false

    @Volatile
    private var processing = false
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    private val audioBuffer = ByteArrayOutputStream()

    companion object {
        private const val SAMPLE_RATE = 16000
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        apiKeyField = findViewById(R.id.apiKeyField)
        recordButton = findViewById(R.id.recordButton)
        statusText = findViewById(R.id.statusText)
        resultText = findViewById(R.id.resultText)
        languageSpinner = findViewById(R.id.languageSpinner)
        cleanupCheck = findViewById(R.id.cleanupCheck)

        // Language spinner
        val languages = arrayOf("Danish", "English", "Auto-detect")
        languageSpinner.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, languages
        )

        // Load saved settings
        val savedKey = prefs.getString("api_key", "") ?: ""
        if (savedKey.isNotEmpty()) {
            val masked = savedKey.take(3) + "\u2022".repeat(
                maxOf(0, savedKey.length - 7)
            ) + savedKey.takeLast(4)
            apiKeyField.setText(masked)
        }

        val langIndex = when (prefs.getString("language", "auto")) {
            "da" -> 0; "en" -> 1; else -> 2
        }
        languageSpinner.setSelection(langIndex)
        cleanupCheck.isChecked = prefs.getBoolean("ai_cleanup", true)

        recordButton.setOnClickListener { onRecordTap() }
        cleanupCheck.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean("ai_cleanup", checked).apply()
        }

        // Save API key immediately when focus leaves the field
        apiKeyField.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) saveSettings()
        }

        // Save language immediately when changed
        languageSpinner.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, pos: Int, id: Long) {
                val langCode = when (pos) { 0 -> "da"; 1 -> "en"; else -> "auto" }
                prefs.edit().putString("language", langCode).apply()
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        }

        requestPermissions()
    }

    override fun onPause() {
        super.onPause()
        saveSettings()
    }

    override fun onDestroy() {
        if (recording) stopRecording()
        super.onDestroy()
    }

    private fun requestPermissions() {
        val needed = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.RECORD_AUDIO)
        }
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), 1)
        }
    }

    private fun saveSettings() {
        // Save API key if changed
        val keyText = apiKeyField.text.toString().trim()
        if (keyText.isNotEmpty() && "\u2022" !in keyText) {
            prefs.edit().putString("api_key", keyText).apply()
            val masked = keyText.take(3) + "\u2022".repeat(
                maxOf(0, keyText.length - 7)
            ) + keyText.takeLast(4)
            apiKeyField.setText(masked)
        }
        // Save language
        val langCode = when (languageSpinner.selectedItemPosition) {
            0 -> "da"; 1 -> "en"; else -> "auto"
        }
        prefs.edit().putString("language", langCode).apply()
    }

    private fun onRecordTap() {
        if (processing) return

        if (recording) {
            stopRecording()
            processAudio()
        } else {
            saveSettings()

            val apiKey = prefs.getString("api_key", "") ?: ""
            if (apiKey.isEmpty()) {
                statusText.text = "Enter your API key first \u2191"
                statusText.setTextColor(getColor(R.color.orange))
                return
            }

            startRecording()
        }
    }

    private fun startRecording() {
        audioBuffer.reset()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            statusText.text = "Microphone permission required"
            statusText.setTextColor(getColor(R.color.orange))
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.RECORD_AUDIO), 1
            )
            return
        }

        val bufSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )

        if (bufSize <= 0) {
            statusText.text = "Microphone error"
            statusText.setTextColor(getColor(R.color.red))
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
            statusText.text = "Microphone permission denied"
            statusText.setTextColor(getColor(R.color.red))
            return
        }

        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            statusText.text = "Could not open microphone"
            statusText.setTextColor(getColor(R.color.red))
            audioRecord?.release()
            audioRecord = null
            return
        }

        audioRecord?.startRecording()
        recording = true

        statusText.text = "\uD83D\uDD34  Recording..."
        statusText.setTextColor(getColor(R.color.red))
        recordButton.text = "\u23F9  Stop"
        recordButton.setBackgroundColor(getColor(R.color.red))
        resultText.visibility = View.GONE

        recordingThread = thread {
            val buffer = ByteArray(bufSize)
            val maxBytes = SAMPLE_RATE * 2 * 7200 // 2 hours max
            while (recording) {
                val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
                if (read > 0) {
                    synchronized(audioBuffer) {
                        audioBuffer.write(buffer, 0, read)
                        if (audioBuffer.size() >= maxBytes) {
                            recording = false
                            runOnUiThread { stopRecording(); processAudio() }
                        }
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
    }

    private fun processAudio() {
        val pcmData = synchronized(audioBuffer) { audioBuffer.toByteArray() }
        val durationMs = (pcmData.size.toLong() * 1000) / (SAMPLE_RATE * 2)

        if (pcmData.size < 6400) {
            statusText.text = "Too short \u2014 try again"
            statusText.setTextColor(getColor(R.color.orange))
            recordButton.text = "\uD83C\uDFA4  Record"
            recordButton.setBackgroundColor(getColor(R.color.accent))
            return
        }

        processing = true
        statusText.text = "Transcribing ${String.format("%.1f", durationMs / 1000.0)}s..."
        statusText.setTextColor(getColor(R.color.accent))
        recordButton.text = "Processing..."
        recordButton.setBackgroundColor(getColor(R.color.dim))

        val wavData = pcmToWav(pcmData)
        val apiKey = prefs.getString("api_key", "") ?: ""
        val language = prefs.getString("language", "auto") ?: "auto"
        val aiCleanup = prefs.getBoolean("ai_cleanup", true)

        thread {
            try {
                val client = WhisperClient(apiKey)
                var text = client.transcribe(wavData, language)

                if (text.isBlank()) {
                    runOnUiThread {
                        statusText.text = "No speech detected"
                        statusText.setTextColor(getColor(R.color.orange))
                    }
                    return@thread
                }

                if (aiCleanup) {
                    runOnUiThread {
                        statusText.text = "Cleaning up..."
                        statusText.setTextColor(getColor(R.color.accent))
                    }
                    text = client.cleanup(text, language)
                }

                // Copy to clipboard
                val finalText = text
                runOnUiThread {
                    val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("SpeakFlow", finalText))

                    statusText.text = "Copied to clipboard!"
                    statusText.setTextColor(getColor(R.color.green))
                    resultText.text = finalText
                    resultText.visibility = View.VISIBLE
                }
            } catch (e: Exception) {
                runOnUiThread {
                    statusText.text = "Error: ${e.message}"
                    statusText.setTextColor(getColor(R.color.red))
                }
            } finally {
                processing = false
                runOnUiThread {
                    recordButton.text = "\uD83C\uDFA4  Record"
                    recordButton.setBackgroundColor(getColor(R.color.accent))
                }
            }
        }
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
