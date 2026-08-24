#!/usr/bin/env python3
"""spec.py — spec.json 을 읽어 API 스펙 의존처를 전부 동기화한다.

대회 중 앱/경로/메서드/필드가 바뀌거나 앱이 추가되면:
  1) spec.json 만 고친다 (extra 앱이면 extra.enabled=true 로)
  2) python3 spec.py            # 검사만: 무엇이 바뀌는지 보고 (파일 수정 안 함)
     python3 spec.py --apply    # 실제 반영
  3) cd ../../terraform && terraform apply   # waf.tf 반영분 배포
  4) extra 앱이면 ./extra-app.sh deploy      # 클러스터에 배포

반영 대상:
  a) tools/spec.py            — waf 생성기의 데이터 (전체 재생성)
  b) waf.tf                   — tools/apply_spec.py --apply 로 위임 (검증된 생성기 재사용)
  c) tools/tuner/score.py     — APPS / SLA_S (튜너가 감시·채점하는 앱 목록)
  d) 경고 출력                — /v1/<앱이름> 규약을 하드코딩한 스크립트 목록
"""
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))          # tools/fix
TOOLS = os.path.dirname(HERE)                              # tools
TUNER = os.path.join(TOOLS, "tuner")
APPLY = "--apply" in sys.argv

# 쿼리값 형식 검증용 — spec.json 은 파라미터 '이름'만 적는다. 형식은 여기서 정한다.
# 모르는 파라미터는 범용(id) 형식으로 검증한다 (영숫자·._:- 만).
REGEX = {
    "email": r"^[a-zA-Z0-9._%+=-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    "id":    r"^[a-zA-Z0-9_.:-]+$",
}

with open(os.path.join(HERE, "spec.json"), encoding="utf-8") as f:
    SPEC = json.load(f)

apps = dict(SPEC["apps"])
extra = SPEC.get("extra") or {}
if extra.get("enabled"):
    apps[extra["name"]] = {k: extra.get(k) for k in ("path", "sla_s", "methods", "query", "body")}
    print(f"※ extra 앱 '{extra['name']}' 포함 (enabled=true)")

prefixes = list(SPEC["prefixes"])

# spec.json(경로/메서드/필드) → waf 생성기(tools/spec.py) 형식으로 변환
endpoints = {}
for name, a in apps.items():
    methods = [m.upper() for m in a.get("methods") or []]
    e = {"path": a["path"]}
    if "GET" in methods:
        q = a.get("query") or []
        if q:
            e["get_query"] = {p: ("email" if p == "email" else "id") for p in q}
        elif a["path"] not in prefixes:
            # 쿼리 없는 GET 은 생성기가 값검증 규칙을 못 만든다 → 경로 프리픽스로 허용
            prefixes.append(a["path"])
    if "POST" in methods:
        e["post_body"] = a.get("body") or []
    if "PUT" in methods:
        e["put"] = True
    endpoints[name] = e


# ── a) tools/spec.py 재생성 ──────────────────────────────────────────────
def py(v):
    return json.dumps(v, ensure_ascii=False)


def gen_tools_spec():
    ep = []
    for name, e in endpoints.items():
        lines = [f'    "{name}": {{', f'        "path": {py(e["path"])},']
        if e.get("get_query"):
            lines.append(f'        "get_query": {py(e["get_query"])},')
        if "post_body" in e:
            lines.append(f'        "post_body": {py(e["post_body"])},')
        if e.get("put"):
            lines.append('        "put": True,')
        lines.append("    },")
        ep.append("\n".join(lines))
    rx = "\n".join(f"    {py(k)}: {py(v)}," for k, v in REGEX.items())
    return f'''"""spec.py — 대회 API 스펙 (tools/fix/spec.json 에서 자동 생성. 직접 고치지 말 것).

바꾸려면 tools/fix/spec.json 을 고치고 `python3 tools/fix/spec.py --apply` 를 실행해라.
"""

REGEX = {{
{rx}
}}

ENDPOINTS = {{
{chr(10).join(ep)}
}}

PREFIXES = {py(prefixes)}

APPS = {py(list(apps))}


def get_paths():
    return [e["path"] for e in ENDPOINTS.values() if "get_query" in e]

def post_paths():
    return [e["path"] for e in ENDPOINTS.values() if "post_body" in e]

def put_paths():
    return [e["path"] for e in ENDPOINTS.values() if e.get("put")]
'''


# ── c) score.py 의 APPS / SLA_S 패치 ────────────────────────────────────
def patch_score(text):
    sla = ", ".join(f'"{n}": {a["sla_s"]:.3f}' for n, a in apps.items())
    ap = ", ".join(f'"{n}"' for n in apps)
    tup = f'({ap},)' if len(apps) == 1 else f'({ap})'
    text, n1 = re.subn(r'^SLA_S = \{[^}]*\}', f'SLA_S = {{{sla}}}', text, count=1, flags=re.M)
    text, n2 = re.subn(r'^APPS = \([^)]*\)', f'APPS = {tup}', text, count=1, flags=re.M)
    if not (n1 and n2):
        raise SystemExit("✗ score.py 에서 SLA_S/APPS 라인을 못 찾았다 — 수동 확인 필요")
    return text


def write_if_changed(path, new):
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if old == new:
        print(f"  = 변경 없음: {os.path.relpath(path, TOOLS)}")
        return False
    if APPLY:
        open(path, "w", encoding="utf-8", newline="\n").write(new)
        print(f"  ✓ 갱신: {os.path.relpath(path, TOOLS)}")
    else:
        print(f"  ! 갱신 필요: {os.path.relpath(path, TOOLS)} (--apply 로 반영)")
    return True


print("== a) tools/spec.py (waf 생성기 데이터)")
write_if_changed(os.path.join(TOOLS, "spec.py"), gen_tools_spec())

print("== c) tools/tuner/score.py (APPS/SLA_S)")
score_path = os.path.join(TUNER, "score.py")
write_if_changed(score_path, patch_score(open(score_path, encoding="utf-8").read()))

print("== b) waf.tf 동기화 (apply_spec.py 위임)")
r = subprocess.run([sys.executable, os.path.join(TOOLS, "apply_spec.py")]
                   + (["--apply"] if APPLY else []), cwd=TOOLS)
if r.returncode != 0:
    sys.exit(r.returncode)

# ── d) 규약 이탈 경고 ────────────────────────────────────────────────────
print("== d) 하드코딩 규약 검사")
odd = [n for n, a in apps.items() if a["path"] != f"/v1/{n}"]
if odd:
    print(f"  ⚠ 경로가 /v1/<앱이름> 규약을 벗어남: {odd}")
    print("    다음 파일은 /v1/$APP 을 하드코딩한다 — 직접 확인·수정해라:")
    print("    tuner/probe.sh (ALB 규칙에서 자동 발견, 실패 시 /v1/<앱> 폴백)")
    print("    tuner/concurrency.sh, tuner/profile.sh, tuner/pretune.sh (시드/부하 URL)")
else:
    print("  [O] 전 앱이 /v1/<앱이름> 규약 — 튜너 스크립트 수정 불필요")
if extra.get("enabled"):
    print(f"== 다음: terraform apply (waf) → ./extra-app.sh deploy ('{extra['name']}' 배포)")
elif APPLY:
    print("== 다음: cd ../../terraform && terraform apply")
