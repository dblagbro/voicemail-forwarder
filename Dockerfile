FROM python:3.12-alpine
WORKDIR /app
RUN apk add --no-cache tzdata gcc musl-dev linux-headers
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app/app
ENV PYTHONUNBUFFERED=1
EXPOSE 8089
CMD ["gunicorn", "-b", "0.0.0.0:8089", "-w", "1", "--threads", "8", "app.app:app"]
