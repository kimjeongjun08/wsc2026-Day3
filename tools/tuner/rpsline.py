#!/usr/bin/env python3
# rpsline.py — 스냅샷을 한 줄로 요약한다.
#   쉘 안에 파이썬을 문자열로 박으면 따옴표가 엉켜 조용히 깨진다(실측: 출력이 코드가 됐다).
#   짧아도 파일로 뺀다.
import json, os
d = json.loads(os.environ.get("SNAP") or "{}")
print(" ".join("%s=%srps" % (k, v.get("rps", 0)) for k, v in sorted(d.items())))
