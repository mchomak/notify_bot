# text.py
phrases = {
    "ru": {
        "start_title": "Привет!",
        "start_desc": "Я бот умных напоминаний. Создай напоминание, посмотри список и управляй ими через кнопки ниже.",
        "help_header": "Доступные команды:",
        "help_items": {
            "start": "краткое приветствие и регистрация в БД",
            "help": "показать список команд",
            "profile": "показать твои данные в боте",
        },

        # Profile
        "profile_not_found": "Пользователь не найден в БД.",
        "profile_title": "Твой профиль",
        "profile_line_id": "ID: <code>{user_id}</code>",
        "profile_line_user": "Username: @{username}",
        "profile_line_lang": "Язык: {lang}",
        "profile_line_created": "Создан: {created}",
        "profile_line_last_seen": "Последний визит: {last_seen}",
        "profile_line_balance": "Баланс (XTR): {balance}",

        # Keyboards
        "kb_create": "➕ Создать напоминание",
        "kb_list": "📋 Мои напоминания",
        "kb_profile": "👤 Профиль",
        "kb_back": "⬅️ Назад",
        "kb_delete": "🗑️ Удалить",
        "kb_cancel": "✖️ Отмена",
        "kb_skip": "Пропустить",

        # Content type labels
        "ctype_text": "Текст",
        "ctype_photo": "Фото",
        "ctype_video": "Видео",
        "ctype_voice": "Голосовое",
        "ctype_audio": "Аудио",
        "ctype_document": "Документ",
        "ctype_video_note": "Кружок",

        # Create flow
        "create_title": "Введите название напоминания (или нажмите «Пропустить»).",
        "create_choose_type": "Что отправлять при напоминании? Выберите тип содержимого.",
        "create_send_text": "Отправьте текст сообщения для напоминания.",
        "create_send_photo": "Отправьте фото (можно с подписью).",
        "create_send_video": "Отправьте видео (можно с подписью).",
        "create_send_voice": "Отправьте голосовое сообщение.",
        "create_send_audio": "Отправьте аудиофайл.",
        "create_send_document": "Отправьте документ.",
        "create_send_video_note": "Отправьте кружок (video note).",

        "create_enter_dt": "Укажите дату и время первого срабатывания в формате <code>YYYY-MM-DD HH:MM</code>.",
        "create_enter_tz": "Укажите часовой пояс (IANA, например <code>Europe/Moscow</code>). Можно нажать «Пропустить» — тогда будет использован {tz}.",
        "create_choose_repeat": "Периодичность:",
        "repeat_once": "Однократно",
        "repeat_daily": "Каждый день",
        "repeat_weekly": "Каждую неделю",
        "repeat_monthly": "Каждый месяц",
        "repeat_cron": "Свой CRON",
        "create_enter_cron": "Введите crontab из 5 полей: <code>m h dom mon dow</code> (пример: <code>30 9 * * 1-5</code>).",

        "create_ok": "Готово! Напоминание создано.\n{summary}",
        "create_cancelled": "Создание напоминания отменено.",

        # Errors
        "errors_invalid_dt": "Неверный формат даты/времени. Пример: <code>2025-08-16 09:30</code>.",
        "errors_past_dt": "Время уже прошло. Укажите момент в будущем.",
        "errors_invalid_tz": "Неверный часовой пояс. Пример: <code>Europe/Moscow</code>.",
        "errors_invalid_cron": "Неверное crontab-выражение.",

        # List / detail
        "alerts_header": "Твои активные напоминания:",
        "alerts_empty": "У тебя нет активных напоминаний.",
        "alert_item": "⏰ {title} • следующее: {next}",
        "alert_info": "<b>{title}</b>\nТип: {content_type}\nПериодичность: {periodicity}\nЧасовой пояс: {tz}\nСледующее срабатывание: {next}\nСоздано: {created}\nID: <code>{id}</code>",
        "deleted": "Напоминание удалено.",
    },

    "en": {
        "start_title": "Hi!",
        "start_desc": "I'm a smart reminder bot. Create reminders, list and manage them via the keyboard below.",
        "help_header": "Available commands:",
        "help_items": {
            "start": "short greeting and DB registration",
            "help": "show the command list",
            "profile": "show your profile data",
        },

        # Profile
        "profile_not_found": "User not found in DB.",
        "profile_title": "Your profile",
        "profile_line_id": "ID: <code>{user_id}</code>",
        "profile_line_user": "Username: @{username}",
        "profile_line_lang": "Language: {lang}",
        "profile_line_created": "Created: {created}",
        "profile_line_last_seen": "Last seen: {last_seen}",
        "profile_line_balance": "Balance (XTR): {balance}",

        # Keyboards
        "kb_create": "➕ Create alert",
        "kb_list": "📋 My alerts",
        "kb_profile": "👤 Profile",
        "kb_back": "⬅️ Back",
        "kb_delete": "🗑️ Delete",
        "kb_cancel": "✖️ Cancel",
        "kb_skip": "Skip",

        # Content type labels
        "ctype_text": "Text",
        "ctype_photo": "Photo",
        "ctype_video": "Video",
        "ctype_voice": "Voice",
        "ctype_audio": "Audio",
        "ctype_document": "Document",
        "ctype_video_note": "Video note",

        # Create flow
        "create_title": "Send an optional alert title (or tap “Skip”).",
        "create_choose_type": "What should I send for this alert? Choose the content type.",
        "create_send_text": "Send the text that I should remind you with.",
        "create_send_photo": "Send a photo (caption optional).",
        "create_send_video": "Send a video (caption optional).",
        "create_send_voice": "Send a voice message.",
        "create_send_audio": "Send an audio file.",
        "create_send_document": "Send a document.",
        "create_send_video_note": "Send a video note.",

        "create_enter_dt": "Provide the first run datetime in format <code>YYYY-MM-DD HH:MM</code>.",
        "create_enter_tz": "Provide a timezone (IANA, e.g. <code>Europe/London</code>). Or tap “Skip” — default {tz}.",
        "create_choose_repeat": "Repeat:",
        "repeat_once": "Once",
        "repeat_daily": "Daily",
        "repeat_weekly": "Weekly",
        "repeat_monthly": "Monthly",
        "repeat_cron": "Custom CRON",
        "create_enter_cron": "Enter a 5-field crontab: <code>m h dom mon dow</code> (e.g. <code>30 9 * * 1-5</code>).",

        "create_ok": "Done! The alert is created.\n{summary}",
        "create_cancelled": "Creation cancelled.",

        # Errors
        "errors_invalid_dt": "Invalid datetime format. Example: <code>2025-08-16 09:30</code>.",
        "errors_past_dt": "That time is in the past. Please provide a future moment.",
        "errors_invalid_tz": "Invalid timezone. Example: <code>Europe/London</code>.",
        "errors_invalid_cron": "Invalid crontab expression.",

        # List / detail
        "alerts_header": "Your active alerts:",
        "alerts_empty": "You have no active alerts.",
        "alert_item": "⏰ {title} • next: {next}",
        "alert_info": "<b>{title}</b>\nType: {content_type}\nRepeat: {periodicity}\nTimezone: {tz}\nNext run: {next}\nCreated: {created}\nID: <code>{id}</code>",
        "deleted": "Alert deleted.",
    },
}
