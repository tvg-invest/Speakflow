package com.speakflow.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var apiKeyField: EditText
    private lateinit var startButton: Button
    private lateinit var statusText: TextView
    private lateinit var languageSpinner: Spinner
    private lateinit var cleanupCheck: CheckBox

    private val prefs by lazy { getSharedPreferences("speakflow", MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        apiKeyField = findViewById(R.id.apiKeyField)
        startButton = findViewById(R.id.startButton)
        statusText = findViewById(R.id.statusText)
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

        val langIndex = when (prefs.getString("language", "da")) {
            "da" -> 0; "en" -> 1; else -> 2
        }
        languageSpinner.setSelection(langIndex)
        cleanupCheck.isChecked = prefs.getBoolean("ai_cleanup", true)

        startButton.setOnClickListener { toggleService() }
        cleanupCheck.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean("ai_cleanup", checked).apply()
        }

        requestPermissions()
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

    private fun toggleService() {
        // Save API key if user typed a new one
        val keyText = apiKeyField.text.toString().trim()
        if (keyText.isNotEmpty() && "\u2022" !in keyText) {
            prefs.edit().putString("api_key", keyText).apply()
            val masked = keyText.take(3) + "\u2022".repeat(
                maxOf(0, keyText.length - 7)
            ) + keyText.takeLast(4)
            apiKeyField.setText(masked)
        }

        val apiKey = prefs.getString("api_key", "") ?: ""
        if (apiKey.isEmpty()) {
            statusText.text = "Enter your OpenAI API key first"
            statusText.setTextColor(getColor(R.color.orange))
            return
        }

        // Save language
        val langCode = when (languageSpinner.selectedItemPosition) {
            0 -> "da"; 1 -> "en"; else -> "auto"
        }
        prefs.edit().putString("language", langCode).apply()

        // Check overlay permission
        if (!Settings.canDrawOverlays(this)) {
            statusText.text = "Grant overlay permission"
            statusText.setTextColor(getColor(R.color.orange))
            startActivity(
                Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:$packageName")
                )
            )
            return
        }

        val serviceIntent = Intent(this, OverlayService::class.java)
        if (OverlayService.isRunning) {
            stopService(serviceIntent)
            startButton.text = "Start SpeakFlow"
            statusText.text = "Stopped"
            statusText.setTextColor(getColor(R.color.dim))
        } else {
            startForegroundService(serviceIntent)
            startButton.text = "Stop SpeakFlow"
            statusText.text = "Running \u2014 floating button active"
            statusText.setTextColor(getColor(R.color.green))
        }
    }

    override fun onResume() {
        super.onResume()
        if (OverlayService.isRunning) {
            startButton.text = "Stop SpeakFlow"
            statusText.text = "Running \u2014 floating button active"
            statusText.setTextColor(getColor(R.color.green))
        } else {
            startButton.text = "Start SpeakFlow"
            statusText.text = "Ready"
            statusText.setTextColor(getColor(R.color.accent))
        }
    }
}
