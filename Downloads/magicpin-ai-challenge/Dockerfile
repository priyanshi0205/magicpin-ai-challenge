FROM python:3.13-slim

WORKDIR /app
COPY bot.py /app/bot.py

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "python bot.py --host 0.0.0.0 --port ${PORT:-8080}"]
