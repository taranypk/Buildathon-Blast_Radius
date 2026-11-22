# The scorer labels commits in this exact image, so keep its Python and SQLite.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# sqlglot at the pinned commit eb11aec (git archive of the needed paths)
COPY vendor/sqlglot-eb11aec.tar.gz sqlglot_version.py /app/
RUN mkdir -p /app/sqlglot \
 && tar xzf /app/sqlglot-eb11aec.tar.gz -C /app/sqlglot \
 && rm /app/sqlglot-eb11aec.tar.gz \
 && cp /app/sqlglot_version.py /app/sqlglot/sqlglot/_version.py \
 && cd /app/sqlglot \
 && python -m compileall -q sqlglot tests \
 && chmod -R a+rX /app
COPY app/ /app/
RUN python -m compileall -q /app/*.py && chmod -R a+rX /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/main.py"]
