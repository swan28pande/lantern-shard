# Same base image the commits were labeled in (its Python and SQLite decide test behaviour).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV SQLGLOT_REF=eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    SQLGLOT_DIR=/opt/sqlglot \
    SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# sqlglot at the pinned commit (source tarball from GitHub)
RUN python -c "import urllib.request,tarfile,io,os; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/' + os.environ['SQLGLOT_REF'] + '.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-$SQLGLOT_REF /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests > /dev/null \
 && chmod -R a+rX /opt/sqlglot

COPY app /app
COPY maps /app/maps
RUN python /app/build_maps.py /app/maps/maps.json.gz /app/maps.pkl \
 && cp /app/brhook.py "$(python -c 'import site; print(site.getsitepackages()[0])')/brhook.py" \
 && echo "import brhook" > "$(python -c 'import site; print(site.getsitepackages()[0])')/brhook.pth" \
 && python -m compileall -q /app "$(python -c 'import site; print(site.getsitepackages()[0])')/brhook.py" > /dev/null \
 && chmod -R a+rX /app

ENV PYTHONPATH=/opt/sqlglot
WORKDIR /opt/sqlglot
CMD ["python", "/app/selector.py"]
