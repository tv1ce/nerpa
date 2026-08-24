# NERPA Склад — Android-приложение (WebView)

Нативная обёртка над мобильным интерфейсом склада NERPA. Приложение открывает
`/warehouse/` на сервере NERPA в локальной сети и работает как обычное Android-приложение:
иконка на экране, полноэкранный запуск, pull-to-refresh, кнопка «назад».

## Как это работает

- При первом запуске спрашивает **адрес сервера** (например `http://192.168.1.50:8080`)
  и сохраняет его. Это удобно, когда IP сервера меняется — не нужно пересобирать APK.
- Если сервер недоступен — показывает экран ошибки с кнопками **«Повторить»** и
  **«Сменить адрес сервера»**.
- Разрешён HTTP (cleartext) — сервер в локальной сети работает без HTTPS.

## Где взять готовый APK (без установки Android Studio)

APK собирается автоматически в **GitHub Actions** (workflow `.github/workflows/android.yml`):

1. Откройте вкладку **Actions** в репозитории на GitHub.
2. Выберите запуск **«Android APK (NERPA Склад)»** (запускается при изменениях в `android/`
   или вручную кнопкой **Run workflow**).
3. Внизу страницы запуска скачайте артефакт **`tms-sklad-debug-apk`** → это файл `app-debug.apk`.
4. Перешлите `app-debug.apk` кладовщику (Telegram/почта), он открывает файл и ставит
   (нужно разрешить «Установка из неизвестных источников»).

## Сборка локально

Нужен JDK 17 и Android SDK (или Android Studio).

```bash
cd android
# через Android Studio: File → Open → выбрать папку android/, затем Run
# или из консоли (Android Studio сам создаст gradle wrapper при первом sync):
gradle assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

## Автообновление приложения

Приложение при каждом запуске запрашивает `<server>/app/version.json` и сравнивает
`versionCode` с установленным. Если на сервере новее — показывает диалог
«Доступно обновление» → скачивает APK с `<server>/app/download` → запускает установку.

Сервер отдаёт файлы из папки **`app_dist/`** (рядом с `tms.db`):
- `app_dist/version.json` — версия (в git, редактируется при релизе)
- `app_dist/tms-sklad.apk` — сам файл (в git **не** хранится, кладётся на сервер вручную)

### Как выпустить новую версию

1. Поднять версию в `android/app/build.gradle` → `versionCode` (например 3) и `versionName`.
2. Запушить — GitHub Actions соберёт новый `app-debug.apk` (вкладка Actions → артефакт).
3. Скачать APK, положить на сервер как `app_dist/tms-sklad.apk`.
4. Обновить `app_dist/version.json` — выставить тот же `versionCode` (3), `versionName`
   и `notes` (что нового).
5. У кладовщиков при следующем заходе появится предложение обновиться.

> Важно: `versionCode` в `version.json` должен совпадать с `versionCode` собранного APK,
> иначе после установки приложение снова будет считать, что есть обновление.

## Параметры

- `applicationId`: `ru.tms.warehouse`
- `minSdk`: 24 (Android 7.0+) · `targetSdk`/`compileSdk`: 34
- Подпись release-сборки debug-ключом — чтобы APK ставился без keystore.

## Структура

```
android/
├── app/
│   ├── build.gradle
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/ru/tms/warehouse/
│       │   ├── MainActivity.kt     # WebView + обработка ошибок/файлов/навигации
│       │   ├── SetupActivity.kt    # экран ввода адреса сервера
│       │   └── Prefs.kt
│       └── res/                    # иконки, строки, темы, layouts, network config
├── build.gradle
├── settings.gradle
└── gradle.properties
```
