FROM python:3.12-slim
WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY manage.py .
CMD ["python", "-m", "app.main"]
