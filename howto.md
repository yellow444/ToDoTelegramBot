# Шпаргалка

Полная документация — в [README.md](README.md), коворкеры — в [docs/coworker.md](docs/coworker.md).

```bash
# локально (Docker + llm-gateway)
docker compose -f docker-compose.yml -f docker-compose.llm.yml up -d --build
docker logs -f todobot

# тесты
.venv/Scripts/python -m pytest

# вход userbot для расшифровки Telegram Premium (телефон и код вводите сами)
docker exec -it todobot python -m todobot.userbot login

# чат-коворкер Claude: ждать задание из бота / сдать отчёт
.venv/Scripts/python -m todobot.worker --wait claude
.venv/Scripts/python -m todobot.worker --report <job_id> --file report.md

# образ и Kubernetes
docker build --no-cache -t yellow444/telegramcalendarserver:latest .
kubectl create namespace telegramcalendar
kubectl apply -f k8s/
```
