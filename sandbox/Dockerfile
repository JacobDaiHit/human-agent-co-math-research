ARG BASE_IMAGE=python:3.13-alpine
FROM ${BASE_IMAGE}

RUN pip install --no-cache-dir --disable-pip-version-check sympy==1.14.0 mpmath==1.3.0

RUN mkdir -p /opt/mathagent /work
COPY runner.py /opt/mathagent/runner.py
RUN chmod 0555 /opt/mathagent/runner.py

ENTRYPOINT []
