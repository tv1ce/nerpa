package ru.tms.warehouse

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Проверка и установка обновлений приложения.
 * При запуске запрашивает <server>/app/version.json и сравнивает versionCode.
 * Если на сервере новее — предлагает скачать и установить APK с <server>/app/download.
 */
class UpdateChecker(private val activity: Activity, private val serverUrl: String) {

    fun check() {
        Thread {
            try {
                val text = httpGet("$serverUrl/app/version.json")
                val json = JSONObject(text)
                val latest = json.optInt("versionCode", 0)
                if (latest > BuildConfig.VERSION_CODE) {
                    val name = json.optString("versionName", "")
                    val notes = json.optString("notes", "")
                    activity.runOnUiThread { promptUpdate(name, notes) }
                }
            } catch (_: Exception) {
                // молча игнорируем — нет связи / нет опубликованной версии
            }
        }.start()
    }

    private fun promptUpdate(versionName: String, notes: String) {
        if (activity.isFinishing) return
        val msg = buildString {
            if (versionName.isNotEmpty()) append("Версия $versionName\n")
            if (notes.isNotEmpty()) append("\n$notes")
        }.ifEmpty { "Доступна новая версия приложения." }

        AlertDialog.Builder(activity)
            .setTitle("Доступно обновление")
            .setMessage(msg)
            .setPositiveButton("Обновить") { _, _ -> downloadAndInstall() }
            .setNegativeButton("Позже", null)
            .setCancelable(true)
            .show()
    }

    private fun downloadAndInstall() {
        Toast.makeText(activity, "Загрузка обновления…", Toast.LENGTH_SHORT).show()
        Thread {
            try {
                val apk = File(activity.cacheDir, "update.apk")
                downloadTo("$serverUrl/app/download", apk)
                activity.runOnUiThread { install(apk) }
            } catch (e: Exception) {
                activity.runOnUiThread {
                    Toast.makeText(activity, "Не удалось скачать обновление", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun install(apk: File) {
        if (activity.isFinishing) return
        val uri: Uri = FileProvider.getUriForFile(
            activity, "${activity.packageName}.fileprovider", apk
        )
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            activity.startActivity(intent)
        } catch (e: Exception) {
            Toast.makeText(activity, "Откройте файл вручную для установки", Toast.LENGTH_LONG).show()
        }
    }

    private fun httpGet(urlStr: String): String {
        val conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
            connectTimeout = 4000
            readTimeout = 4000
            requestMethod = "GET"
        }
        try {
            conn.inputStream.bufferedReader().use { return it.readText() }
        } finally {
            conn.disconnect()
        }
    }

    private fun downloadTo(urlStr: String, dest: File) {
        val conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
            connectTimeout = 8000
            readTimeout = 30000
            requestMethod = "GET"
        }
        try {
            conn.inputStream.use { input ->
                dest.outputStream().use { output -> input.copyTo(output) }
            }
        } finally {
            conn.disconnect()
        }
    }
}
