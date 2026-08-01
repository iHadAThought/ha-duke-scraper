# Home Assistant Supervisor builds this folder as the Docker context.
# Base image includes Chromium for Playwright (amd64 / aarch64).
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY worker.py /app/worker.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh \
 && pip install --no-cache-dir "playwright==1.49.1" "tzdata" \
 && ln -snf /usr/share/zoneinfo/America/New_York /etc/localtime || true

ENV DUKE_SCRAPER_DATA=/config/.duke_scraper
ENV DUKE_SCRAPER_HOST=0.0.0.0
ENV DUKE_SCRAPER_PORT=8765
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV TZ=America/New_York

LABEL \
  io.hass.name="Duke Energy Scraper Worker" \
  io.hass.description="Playwright worker for Duke Energy Scraper" \
  io.hass.type="addon" \
  io.hass.version="1.4.0" \
  io.hass.arch="aarch64|amd64"

EXPOSE 8765
CMD ["/run.sh"]
