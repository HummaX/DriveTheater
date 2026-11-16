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
# The private installer uses umask 077; COPY retains restrictive source modes.
# Normalize immutable image assets only, never the separately mounted /data.
RUN chmod 0755 /app && chmod -R a=rX /app/drivetheater /app/ui
USER 10001:10001
# Fail the build instead of deploying an image that its non-root user cannot read.
RUN python - <<'PY'
from pathlib import Path
import os
import drivetheater

if os.geteuid() == 0:
    raise RuntimeError('The image readability check must run as a non-root user')

def walk_error(error):
    raise error

for root in (Path('drivetheater'), Path('ui')):
    if not root.is_dir():
        raise RuntimeError(f'Missing application directory: {root}')
    for directory, _, filenames in os.walk(root, onerror=walk_error):
        if os.access(directory, os.W_OK):
            raise RuntimeError(f'Application directory is writable: {directory}')
        for filename in filenames:
            path = Path(directory) / filename
            with path.open('rb') as asset:
                asset.read(1)
            if os.access(path, os.W_OK):
                raise RuntimeError(f'Application file is writable: {path}')
Path('ui/index.html').read_bytes()
print('Application code and UI assets are readable and read-only for the non-root image user.')
PY
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "drivetheater.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
