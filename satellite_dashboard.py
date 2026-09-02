import subprocess
import sys
import os
import re
import math
import socket
import struct
import select
import time
import json
from datetime import datetime
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout
from rich.table import Table
from collections import deque

console = Console()

# ==========================================
# НАСТРОЙКИ САТЕЛЛИТА (WYOMING PROTOCOL)
# ==========================================

# --- Основные параметры ---
SAT_NAME   = "pi3b_satellite"  # Имя сателлита, которое будет отображаться в Home Assistant
SAT_URI    = "tcp://0.0.0.0"   # Адрес прослушивания (0.0.0.0 означает все доступные сетевые интерфейсы)
SAT_PORT   = 10700             # Локальный порт для подключения Home Assistant

# --- Подключение к Home Assistant ---
HA_HOST = "192.168.0.15"       # IP-адрес сервера Home Assistant
HA_PORT = 10300                # Порт интеграции Wyoming Protocol в Home Assistant

# --- Аудио устройства (ALSA) ---
# Используйте 'arecord -l' и 'aplay -l' в терминале для поиска правильных значений
MIC_DEVICE = "plughw:2,0"      # Устройство микрофона (plughw выполняет автоматическое преобразование формата)
SND_DEVICE = "plughw:0,0"      # Устройство вывода звука (динамика)
MIC_RATE   = 16000             # Частота дискретизации микрофона (стандарт для распознавания речи)
SND_RATE   = 22050             # Частота дискретизации динамика (баланс качества и нагрузки на CPU Pi 3B)
VOLUME_STEP = 5                # Шаг изменения громкости по клавишам '+' и '-'

# --- Оптимизация для Raspberry Pi 3B (1 ГБ ОЗУ, 4 ядра) ---
MIC_BUFFER_SEC = 0.05          # Размер аудио-буфера микрофона. Меньше = ниже задержка, но выше нагрузка на CPU.
SND_BUFFER_SEC = 0.1           # Размер аудио-буфера динамика. Больше = стабильнее звук, меньше треска.
MIC_CHANNELS = 1               # Моно-запись (достаточно для распознавания речи, экономит ресурсы)
SND_CHANNELS = 1               # Моно-воспроизведение

# --- Усиление звука (программное) ---
MIC_VOLUME_MULTIPLIER = 2.0    # Множитель громкости микрофона (полезно для тихих USB-микрофонов)
SND_VOLUME_MULTIPLIER = 1.5    # Множитель громкости динамика

# --- Wake Word (Ключевое слово для активации) ---
ENABLE_WAKE_WORD = False       # Включить локальное распознавание ключевого слова (требует wyoming-openwakeword)
WAKE_WORD_HOST = "192.168.0.15"# IP-адрес, где запущен сервис openWakeWord
WAKE_WORD_PORT = 10400         # Порт сервиса openWakeWord
WAKE_WORD_NAME = "Alexa"       # Название модели wake-word (например, 'ok_nabu', 'hey_jarvis')

# --- Переподключение при обрыве связи ---
AUTO_RECONNECT = True          # Автоматически пытаться восстановить соединение с HA
RECONNECT_SECONDS = 5          # Пауза между попытками переподключения (в секундах)

# --- Отладка ---
DEBUG_MODE = False             # Включить подробный вывод (полезно при поиске неисправностей)
LOG_LEVEL = "INFO"             # Уровень логирования: DEBUG, INFO, WARNING, ERROR

# --- Команда запуска wyoming_satellite ---
# Формируем аргументы командной строки для запуска модуля как подпроцесса
SAT_COMMAND = [
    sys.executable, "-m", "wyoming_satellite",
    "--name", SAT_NAME,
    "--uri", f"{SAT_URI}:{SAT_PORT}",
    "--mic-command", f"arecord -D {MIC_DEVICE} -r {MIC_RATE} -c 1 -f S16_LE -t raw",
    "--snd-command", f"aplay -D {SND_DEVICE} -r {SND_RATE} -c 1 -f S16_LE -t raw",
    "--wake-uri", f"tcp://{WAKE_WORD_HOST}:{WAKE_WORD_PORT}",
    "--wake-word-name", WAKE_WORD_NAME,
]

# ==========================================
# УПРАВЛЕНИЕ ГРОМКОСТЬЮ (ALSA MIXER)
# ==========================================

