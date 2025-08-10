API_TOKEN = "7864432050:AAHcCUkyD6a29s0pFgZDj9b-FIiFC6iNO_0"

DEEPSEEK_KEY = "sk-93a1b4e6156044849ecb36fe377c10ec"

CSV_FILE = "notifications.csv"

MODEL_NAME = "deepseek-chat"

API_URL = "https://api.deepseek.com"

CSV_FILE = "notifications.csv"

TIME_PARSE_LOG_FILE = "time_parse_log.csv"

CSV_FIELDS = [
  "user_id","created_at","send_at","alert_id",
  "title","kind","times","days_of_week",
  "window_start","window_end","interval_minutes","cron_expr",
  "src_chat_id","src_message_id","content_type"
]

INTERVAL_JSON_SPEC = {
    "kind": "one_time|daily|weekly|window_interval|cron",
    "times": "list of 'HH:MM' strings in 24h (for daily/weekly)",
    "days_of_week": "list of ['mon','tue','wed','thu','fri','sat','sun'] (weekly only)",
    "window": {"start": "HH:MM", "end": "HH:MM"},
    "interval_minutes": "integer minutes for window_interval",
    "cron_expr": "string crontab expression (optional)",
    "timezone": "IANA tz, optional (ignore if absent)",
    "name": "optional short title (<=100 chars)"
}
