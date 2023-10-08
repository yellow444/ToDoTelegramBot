
FROM node:20.7.0 AS node_base
WORKDIR  /calendar
COPY calendar/ ./
COPY favicon.ico .
RUN npm install
RUN npx webpack


FROM python:3.8 as base

# EXPOSE 80 443
RUN apt-get update -y && apt-get install nodejs -y 

ENV PYTHONDONTWRITEBYTECODE=1

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY favicon.ico requirements.txt ./
RUN python -m pip install --upgrade pip
RUN python -m pip install -r requirements.txt

# COPY app.py ./.env  ./
# COPY calendar/static ./calendar/static
# COPY calendar/dist ./calendar/dist
COPY --from=node_base ./calendar/dist ./calendar/dist
COPY --from=node_base ./calendar/static ./calendar/static
COPY --from=node_base ./calendar/ ./calendar/
ENTRYPOINT ["python", "app.py"]

