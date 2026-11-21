#!/bin/sh
set -e
cp -r /mnt/. /src/ && cd /src
export PYTHONPATH=/src:/tools SKIP_INTEGRATION=0
find tests -name 'test_*.py' | sort > /tmp/files
for s in 0 1 2 3; do
  awk -v s=$s 'NR%4==s' /tmp/files > /tmp/shard$s
  ( MAP_OUT=/out/classes$s.json python -m pytest -p no:cacheprovider -p classplugin -q --durations=0 $(cat /tmp/shard$s) > /out/clog$s.txt 2>&1 || true ) &
done
wait
tail -2 /out/clog*.txt
