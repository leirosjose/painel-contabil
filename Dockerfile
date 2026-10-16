FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

WORKDIR /app

COPY requirements-lock.txt .
# O Chromium do Playwright serve aos dois motores do PDF: o proprio
# Playwright (HTML -> PDF) e o Kaleido (graficos -> PNG).
RUN pip install -r requirements-lock.txt \
 && playwright install --with-deps chromium

COPY . .

EXPOSE 8503
CMD ["streamlit", "run", "app.py", "--server.port=8503", "--server.address=0.0.0.0", "--server.headless=true"]
