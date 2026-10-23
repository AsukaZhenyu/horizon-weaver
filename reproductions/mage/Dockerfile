FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /tmp
COPY requirements-math.lock.txt /tmp/
COPY wheels /wheels
RUN pip install --no-index --no-cache-dir --find-links=/wheels -r requirements-math.lock.txt && rm -rf /wheels /tmp/requirements-math.lock.txt
ENV PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
ENTRYPOINT ["python", "-I"]
