У вас сборка шла системным Python (C:\Program Files\Python311), где нет aiogram. Нужно собирать тем Python, где стоят зависимости (ваш venv), и удалить старый spec.

Сделайте так в PowerShell:
1) Удалите старые артефакты:
```powershell
cd F:\YandexDisk\Cod\Python\TGM\Telegram_ScreenShoter
Remove-Item -Recurse -Force .\build, .\dist, .\ScreenshotBot.spec -ErrorAction SilentlyContinue
```

2) Убедитесь, что зависимости стоят в venv:
```powershell
C:\Users\Administrator\.virtualenvs\Telegram_ScreenShoter-ypZW3pLU\Scripts\python.exe -m pip install -r requirements.txt
```

3) Соберите EXE явным Python из venv (одной строкой):
```powershell
C:\Users\Administrator\.virtualenvs\Telegram_ScreenShoter-ypZW3pLU\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --name ScreenshotBot --hidden-import win32timezone --collect-all aiogram --collect-all PIL --collect-all magic_filter --paths . screenshot_bot.py
```
- Не добавляйте --add-data для settings.ini — он должен лежать рядом, отдельно.

4) Положите settings.ini рядом с dist\ScreenshotBot.exe и запустите EXE:
```powershell
.\dist\ScreenshotBot.exe
```

Полезные проверки:
- Где берётся pyinstaller:
```powershell
Get-Command pyinstaller
```
(Игнорируем его и всегда используем python.exe -m PyInstaller из venv, как выше.)
- Проверка, что aiogram есть в venv:
```powershell
C:\Users\Administrator\.virtualenvs\Telegram_ScreenShoter-ypZW3pLU\Scripts\python.exe -c "import aiogram,sys; print('aiogram:', aiogram.__file__); print('py:', sys.executable)"
```

Если всё ещё ругнётся на модули, добавьте к команде сборки:
- для aiohttp и друзей:
```powershell
--collect-all aiohttp --collect-all yarl --collect-all multidict --collect-all aiosignal
```

Итого: ключевой момент — собирать через ваш venv-питон (python.exe -m PyInstaller), а не через «pyinstaller» в PATH, и не встраивать settings.ini.