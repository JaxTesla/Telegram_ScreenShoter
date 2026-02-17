import asyncio
import os
import sys
import configparser
import ctypes
from ctypes import wintypes
from datetime import datetime
from io import BytesIO
from typing import List, Tuple, Optional, Callable, Dict
from pathlib import Path
import contextlib

# Основные библиотеки для бота
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile, BufferedInputFile, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Библиотеки для скриншотов
import pyautogui
import pygetwindow as gw
from PIL import Image, ImageDraw, ImageFont, ImageGrab

# Библиотеки для работы с системой
import psutil
import win32gui
import win32ui
import win32con
import win32api


def load_settings(path: str = "settings.ini") -> Tuple[str, List[int]]:
    """Загрузка настроек бота из файла settings.ini с поддержкой PyInstaller.

    Ищет файл в следующем порядке:
    1) Точный путь/относительно текущей рабочей директории
    2) Рядом с исполняемым файлом (для onefile/onedir)
    3) В каталоге распаковки PyInstaller (_MEIPASS), если передан --add-data
    Ожидается секция [telegram] с ключами bot_token и allowed_users.
    """
    config = configparser.ConfigParser()

    # Список путей для поиска файла
    candidate_paths: List[Path] = []

    # 1) Точный путь (если передан абсолютный или относительный)
    p = Path(path)
    if p.is_absolute() and p.is_file():
        candidate_paths.append(p)
    else:
        # 2) Текущая рабочая директория
        candidate_paths.append(Path.cwd() / path)

        # 3) Каталог exe (PyInstaller onefile/onedir)
        try:
            exe_dir = Path(sys.executable).parent if getattr(
                sys, "frozen", False) else Path(__file__).parent
            candidate_paths.append(exe_dir / path)
        except Exception:
            pass

        # 4) Каталог _MEIPASS (если --add-data)
        try:
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidate_paths.append(Path(meipass) / path)
        except Exception:
            pass

    # Ищем файл по списку путей
    for cp in candidate_paths:
        try:
            if cp.is_file():
                with cp.open("r", encoding="utf-8-sig") as cfg_file:
                    config.read_file(cfg_file)
                break
        except (FileNotFoundError, PermissionError):
            continue
        except (configparser.Error, UnicodeDecodeError):
            continue
    else:
        # Не нашли файл
        print(f"❌ Файл settings.ini не найден. Искали в:")
        for cp in candidate_paths:
            print(f"   - {cp} (существует: {cp.is_file()})")
        return "", []

    token = config.get("telegram", "bot_token", fallback="").strip()
    users_raw = config.get("telegram", "allowed_users", fallback="").strip()

    allowed_users: List[int] = []
    if users_raw:
        for part in users_raw.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                allowed_users.append(int(part))
            except ValueError:
                # Пропускаем некорректные значения
                continue

    return token, allowed_users


def get_virtual_screen_bounds() -> Tuple[int, int, int, int]:
    """Возвращает координаты виртуального экрана Windows (x, y, w, h).

    Учитывает мультимониторную конфигурацию, где x/y могут быть отрицательными.
    """
    user32 = ctypes.windll.user32
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return int(vx), int(vy), int(vw), int(vh)


def clamp(value: int, min_value: int, max_value: int) -> int:
    """Ограничение значения в границах [min_value, max_value]."""
    return max(min_value, min(value, max_value))


_CTRL_HANDLER_REF = None  # Храним ссылку на обработчик, чтобы GC не удалил callback