def get_mixer_controls():
    """Получает список доступных элементов управления громкостью для указанного аудиоустройства"""
    try:
        result = subprocess.run(
            ['amixer', '-D', SND_DEVICE, 'scontrols'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            controls = []
            for line in result.stdout.splitlines():
                # Ищем паттерн: Simple mixer control 'Master',0
                match = re.search(r"Simple mixer control '([^']+)'", line)
                if match:
                    controls.append(match.group(1))
            return controls
    except Exception:
        pass
    return []

def find_volume_control():
    """Находит наиболее подходящий элемент управления громкостью по приоритету"""
    controls = get_mixer_controls()
    # Приоритет поиска: Master (главный) > Speaker (динамики) > PCM (цифровой) > первый попавшийся
    for name in ['Master', 'Speaker', 'PCM']:
        if name in controls:
            return name
    if controls:
        return controls[0]
    return None

# Инициализируем имя элемента управления громкостью при загрузке
VOLUME_CONTROL = find_volume_control()

def get_volume():
    """Возвращает текущий уровень громкости в процентах"""
    if not VOLUME_CONTROL:
        return 50
    try:
        result = subprocess.run(
            ['amixer', '-D', SND_DEVICE, 'get', VOLUME_CONTROL],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            # Ищем значение в формате [XX%]
            match = re.search(r'\[(\d+)%\]', result.stdout)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return 50

def set_volume(percent):
    """Устанавливает уровень громкости (ограничивает диапазон от 0 до 100)"""
    if not VOLUME_CONTROL:
        return False
    percent = max(0, min(100, percent))
    try:
        subprocess.run(
            ['amixer', '-D', SND_DEVICE, 'set', VOLUME_CONTROL, f'{percent}%'],
            capture_output=True, timeout=3
        )
        return True
    except Exception:
        return False

def volume_bar(percent, width=20):
    """Генерирует строку-индикатор (прогресс-бар) для отображения громкости"""
    filled = int(percent / 100 * width)
    return '█' * filled + '░' * (width - filled)

# ==========================================
# РАБОТА СО ЗВУКОВЫМИ ФАЙЛАМИ
# ==========================================
SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sound")

def find_sound(keywords, default_name):
    """Ищет звуковой файл по ключевым словам в имени или возвращает имя по умолчанию"""
    path = os.path.join(SOUND_DIR, default_name)
    if os.path.exists(path):
        return path
    if os.path.isdir(SOUND_DIR):
        for f in sorted(os.listdir(SOUND_DIR)):
            if f.lower().endswith(('.wav', '.wave')):
                low = f.lower()
                if any(k in low for k in keywords):
                    return os.path.join(SOUND_DIR, f)
    return None

# Предварительный поиск системных звуков при инициализации
SOUND_HELLO = find_sound(["привет", "hello", "greet", "welcome"], "41.wav")
SOUND_DONE  = find_sound(["оконч", "заверш", "done", "finish", "check"], "47.wav")
SOUND_READY = find_sound(["готов", "ready", "work", "работа"], "50.wav")
SOUND_STOP  = find_sound(["стоп", "stop", "пока", "bye", "выкл", "конец"], "46.wav")
SOUND_TEST  = os.path.join(SOUND_DIR, "39.wav")

def play_wav(path, block=True):
    """Воспроизводит WAV-файл через aplay. Если block=True, ждет окончания воспроизведения."""
    if not path or not os.path.exists(path):
        return False
    try:
        p = subprocess.Popen(
            ['aplay', '-q', '-D', SND_DEVICE, path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if block:
            p.wait()
        return True
    except Exception:
        return False

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ
# ==========================================

def get_local_ip():
    """Определяет локальный IP-адрес устройства (через фиктивное подключение к Google DNS)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def parse_devices(output):
    """Парсит вывод команд arecord -l / aplay -l в структурированный список словарей"""
    devices = []
    for line in output.splitlines():
        m = re.search(r'card\s+(\d+):\s*(.+?),\s*device\s+(\d+):\s*(.*)', line)
        if m:
            card, cname, dev, dname = m.groups()
            devices.append({
                "id": f"{card},{dev}",
                "desc": f"card {card}: {cname.strip()} | dev {dev}: {dname.strip()}"
            })
    return devices

def get_audio_devices():
    """Возвращает словарь с доступными микрофонами и динамиками"""
    devices = {"mic": [], "speaker": []}
    try:
        r = subprocess.run(['arecord', '-l'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0: devices["mic"] = parse_devices(r.stdout)
    except Exception: pass
    try:
        r = subprocess.run(['aplay', '-l'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0: devices["speaker"] = parse_devices(r.stdout)
    except Exception: pass
    return devices

def test_module():
    """Проверяет, установлен ли и доступен ли модуль wyoming_satellite"""
    r = subprocess.run([sys.executable, "-c", "import wyoming_satellite"], capture_output=True)
    return r.returncode == 0

def test_port_free(port):
    """Проверяет, свободен ли указанный TCP-порт"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()

def vol_bar(rms, width=25):
    """Генерирует индикатор уровня громкости (RMS) для диагностики микрофона"""
    level = min(1.0, rms / 4000.0) # Нормализация (4000 - условный порог громкого звука)
    filled = int(level * width)
    return '█' * filled + '░' * (width - filled)

def record_rms(seconds=2.5, live_cb=None):
    """Записывает звук с микрофона в течение заданного времени и вычисляет максимальный RMS (уровень сигнала)"""
    proc = subprocess.Popen(
        ['arecord', '-D', MIC_DEVICE, '-r', str(MIC_RATE), '-c', '1', '-f', 'S16_LE', '-t', 'raw'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    max_rms = 0
    start = time.time()
    chunk = (MIC_RATE // 10) * 2 # Читаем кусками по 0.1 секунды
    while time.time() - start < seconds:
        data = proc.stdout.read(chunk)
        if not data: break
        n = len(data) // 2
        samples = struct.unpack(f'<{n}h', data)
        rms = math.sqrt(sum(s * s for s in samples) / n) if n else 0
        max_rms = max(max_rms, rms)
        if live_cb: live_cb(rms) # Вызов функции обратного вызова для обновления UI в реальном времени
    proc.terminate()
    return max_rms

# ==========================================
# ДИАГНОСТИКА СИСТЕМЫ
# ==========================================

def draw_diag(entries):
    """Рисует таблицу диагностики с использованием библиотеки Rich"""
    t = Table(show_header=True, header_style="bold cyan", expand=True)
    t.add_column("Тест", ratio=2)
    t.add_column("Статус", width=10)
    t.add_column("Результат", ratio=3)
    for e in entries:
        t.add_row(e["name"], e["status"], e["detail"])
    return Panel(t, title="[bold cyan] ДИАГНОСТИКА СПУТНИКА[/bold cyan]", border_style="cyan")

def run_diagnostics():
    """Запускает серию проверок оборудования и окружения перед основным запуском"""
    dev = get_audio_devices()
    mic_id = MIC_DEVICE.replace("plughw:", "")
    snd_id = SND_DEVICE.replace("plughw:", "")

    entries = [
        {"name": "🐍 Модуль wyoming_satellite", "status": "[dim]—[/dim]", "detail": ""},
        {"name": f"🎤 Микрофон {MIC_DEVICE}",   "status": "[dim]—[/dim]", "detail": ""},
        {"name": f"🔊 Динамик {SND_DEVICE}",    "status": "[dim]—[/dim]", "detail": ""},
        {"name": "🎙️ Сигнал с микрофона",       "status": "[dim]—[/dim]", "detail": "говорите в микрофон!"},
        {"name": "🔉 Динамики (тестовый звук)", "status": "[dim]—[/dim]", "detail": ""},
        {"name": f"🔌 Порт {SAT_PORT}",          "status": "[dim]—[/dim]", "detail": ""},
        {"name": "🔉 Элемент громкости",         "status": "[dim]—[/dim]", "detail": ""},
    ]

    with Live(draw_diag(entries), console=console, refresh_per_second=12) as live:
        def upd(i, status, detail):
            entries[i]["status"], entries[i]["detail"] = status, detail
            live.update(draw_diag(entries))

        # 1. Проверка модуля
        upd(0, "[yellow]…[/yellow]", "проверка...")
        ok = test_module()
        upd(0, "[green]✅[/green]" if ok else "[red]❌[/red]",
            "модуль доступен" if ok else "не найден! Выполните: script/setup")
        time.sleep(0.2)

        # 2. Проверка микрофона
        ok = any(d["id"] == mic_id for d in dev["mic"])
        avail = ", ".join(d["id"] for d in dev["mic"]) or "нет устройств"
        upd(1, "[green]✅[/green]" if ok else "[red]❌[/red]",
            "устройство найдено" if ok else f"НЕ найдено! Доступны: {avail}")
        time.sleep(0.2)

        # 3. Проверка динамика
        ok = any(d["id"] == snd_id for d in dev["speaker"])
        avail = ", ".join(d["id"] for d in dev["speaker"]) or "нет устройств"
        upd(2, "[green]✅[/green]" if ok else "[red]❌[/red]",
            "устройство найдено" if ok else f"НЕ найдено! Доступны: {avail}")
        time.sleep(0.2)

        # 4. Проверка сигнала микрофона (RMS)
        upd(3, "[yellow]…[/yellow]", "говорите в микрофон!")
        def cb(rms):
            upd(3, "[yellow]…[/yellow]", f"[cyan]{vol_bar(rms)}[/cyan] rms={int(rms)}")
        max_rms = record_rms(2.5, cb)
        ok = max_rms > 300 # Порог чувствительности
        upd(3, "[green]✅[/green]" if ok else "[red]❌[/red]",
            f"макс. rms={int(max_rms)} — сигнал есть 👍" if ok else f"rms={int(max_rms)} — ТИШИНА! Проверьте микрофон")
        time.sleep(0.4)

        # 5. Проверка воспроизведения звука
        upd(4, "[yellow]…[/yellow]", "воспроизведение тестового звука...")
        ok = play_wav(SOUND_TEST)
        upd(4, "[green]✅[/green]" if ok else "[red]❌[/red]",
            "звук воспроизведен (слышно?)" if ok else f"файл не найден: {SOUND_TEST}")
        time.sleep(0.2)

        # 6. Проверка порта
        ok = test_port_free(SAT_PORT)
        upd(5, "[green]✅[/green]" if ok else "[red]❌[/red]",
            "порт свободен" if ok else "порт ЗАНЯТ! Остановите старый процесс")

        # 7. Проверка микшера громкости
        if VOLUME_CONTROL:
            vol = get_volume()
            upd(6, "[green]✅[/green]", f"найдено: {VOLUME_CONTROL}, текущая: {vol}%")
        else:
            upd(6, "[red]❌[/red]", "элемент управления не найден! Проверьте: amixer -D plughw:X,Y scontrols")

    failed = [e for e in entries if "❌" in e["status"]]
    if failed:
        console.print(f"\n[bold red]❌ Провалено тестов: {len(failed)}[/bold red]")
    else:
        console.print("\n[bold green]✅ Все тесты пройдены! Спутник готов к запуску.[/bold green]")
    return len(failed) == 0

# ==========================================
# ДАШБОРД (ИНТЕРФЕЙС)
# ==========================================

class SatelliteDashboard:
    def __init__(self):
        self.status = "🟡 Запуск..."
        self.state = "Инициализация систем"
        self.logs = deque(maxlen=10) # Храним только последние 10 строк лога для экономии памяти
        self.local_ip = get_local_ip()
        self.audio_devices = get_audio_devices()
        self.volume = get_volume()

    def change_volume(self, delta):
        """Изменяет громкость на заданный шаг"""
        new_vol = self.volume + delta
        if set_volume(new_vol):
            old_vol = self.volume
            self.volume = new_vol
            return f"🔉 Громкость: {old_vol}% → {self.volume}%"
        return "❌ Ошибка изменения громкости"

    def set_volume_absolute(self, percent):
        """Устанавливает абсолютное значение громкости"""
        if set_volume(percent):
            old_vol = self.volume
            self.volume = percent
            return f"🔉 Громкость: {old_vol}% → {self.volume}%"
        return "❌ Ошибка установки громкости"

    def parse_log(self, line):
        """
        Анализирует строку лога от wyoming_satellite.
        ЗДЕСЬ РЕАЛИЗОВАН ПЕРЕВОД ЛОГОВ НА РУССКИЙ ЯЗЫК:
        Мы проверяем наличие ключевых английских слов и заменяем статус на русский аналог.
        """
        line_lower = line.lower()
        
        # Словарь маппинга английских логов на русские статусы
        if "connected" in line_lower:
            self.status = "🟢 Подключено к Home Assistant"
            self.state = "🛑 Ожидание команды (Idle)"
        elif "error" in line_lower or "exception" in line_lower or "failed" in line_lower:
            self.status = "🔴 Ошибка соединения или выполнения"
        elif "wake word" in line_lower or "detected" in line_lower:
            self.state = "🗣️ Распознано ключевое слово!"
        elif "streaming" in line_lower:
            self.state = "🎙️ Передача звука на сервер"
        elif "playing" in line_lower or "response" in line_lower:
            self.state = "🔊 Воспроизведение ответа"
        elif "ready" in line_lower:
            self.state = "✅ Готов к работе"
        elif "listening" in line_lower:
            self.state = "👂 Слушаю..."

        # Форматирование строки лога для вывода (обрезаем лишнее, добавляем время)
        timestamp = datetime.now().strftime("%H:%M:%S")
        clean_line = line.strip()
        
        # Дополнительная очистка лога от технических префиксов Wyoming, если нужно
        if "INFO:" in clean_line:
            clean_line = clean_line.split("INFO:", 1)[1].strip()
            
        if clean_line:
            self.logs.append(f"[dim]{timestamp}[/dim] {clean_line}")

    def create_audio_table(self):
        """Генерирует таблицу доступных аудиоустройств с отметкой выбранных"""
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column("Устройства", ratio=1)
        table.add_row("[bold yellow]🎤 Микрофоны:[/bold yellow]")
        if self.audio_devices["mic"]:
            for d in self.audio_devices["mic"]:
                mark = "[bold green]✓[/bold green]" if d["id"] == MIC_DEVICE.replace("plughw:", "") else " "
                table.add_row(f"  {mark} [dim]{d['desc']}[/dim]")
        else:
            table.add_row("  [red]Не найдено[/red]")
        table.add_row("")
        table.add_row("[bold magenta]🔊 Динамики:[/bold magenta]")
        if self.audio_devices["speaker"]:
            for d in self.audio_devices["speaker"]:
                mark = "[bold green]✓[/bold green]" if d["id"] == SND_DEVICE.replace("plughw:", "") else " "
                table.add_row(f"  {mark} [dim]{d['desc']}[/dim]")
        else:
            table.add_row("  [red]Не найдено[/red]")
        return table

    def create_settings_table(self):
        """Генерирует таблицу с текущими настройками и инструкцией по подключению в HA"""
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column("Параметр", style="cyan", no_wrap=True)
        table.add_column("Значение", style="white")
        table.add_row(" Имя", SAT_NAME)
        table.add_row("🔌 URI", f"{SAT_URI}:{SAT_PORT}")
        table.add_row("🌐 IP Pi", self.local_ip)
        table.add_row("🎤 Микрофон", f"{MIC_DEVICE} @ {MIC_RATE} Hz")
        table.add_row("🔊 Динамик", f"{SND_DEVICE} @ {SND_RATE} Hz")
        table.add_row("🔉 Громкость", f"[bold]{self.volume}%[/bold] {volume_bar(self.volume)}")
        table.add_row("🎛️ Контроль", f"{VOLUME_CONTROL or 'не найден'}")
        table.add_row("", "")
        table.add_row("[bold green]Подключение в HA:[/bold green]", "")
        table.add_row("[dim]1.[/dim]", "Настройки → Устройства и сервисы")
        table.add_row("[dim]2.[/dim]", "Добавить интеграцию → Wyoming Protocol")
        table.add_row("[dim]3.[/dim]", f"Хост: [bold]{self.local_ip}[/bold] Порт: [bold]{SAT_PORT}[/bold]")
        return table

    def generate_layout(self):
        """Собирает все панели в единый макет (Layout) для библиотеки Rich"""
        header = Text("🛰️ WYOMING SATELLITE DASHBOARD", style="bold cyan")
        header_panel = Panel(header, style="cyan", expand=True)

        status_text = Text(
            f"Статус: {self.status}\nСостояние: {self.state}\n"
            "[dim]Клавиши: [+] громче | [-] тише | [t] тест | [r] устройства | [q] выход[/dim]",
            style="bold white")
        status_panel = Panel(status_text, title="[bold green]Система[/bold green]", border_style="green", expand=True)

        audio_panel = Panel(self.create_audio_table(), title="[bold blue]Аудио устройства[/bold blue]",
                            border_style="blue", expand=True)
        settings_panel = Panel(self.create_settings_table(), title="[bold magenta]Настройки спутника (HA)[/bold magenta]",
                               border_style="magenta", expand=True)
        
        logs_text = "\n".join(self.logs) if self.logs else "[dim]Ожидание логов...[/dim]"
        logs_panel = Panel(logs_text, title="[bold yellow]Живые логи (Live Logs)[/bold yellow]",
                           border_style="yellow", expand=True)

        layout = Layout()
        layout.split_column(
            Layout(header_panel, size=3),
            Layout(status_panel, size=6),
            Layout(name="middle", size=14),
            Layout(logs_panel),
        )
        layout["middle"].split_row(
            Layout(audio_panel, ratio=1),
            Layout(settings_panel, ratio=1),
        )
        return layout

# ==========================================
# ГЛАВНЫЙ ЦИКЛ ПРИЛОЖЕНИЯ
# ==========================================

def main():
    console.print(Panel("[bold cyan]🛰️ WYOMING SATELLITE DASHBOARD[/bold cyan]\nПредпусковая проверка системы",
                        border_style="cyan"))

    if not VOLUME_CONTROL:
        console.print("[bold red]⚠️ Элемент управления громкостью не найден![/bold red]")
        console.print("[dim]Выполните: amixer -D plughw:X,Y scontrols[/dim]")
        console.print("[dim]И измените VOLUME_CONTROL в коде вручную[/dim]\n")

    if play_wav(SOUND_HELLO):
        console.print("[dim]🔊 Приветствие воспроизведено[/dim]")
    else:
        console.print("[dim]⚠️ Звук приветствия не найден в папке sound/[/dim]")

    all_ok = run_diagnostics()
    if not all_ok:
        console.print("[yellow]⚠️ Есть проваленные тесты — спутник может работать некорректно.[/yellow]")

    play_wav(SOUND_DONE)

    try:
        input("[bold green]Нажмите Enter для запуска спутника...[/bold green]")
    except KeyboardInterrupt:
        console.print("\n[bold red]⛔ Выход.[/bold red]")
        return

    dashboard = SatelliteDashboard()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1" # Отключаем буферизацию вывода для мгновенного чтения логов

    # Запуск wyoming_satellite как подпроцесса
    process = subprocess.Popen(SAT_COMMAND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    fd_out = process.stdout.fileno()
    buf = b''

    play_wav(SOUND_READY, block=False)

    try:
        # Live-обновление интерфейса с частотой 4 кадра в секунду
        with Live(dashboard.generate_layout(), console=console, refresh_per_second=4, screen=True) as live:
            while True:
                # Проверка, не завершился ли подпроцесс неожиданно
                if process.poll() is not None:
                    dashboard.status = "🔴 Процесс сателлита завершен"
                    live.update(dashboard.generate_layout())
                    break

                # Неблокирующий опрос потоков: вывод подпроцесса и ввод пользователя (stdin)
                rl, _, _ = select.select([fd_out, sys.stdin], [], [], 0.1)

                # Обработка нажатий клавиш пользователя
                if sys.stdin in rl:
                    cmd = sys.stdin.readline().strip().lower()
                    ts = datetime.now().strftime("%H:%M:%S")
                    if cmd in ('q', 'й'):
                        raise KeyboardInterrupt
                    elif cmd == '+':
                        msg = dashboard.change_volume(VOLUME_STEP)
                        dashboard.logs.append(f"[dim]{ts}[/dim] {msg}")
                    elif cmd == '-':
                        msg = dashboard.change_volume(-VOLUME_STEP)
                        dashboard.logs.append(f"[dim]{ts}[/dim] {msg}")
                    elif cmd.startswith('v') or cmd.startswith('м'):
                        # Установка абсолютной громкости: v50 или м70
                        try:
                            vol = int(cmd[1:])
                            msg = dashboard.set_volume_absolute(vol)
                            dashboard.logs.append(f"[dim]{ts}[/dim] {msg}")
                        except ValueError:
                            pass
                    elif cmd in ('t', 'е'):
                        ok = play_wav(SOUND_TEST)
                        dashboard.logs.append(f"[dim]{ts}[/dim] 🔉 Тест динамиков: "
                                              + ("✅ звук проигран" if ok else "❌ файл не найден в sound/"))
                    elif cmd in ('r', 'к'):
                        dashboard.audio_devices = get_audio_devices()
                        dashboard.logs.append(f"[dim]{ts}[/dim] 🔄 Список устройств обновлен")
                    live.update(dashboard.generate_layout())

                # Обработка вывода (логов) от подпроцесса wyoming_satellite
                if fd_out in rl:
                    data = os.read(fd_out, 4096)
                    if not data:
                        continue
                    buf += data
                    # Разделяем буфер по строкам, обрабатываем полные строки
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        dashboard.parse_log(line.decode(errors='ignore'))
                        live.update(dashboard.generate_layout())

    except KeyboardInterrupt:
        pass

    console.print("\n[bold red]⛔ Остановка сателлита...[/bold red]")
    process.terminate()
    process.wait()
    play_wav(SOUND_STOP)

if __name__ == "__main__":
    main()
