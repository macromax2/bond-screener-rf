# Скринер облигаций РФ — образ для облачного деплоя.
# Собирается хостингом автоматически при пуше в git-репозиторий.
FROM python:3.12-slim

WORKDIR /app

# зависимости отдельным слоем: пока не менялся requirements, сборка кэшируется
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# только код: данные собираются в рантайме (data/ создаётся сама)
COPY app ./app
COPY static ./static

ENV PYTHONUNBUFFERED=1
# $PORT подставляет хостинг (Render и др.), по умолчанию 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