def install_windows_console_ctrl_handler(loop: asyncio.AbstractEventLoop, on_stop: Callable[[], None]) -> None:
    """Устанавливает обработчик закрытия консольного окна на Windows.

    Реагирует на CTRL_C/CTRL_CLOSE/CTRL_LOGOFF/CTRL_SHUTDOWN и инициирует мягкую остановку.
    """
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        CTRL_C_EVENT = 0
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        def handler(ctrl_type: int) -> bool:
            if ctrl_type in (CTRL_C_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    loop.call_soon_threadsafe(on_stop)
                except Exception:
                    pass
                # Сообщаем ОС, что событие обработано, даём время на завершение
                return True
            return False

        routine = HANDLER_ROUTINE(handler)
        ok = kernel32.SetConsoleCtrlHandler(routine, True)
        if ok == 0:
            return
        global _CTRL_HANDLER_REF
        _CTRL_HANDLER_REF = routine
    except Exception:
        # В случае ошибки просто не устанавливаем обработчик
        pass


async def prepare_window(window: gw.Win32Window, wait_seconds: float = 0.5) -> None:
    """Готовит окно к съемке без изменения его размеров.

    Восстанавливает только если окно действительно свернуто, затем активирует и ждёт стабилизацию.
    """
    try:
        # Восстанавливаем только когда окно свернуто (чтобы не сбрасывать максимизацию/размер)
        try:
            is_minimized = getattr(window, "isMinimized", False)
        except Exception:
            is_minimized = False

        if is_minimized:
            with contextlib.suppress(Exception):
                window.restore()

        # Активируем окно (без изменения размеров)
        with contextlib.suppress(Exception):
            window.activate()

        # Небольшая задержка, чтобы ОС успела применить фокус/координаты
        await asyncio.sleep(wait_seconds)
    except Exception:
        # Игнорируем подготовительные ошибки — ниже есть ретраи
        pass


def get_window_hwnd(window: gw.Win32Window) -> Optional[int]:
    """Возвращает HWND окна из pygetwindow.Win32Window, если доступен."""
    try:
        hwnd = getattr(window, "_hWnd", None)
        if isinstance(hwnd, int) and hwnd != 0:
            return hwnd
    except Exception:
        pass
    return None


def capture_window_via_printwindow(window: gw.Win32Window) -> Optional[Image.Image]:
    """Пытается захватить изображение окна через PrintWindow без активации.

    Возвращает PIL.Image при успехе, иначе None.
    """
    hwnd = get_window_hwnd(window)
    if not hwnd:
        return None
    try:
        # Получаем размер окна
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = max(1, right - left)
        height = max(1, bottom - top)

        # Создаем контексты и bitmap
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)

        # Флаг 2 = PW_RENDERFULLCONTENT (если поддерживается окном)
        PW_RENDERFULLCONTENT = 2
        result = win32gui.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if result != 1:
            # Попробуем без флага
            result = win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
            if result != 1:
                # Очистка ресурсов
                win32gui.DeleteObject(save_bitmap.GetHandle())
                save_dc.DeleteDC()
                mfc_dc.DeleteDC()
                win32gui.ReleaseDC(hwnd, hwnd_dc)
                return None

        bmp_info = save_bitmap.GetInfo()
        bmp_str = save_bitmap.GetBitmapBits(True)

        # Конвертируем BGRX -> RGB
        img = Image.frombuffer(
            "RGB",
            (bmp_info['bmWidth'], bmp_info['bmHeight']),
            bmp_str,
            "raw",
            "BGRX",
            0,
            1,
        )

        # Очистка ресурсов
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return img
    except Exception:
        # На ошибке — вернем None, оставив фолбэки
        return None


async def bring_window_to_front_no_resize(window: gw.Win32Window) -> None:
    """Поднимает окно на передний план без изменения размеров (через TOPMOST/NOTOPMOST)."""
    hwnd = get_window_hwnd(window)
    if not hwnd:
        return
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
    try:
        # Временно делаем TOPMOST, затем убираем — окно оказывается сверху
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
        await asyncio.sleep(0.05)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    except Exception:
        pass


def grab_virtual_screen(include_layered: bool = True) -> Image.Image:
    """Снимает всю виртуальную поверхность (учет всех мониторов)."""
    try:
        return ImageGrab.grab(all_screens=True, include_layered_windows=include_layered)
    except TypeError:
        return ImageGrab.grab(all_screens=True)
    except Exception:
        return pyautogui.screenshot()


def crop_fullscreen_to_window(full_img: Image.Image, window: gw.Win32Window) -> Image.Image:
    """Обрезает полноэкранный снимок до границ окна с учетом виртуального экрана."""
    vx, vy, vw, vh = get_virtual_screen_bounds()

    left = int(window.left)
    top = int(window.top)
    right = left + int(window.width)
    bottom = top + int(window.height)

    offset_x = -vx
    offset_y = -vy

    x1 = left + offset_x
    y1 = top + offset_y
    x2 = right + offset_x
    y2 = bottom + offset_y

    img_w, img_h = full_img.size
    x1c = clamp(x1, 0, img_w)
    y1c = clamp(y1, 0, img_h)
    x2c = clamp(x2, 0, img_w)
    y2c = clamp(y2, 0, img_h)

    if x2c <= x1c or y2c <= y1c:
        return full_img

    return full_img.crop((x1c, y1c, x2c, y2c))


