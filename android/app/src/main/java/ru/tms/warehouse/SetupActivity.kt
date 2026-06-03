package ru.tms.warehouse

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * Экран первого запуска / смены сервера.
 * Кладовщик вводит адрес сервера TMS в локальной сети (http://IP:порт),
 * адрес сохраняется в SharedPreferences и используется при каждом запуске.
 */
class SetupActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_setup)

        val input = findViewById<EditText>(R.id.serverUrl)
        val save = findViewById<Button>(R.id.btnSave)

        // Подставляем уже сохранённый адрес, если есть
        val prefs = getSharedPreferences(Prefs.NAME, MODE_PRIVATE)
        prefs.getString(Prefs.SERVER_URL, null)?.let { input.setText(it) }

        save.setOnClickListener {
            val url = normalize(input.text.toString())
            if (url == null) {
                Toast.makeText(this, R.string.setup_invalid, Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            prefs.edit().putString(Prefs.SERVER_URL, url).apply()
            startActivity(Intent(this, MainActivity::class.java))
            finish()
        }
    }

    /** Приводит ввод к корректному URL или возвращает null. */
    private fun normalize(raw: String): String? {
        var s = raw.trim()
        if (s.isEmpty() || s == "http://" || s == "https://") return null
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "http://$s"
        }
        // Убираем хвостовой слэш для единообразия
        return s.trimEnd('/')
    }
}
