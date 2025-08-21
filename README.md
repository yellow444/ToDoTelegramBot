# ToDoTelegramBot

Телеграм-бот для управления задачами и напоминаниями. Пользователь отправляет сообщение, выбирает дату и время в встроенном календаре, а бот сохранит напоминание и пришлёт его в нужный момент.

## Возможности
- добавление задач и напоминаний через встроенный календарь;
- хранение задач в MongoDB и автоматическая отправка напоминаний в заданное время;
- управление задачами через inline-кнопки (выполнить, напомнить, удалить).

## Требования
- Python 3.8+
- MongoDB 4+
- Docker (для контейнерного запуска)
- docker-compose (по желанию)
- Kubernetes и Helm (по желанию)

## Запуск
### Локально
```bash
pip install -r requirements.txt
python app.py
```

### Docker
```bash
docker build -t todotelegrambot .
docker run --env-file .env todotelegrambot
```

### docker-compose
```bash
docker-compose up -d
```

### Kubernetes / Helm
В каталоге `k8s` находятся манифесты для запуска в Kubernetes.

#### Секреты
```bash
kubectl create secret generic telegramcalendar-secret \
  --namespace telegramcalendar \
  --from-literal=TOKEN=<bot-token> \
  --from-literal=MONGO_USER=<mongo-user> \
  --from-literal=MONGO_PASS=<mongo-pass>
```
Замените примерные значения на реальные и ограничьте доступ к секретам.

После создания секрета можно применить манифесты:
```bash
kubectl apply -f k8s/
```
Для использования Helm подготовьте чарт на основе этих файлов и установите его, например:
```bash
helm upgrade --install todotelegrambot ./k8s/chart
```

## Переменные окружения
| Переменная        | Описание                                      |
|-------------------|-----------------------------------------------|
| `TOKEN`           | токен Telegram-бота от @BotFather              |
| `MYHOSTNAME`      | внешний URL, на который указывает вебхук      |
| `SSL_CERT`        | путь к SSL-сертификату                        |
| `SSL_KEY`         | путь к приватному ключу                       |
| `PORT`            | порт веб‑сервера                              |
| `MONGO_HOST`      | адрес сервера MongoDB                         |
| `MONGO_PORT`      | порт MongoDB                                  |
| `MONGO_USER`      | имя пользователя MongoDB                      |
| `MONGO_PASS`      | пароль пользователя MongoDB                   |
| `DB_NAME`         | имя базы данных                               |
| `COLLECTION_NAME` | имя коллекции для хранения задач              |

## Настройка MongoDB
1. Запустить сервер MongoDB (локально или в контейнере). Пример через Docker:
   ```bash
   docker run -d \
     -p 27017:27017 \
     -e MONGO_INITDB_ROOT_USERNAME=root \
     -e MONGO_INITDB_ROOT_PASSWORD=password123 \
     --name mongodb mongo
   ```
2. Создать базу данных и коллекцию согласно переменным окружения `DB_NAME` и `COLLECTION_NAME`.
3. Убедиться, что переменные окружения `MONGO_USER` и `MONGO_PASS` совпадают с учётными данными сервера.

После запуска бот готов принимать задачи и присылать напоминания.
