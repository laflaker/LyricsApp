##Диагностика SMTC - что именно плеер отдаёт Windows (а значит, и LyricsApp).
#Запуск:      python smtc_debug.py
#Остановка:   Ctrl+C
#Результат пишется и в консоль, и в smtc_debug.log - этот файл можно переслать.
##Смотри на строки "позиция". Если при играющем треке позиция не растёт или
##прыгает назад, плеер отдаёт в Windows сломанный таймлайн, и синхронизировать
##текст по нему невозможно LyricsApp в этом случае считает время сам.
    
import asyncio
import sys
import time

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
)

STATUS_NAMES = {
    0: "CLOSED",
    1: "OPENED",
    2: "CHANGING",
    3: "STOPPED",
    4: "PLAYING",
    5: "PAUSED",
}
PLAYING = 4
LOG_FILE = "smtc_debug.log"


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def out(text="", log=None):
    print(text)
    if log is not None:
        log.write(text + "\n")
        log.flush()


async def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    log = open(LOG_FILE, "w", encoding="utf-8")
    manager = await MediaManager.request_async()
    previous = {}
    out("Опрос SMTC каждую секунду. Ctrl+C - выход.", log)
    out("", log)
    while True:
        try:
            current = manager.get_current_session()
            sessions = list(manager.get_sessions())
        except Exception as exc:
            out(f"Не удалось получить сессии: {exc!r}", log)
            await asyncio.sleep(2)
            continue

        if not sessions:
            out(f"{time.strftime('%H:%M:%S')}  нет активных медиа-сессий - "
                "плеер не отдаёт данные в Windows", log)

        for session in sessions:
            try:
                info = await session.try_get_media_properties_async()
                timeline = session.get_timeline_properties()
                status = session.get_playback_info().playback_status
                app_id = session.source_app_user_model_id or "?"
                title = info.title or ""
                artist = info.artist or ""
                pos = timeline.position.total_seconds()
                duration = (timeline.end_time - timeline.start_time).total_seconds()

                now = time.monotonic()
                rate_text = ""
                prev = previous.get(app_id)
                if prev is not None and now - prev[0] > 1.0:
                    rate = (pos - prev[1]) / (now - prev[0])
                    rate_text = f"  скорость: {rate:.2f}x"
                    if status == PLAYING and not (0.5 <= rate <= 1.5):
                        rate_text += "  <-- ТАЙМЛАЙН НЕ В ПОРЯДКЕ"
                previous[app_id] = (now, pos)

                marker = "*" if session is current else " "
                out(f"{time.strftime('%H:%M:%S')}{marker} {app_id}", log)
                out(f"    статус: {STATUS_NAMES.get(status, status)}  "
                    f"позиция: {fmt_time(pos)} / {fmt_time(duration)}{rate_text}", log)
                out(f"    трек: {artist!r} - {title!r}", log)
            except Exception as exc:
                out(f"{time.strftime('%H:%M:%S')}  ошибка чтения сессии: {exc!r}", log)

        out("", log)
        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Файл smtc_debug.log сохранён рядом со скриптом.")
