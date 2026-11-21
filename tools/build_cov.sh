#!/bin/sh
# run inside br-arm: /mnt = sqlglot checkout (ro), /out = output dir, /tools = this dir
set -e
cp -r /mnt/. /src/ && cd /src
export PYTHONPATH=/src:/tools SKIP_INTEGRATION=0 COVERAGE_CORE=ctrace
FILES=$(cd /src && find tests -name 'test_*.py' | sort)
i=0
for f in $FILES; do echo "$f"; done > /tmp/files
# 4 shards balanced by round robin
for s in 0 1 2 3; do
  awk -v s=$s 'NR%4==s' /tmp/files > /tmp/shard$s
  ( MAP_OUT=/out/opens$s.json python -m coverage run --data-file=/out/cov$s --source=sqlglot,tests \
      -m pytest -p no:cacheprovider -p mapplugin -q $(cat /tmp/shard$s) > /out/log$s.txt 2>&1 || true ) &
done
wait
tail -2 /out/log*.txt
