# Self-hosting on a server/NAS. Data lives in /srv/data (mount a volume there).
# The code is baked in; mounting a newer copy of the repo at /srv overrides it.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /srv
COPY app /srv/app
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--proxy-headers"]
