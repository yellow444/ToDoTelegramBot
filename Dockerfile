FROM python:3.8
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt favicon.ico .env app.py messages.py telegramcalendar.py config.py handlers.py db.py utils.py scheduler.py ./
RUN python -m pip install --no-cache-dir --upgrade pip 
RUN python -m pip install --no-cache-dir -r requirements.txt

ENTRYPOINT ["python", "app.py"]

