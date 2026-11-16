# syntax=docker/dockerfile:1
FROM node:24-alpine AS ui
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DT_DATA_DIR=/data DT_UI_DIR=/app/ui
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates aria2 megatools libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 drivetheater && useradd --uid 10001 --gid 10001 --no-create-home drivetheater
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/drivetheater ./drivetheater
COPY --from=ui /build/dist ./ui
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "drivetheater.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
