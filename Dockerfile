FROM python:3.8 as base

ENV PYTHONDONTWRITEBYTECODE=1

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt favicon.ico .env app.py messages.py telegramcalendar.py config.py utils.py ./
RUN python -m pip install --upgrade pip
RUN python -m pip install -r requirements.txt

ENTRYPOINT ["python", "app.py"]

