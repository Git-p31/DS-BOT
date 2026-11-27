# DS-BOT в Docker — для любого сервера/хостинга, который умеет запускать Docker
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# при каждом запуске обновляем yt-dlp — YouTube часто всё ломает
CMD ["sh", "-c", "pip install -q -U 'yt-dlp[default]' && python bot.py"]
