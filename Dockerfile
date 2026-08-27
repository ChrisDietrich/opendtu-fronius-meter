FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opendtu_fronius_meter.py entrypoint.py .

# Both possible virtual-meter ports: the GEN24's additional-meter UI only
# offers a fixed dropdown of 502 or 1502, not an arbitrary port. Actual
# bound port is controlled by TCP_LISTEN_PORT. Only one is ever used at a
# time -- there's only one combined meter -- but either could be picked.
EXPOSE 1502
EXPOSE 502

# entrypoint.py translates /data/options.json (Home Assistant Supervisor
# App config) into env vars when present, then runs opendtu_fronius_meter.py
# unchanged -- see entrypoint.py's docstring. Run as a plain container
# (docker run/compose) instead and /data/options.json simply won't exist,
# so this behaves exactly like running opendtu_fronius_meter.py directly.
CMD ["python3", "entrypoint.py"]