async def capture_window_image(target_window: gw.Win32Window, retries: int = 3) -> Image.Image:
    """Пытается захватить изображение окна с несколькими ретраями.

    Основной путь — полный снимок + кроп по границам окна (устойчивее на Windows),
    фолбэк — региональный скриншот, если кроп по каким-то причинам не подходит.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            await bring_window_to_front_no_resize(target_window)
            await prepare_window(target_window, wait_seconds=0.25)

            # Основной путь: полный снимок со всех мониторов с последующим кропом
            full_img = grab_virtual_screen()
            cropped = crop_fullscreen_to_window(full_img, target_window)

            # Проверим, что результирующая область не пустая
            if cropped.width > 5 and cropped.height > 5:
                return cropped

            # Если по размерам получилось что-то неадекватное, попробуем фолбэк
            vx, vy, vw, vh = get_virtual_screen_bounds()
            left = clamp(int(target_window.left), vx, vx + vw)
            top = clamp(int(target_window.top), vy, vy + vh)
            width = clamp(int(target_window.width), 1, vw)
            height = clamp(int(target_window.height), 1, vh)

            # Попытка через Pillow (координаты виртуального экрана)
            try:
                bbox = (left, top, left + width, top + height)
                fallback_img = ImageGrab.grab(bbox=bbox, all_screens=True)
                if fallback_img.width > 5 and fallback_img.height > 5:
                    return fallback_img
            except Exception:
                pass

            # Фолбэк: региональный скриншот pyautogui (смещение в координаты pyautogui)
            offset_x = -vx
            offset_y = -vy
            region = (
                left + offset_x,
                top + offset_y,
                width,
                height,
            )

            fallback_img = pyautogui.screenshot(region=region)
            if fallback_img.width > 5 and fallback_img.height > 5:
                return fallback_img

        except Exception as e:
            last_error = e
            # Небольшая пауза перед следующей попыткой
            await asyncio.sleep(0.25)

    # Если все попытки провалились — пробрасываем последнюю ошибку
    if last_error:
        raise last_error
    raise RuntimeError("Не удалось получить изображение окна")


class ScreenshotBot:
    def __init__(self, token: str, allowed_users: list = None):
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        self.allowed_users = allowed_users or []
        self.stop_event = asyncio.Event()
        self.window_index_map: Dict[Tuple[int, int], List[str]] = {}

        # Регистрация обработчиков команд
        self.dp.message(Command("start"))(self.start_command)
        self.dp.message(Command("screenshot"))(self.full_screenshot)
        self.dp.message(Command("window"))(self.window_screenshot)
        self.dp.message(Command("windows"))(self.list_windows)
        self.dp.message(Command("help"))(self.help_command)
        # Регистрация обработчика callback для выбора окна
        self.dp.callback_query(F.data.startswith(
            "shot:"))(self.window_button_handler)

    def check_user_access(self, user_id: int) -> bool:
        """Проверка доступа пользователя к боту"""
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    async def start_command(self, message: Message):
        """Команда /start"""
        if not self.check_user_access(message.from_user.id):
            await message.reply("У вас нет доступа к этому боту.")
            return

        welcome_text = """
🖥️ **Добро пожаловать в Screenshot Bot!**

Доступные команды:
• /screenshot - Полный скриншот экрана
• /window <название> - Скриншот конкретного окна
• /windows - Список открытых окон
• /help - Помощь

