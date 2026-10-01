FROM python:3.13-alpine

RUN pip install --no-cache-dir sympy==1.14.0

RUN mkdir -p /opt/mathagent /work
COPY runner.py /opt/mathagent/runner.py
RUN chmod 0555 /opt/mathagent/runner.py

ENTRYPOINT []
