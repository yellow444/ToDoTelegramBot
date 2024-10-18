FROM python:3.8 as base

EXPOSE 80 443
RUN apt-get update -y && apt-get install nodejs -y 

ENV PYTHONDONTWRITEBYTECODE=1

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY favicon.ico requirements.txt ./
RUN python -m pip install --upgrade pip
RUN python -m pip install -r requirements.txt

ENTRYPOINT ["python", "app.py"]