Бот может делать скриншоты всего экрана или отдельных приложений.
        """
        await message.reply(welcome_text, parse_mode="Markdown")

    async def full_screenshot(self, message: Message):
        """Команда /screenshot - полный скриншот экрана"""
        if not self.check_user_access(message.from_user.id):
            await message.reply("У вас нет доступа к этому боту.")
            return

        try:
            await message.reply("📸 Делаю скриншот экрана...")

            # Делаем скриншот
            screenshot = pyautogui.screenshot()

            # Конвертируем в BytesIO для отправки
            bio = BytesIO()
            screenshot.save(bio, format='PNG')
            bio.seek(0)

            # Создаем имя файла с временной меткой
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"

            # Отправляем скриншот
            screenshot_file = BufferedInputFile(
                bio.getvalue(), filename=filename)
            await message.reply_photo(
                photo=screenshot_file,
                caption=f"🖥️ Скриншот экрана\n🕐 {datetime.now().strftime('%H:%M:%S')}"
            )

        except Exception as e:
            await message.reply(f"❌ Ошибка при создании скриншота: {str(e)}")

    async def window_screenshot(self, message: Message):
        """Команда /window - скриншот конкретного окна"""
        if not self.check_user_access(message.from_user.id):
            await message.reply("У вас нет доступа к этому боту.")
            return

        # Получаем название окна из команды
        command_parts = message.text.split(maxsplit=1)
        if len(command_parts) < 2:
            await message.reply(
                "❌ Укажите название окна.\n"
                "Пример: `/window Chrome`\n"
                "Используйте /windows для списка открытых окон.",
                parse_mode="Markdown"
            )
            return

        window_name = command_parts[1]

        try:
            await message.reply(f"🔍 Ищу окно '{window_name}'...")

            # Получаем все окна
            windows = gw.getAllWindows()
            target_window = None

            # Ищем окно по частичному совпадению названия
            for window in windows:
                if (window_name.lower() in window.title.lower() and
                        window.visible and window.width > 0 and window.height > 0):
                    target_window = window
                    break

            if not target_window:
                await message.reply(
                    f"❌ Окно '{window_name}' не найдено.\n"
                    "Используйте /windows для списка доступных окон."
                )
                return

            # Захватываем изображение окна устойчивым способом (ретраи, кроп)
            screenshot = await capture_window_image(target_window, retries=3)

            # Конвертируем в BytesIO
            bio = BytesIO()
            screenshot.save(bio, format='PNG')
            bio.seek(0)

            # Создаем имя файла
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(
                c for c in target_window.title if c.isalnum() or c in (' ', '-', '.'))[:20]
            filename = f"window_{safe_name}_{timestamp}.png"

            # Отправляем скриншот
            screenshot_file = BufferedInputFile(
                bio.getvalue(), filename=filename)
            await message.reply_photo(
                photo=screenshot_file,
                caption=f"🪟 Окно: {target_window.title}\n🕐 {datetime.now().strftime('%H:%M:%S')}"
            )

        except Exception as e:
            # Специальная подсказка для Windows-кода 258 (таймаут)
            err_text = str(e)
            hint = ""
            if "258" in err_text:
                hint = (
                    "\n\n💡 Подсказки: проверьте, что окно не защищено UAC/экраном, "
                    "не запущено с другими привилегиями, и не перекрыто системными окнами. "
                    "Попробуйте развернуть окно на главный монитор."
                )
            await message.reply(f"❌ Ошибка при создании скриншота окна: {err_text}{hint}")

    async def list_windows(self, message: Message):
        """Команда /windows - список открытых окон"""
        if not self.check_user_access(message.from_user.id):
            await message.reply("У вас нет доступа к этому боту.")
            return

        try:
            windows = gw.getAllWindows()
            visible_windows = [
                w for w in windows
                if w.visible and w.width > 0 and w.height > 0 and w.title.strip()
            ]

            if not visible_windows:
                await message.reply("❌ Открытых окон не найдено.")
                return

            titles: List[str] = [w.title.strip() for w in visible_windows[:25]]

            # Формируем инлайн-клавиатуру: 1 кнопка = 1 окно
            kb = InlineKeyboardBuilder()
            for idx, title in enumerate(titles):
                btn_text = title[:64]  # ограничение длины текста кнопки
                kb.button(text=btn_text, callback_data=f"shot:{idx}")
            kb.adjust(1)

            sent = await message.reply(
                "🪟 Выберите окно:",
                reply_markup=kb.as_markup()
            )

            # Сохраняем соответствие для этого сообщения
            self.window_index_map[(sent.chat.id, sent.message_id)] = titles

        except Exception as e:
            await message.reply(f"❌ Ошибка при получении списка окон: {str(e)}")

    async def window_button_handler(self, callback: CallbackQuery):
        """Скриншот выбранного окна по нажатию инлайн-кнопки."""
        try:
            if not callback.message:
                await callback.answer()
                return

            if not self.check_user_access(callback.from_user.id):
                await callback.answer("Нет доступа", show_alert=True)
                return

            data = callback.data or ""
            if not data.startswith("shot:"):
                await callback.answer()
                return

            try:
                idx = int(data.split(":", 1)[1])
            except Exception:
                await callback.answer("Некорректный выбор", show_alert=True)
                return

            key = (callback.message.chat.id, callback.message.message_id)
            titles = self.window_index_map.get(key)
            if not titles or idx < 0 or idx >= len(titles):
                await callback.answer("Список устарел, обновите /windows", show_alert=True)
                return

            selected_title = titles[idx]
            await callback.answer("Снимаю окно…", show_alert=False)

            # Поиск окна по точному названию, затем по частичному совпадению
            windows = gw.getAllWindows()
            target_window = None
            for w in windows:
                if (w.visible and w.width > 0 and w.height > 0 and w.title.strip() == selected_title):
                    target_window = w
                    break
            if not target_window:
                for w in windows:
                    if (w.visible and w.width > 0 and w.height > 0 and w.title.strip() and
                            selected_title.lower() in w.title.lower()):
                        target_window = w
                        break

            if not target_window:
                await callback.message.reply("❌ Окно не найдено. Обновите /windows и попробуйте снова.")
                return

            screenshot = await capture_window_image(target_window, retries=3)

            bio = BytesIO()
            screenshot.save(bio, format='PNG')
            bio.seek(0)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(
                c for c in target_window.title if c.isalnum() or c in (' ', '-', '.'))[:20]
            filename = f"window_{safe_name}_{timestamp}.png"

            screenshot_file = BufferedInputFile(
                bio.getvalue(), filename=filename)
            await callback.message.reply_photo(
                photo=screenshot_file,
                caption=f"🪟 Окно: {target_window.title}\n🕐 {datetime.now().strftime('%H:%M:%S')}"
            )

        except Exception as e:
            err_text = str(e)
            hint = ""
            if "258" in err_text:
                hint = (
                    "\n\n💡 Подсказки: проверьте, что окно не защищено UAC/экраном, "
                    "не запущено с другими привилегиями, и не перекрыто системными окнами. "
                    "Попробуйте развернуть окно на главный монитор."
                )
            with contextlib.suppress(Exception):
                await callback.answer("Ошибка", show_alert=False)
            if callback.message:
                await callback.message.reply(f"❌ Ошибка при создании скриншота окна: {err_text}{hint}")

    async def help_command(self, message: Message):
        """Команда /help"""
        help_text = """
