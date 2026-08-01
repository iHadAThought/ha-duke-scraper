FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app
COPY worker.py /app/worker.py

# Image ships browsers; install the Python package + tzdata explicitly.
RUN pip install --no-cache-dir "playwright==1.49.1" "tzdata" \
 && ln -snf /usr/share/zoneinfo/America/New_York /etc/localtime || true

ENV DUKE_SCRAPER_DATA=/data
ENV DUKE_SCRAPER_HOST=0.0.0.0
ENV DUKE_SCRAPER_PORT=8765
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV TZ=America/New_York

EXPOSE 8765
CMD ["python", "/app/worker.py"]
