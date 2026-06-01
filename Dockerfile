FROM python:3.11-slim

# ffmpeg для обработки аудио
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Порт для Stripe webhook (Railway роутит HTTP через PORT env variable)
EXPOSE 8080

CMD ["python", "bot.py"]