🤖 **Screenshot Bot - Помощь**

**Основные команды:**
• `/screenshot` - Полный скриншот экрана
• `/window` название окна - Скриншот конкретного окна
• `/windows` - Показать список открытых окон
• `/help` - Эта справка

**Примеры использования:**
• `/window Chrome` - скриншот браузера Chrome
• `/window Notepad` - скриншот блокнота
• `/window Telegram` - скриншот Telegram

**Безопасность:**
• Бот работает только с разрешенными пользователями
• Все скриншоты имеют временные метки
• Поддерживается только локальное управление

**Требования:**
• Python 3.8+
• aiogram, pyautogui, pygetwindow, Pillow
• Права доступа к экрану
        """
        await message.reply(help_text, parse_mode="Markdown")

    async def shutdown(self):
        """Корректное завершение работы: закрытие FSM-хранилища и HTTP-сессии бота."""
        # Закрываем FSM-хранилище, если есть и поддерживает закрытие
        storage = getattr(self.dp, "storage", None)
        try:
            if storage and hasattr(storage, "close"):
                await storage.close()
            if storage and hasattr(storage, "wait_closed"):
                await storage.wait_closed()
        except Exception:
            pass

        # Закрываем HTTP-сессию бота
        try:
            if hasattr(self.bot, "session") and self.bot.session:
                await self.bot.session.close()
        except Exception:
            pass

    def request_stop(self) -> None:
        """Инициирует остановку polling (используется из обработчиков сигналов/событий)."""
        if not self.stop_event.is_set():
            self.stop_event.set()

    async def start_polling(self):
        """Запуск бота"""
        print("🤖 Бот запущен и готов к работе!")
        loop = asyncio.get_running_loop()
        # Устанавливаем обработчик закрытия консольного окна Windows
        install_windows_console_ctrl_handler(loop, self.request_stop)

        poll_task = asyncio.create_task(self.dp.start_polling(self.bot))
        stop_wait_task = asyncio.create_task(self.stop_event.wait())
        try:
            done, pending = await asyncio.wait({poll_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED)
            # Если получили запрос на остановку — отменяем polling
            if stop_wait_task in done and not poll_task.done():
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Мягкое прекращение по отмене/Ctrl+C
            pass
        finally:
            print("🛑 Останавливаю бота, выполняю очистку ресурсов...")
            # Отменяем вспомогательную задачу ожидания, если она ещё активна
            if not stop_wait_task.done():
                stop_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_wait_task
            await self.shutdown()


# Функция для запуска бота
async def main():
    # Загружаем настройки из settings.ini
    BOT_TOKEN, ALLOWED_USERS = load_settings()

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден bot_token в settings.ini. Укажите токен в секции [telegram]."
        )

    bot = ScreenshotBot(BOT_TOKEN, ALLOWED_USERS)
    await bot.start_polling()


if __name__ == "__main__":
    # Отключаем сбой защиты в macOS
    pyautogui.FAILSAFE = False

    # Запускаем бота
    asyncio.run(main())
