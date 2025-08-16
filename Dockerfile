# Dockerfile
FROM python:3.11-slim

# Быстрые и предсказуемые сборки Python
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Инструменты сборки для пакетов без manylinux-колёс (greenlet и др.)
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential gcc g++ python3-dev libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) Сначала зависимости — чтобы кэшировались слои
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir --prefer-binary -r requirements.txt

# 2) Затем код
COPY . .

# Каталоги под БД и логи (маунтим из compose)
RUN mkdir -p /app/data /app/logs

# Запуск
CMD ["python", "main.py"]
