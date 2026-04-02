package com.speakflow.app

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class WhisperClient(private val apiKey: String) {

    companion object {
        val client: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    fun transcribe(wavData: ByteArray, language: String): String {
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("model", "whisper-1")
            .addFormDataPart(
                "file", "recording.wav",
                wavData.toRequestBody("audio/wav".toMediaType())
            )

        if (language != "auto") {
            body.addFormDataPart("language", language)
        }

        val request = Request.Builder()
            .url("https://api.openai.com/v1/audio/transcriptions")
            .header("Authorization", "Bearer $apiKey")
            .post(body.build())
            .build()

        val response = client.newCall(request).execute()
        return response.use {
            val responseBody = it.body?.string() ?: ""

            if (!it.isSuccessful) {
                val error = try {
                    JSONObject(responseBody).getJSONObject("error").getString("message")
                } catch (_: Exception) {
                    responseBody
                }
                throw RuntimeException(error)
            }

            JSONObject(responseBody).getString("text")
        }
    }

    fun cleanup(text: String, language: String): String {
        val systemPrompt = "You are a text cleanup assistant. Clean up the following speech " +
                "transcription. Fix punctuation, remove filler words (like 'um', 'uh', " +
                "'\u00f8h', 'alts\u00e5'), fix obvious speech-to-text errors, but preserve the " +
                "original meaning and language. Keep the same language as the input. " +
                "Output ONLY the cleaned text, nothing else."

        val messages = JSONArray().apply {
            put(JSONObject().put("role", "system").put("content", systemPrompt))
            put(JSONObject().put("role", "user").put("content", text))
        }

        val json = JSONObject().apply {
            put("model", "gpt-4o-mini")
            put("messages", messages)
        }

        val request = Request.Builder()
            .url("https://api.openai.com/v1/chat/completions")
            .header("Authorization", "Bearer $apiKey")
            .header("Content-Type", "application/json")
            .post(json.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val response = client.newCall(request).execute()
        return response.use {
            val responseBody = it.body?.string() ?: ""

            if (!it.isSuccessful) {
                return text
            }

            try {
                JSONObject(responseBody)
                    .getJSONArray("choices")
                    .getJSONObject(0)
                    .getJSONObject("message")
                    .getString("content")
                    .trim()
            } catch (_: Exception) {
                text
            }
        }
    }
}
