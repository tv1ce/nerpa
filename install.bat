@echo off
echo ============================================
echo    NERPA -- Установка зависимостей
echo ============================================
echo.

python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo ============================================
echo    Готово! Для запуска используйте start.bat
echo ============================================
pause
