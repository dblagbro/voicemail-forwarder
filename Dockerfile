FROM python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
WORKDIR /app
RUN apk upgrade --no-cache && apk add --no-cache tzdata
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && pip install --no-cache-dir -r /app/requirements.lock \
    && pip check \
    && python -m pip uninstall -y pip
COPY app /app/app
ENV PYTHONUNBUFFERED=1
EXPOSE 8089
CMD ["gunicorn", "-b", "0.0.0.0:8089", "-w", "1", "--threads", "8", "app.app:app"]
