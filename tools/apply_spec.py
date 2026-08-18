"""apply_spec.py — 스펙 변경 대응툴 (spec.py 기준으로 waf.tf 동기화).

대회 중 경로/쿼리/바디가 바뀌면 spec.py만 고치고 실행:
  python apply_spec.py            # --check: waf.tf가 spec과 맞는지 검사·보고 (수정 안 함)
  python apply_spec.py --apply    # 경로 locals 자동 갱신 + AllowValid HCL 생성 + 검사 + fmt
  python apply_spec.py --tf DIR   # terraform 경로 (기본 ../terraform)

동작(안전 설계):
  1) 경로 locals (waf_get_exact/post/put/prefix) → SPEC:PATHS 마커 구간 **자동 갱신** (단순·안전).
  2) AllowValidGET/POST/PUT 규칙 → spec 기준 HCL을 `tools/generated_waf_rules.tf.txt`로 **생성**(검토용).
     + 현재 waf.tf에 각 엔드포인트의 경로·쿼리arg·바디필드가 있는지 **정합성 검사**(누락=FP/보안구멍 경고).
  ※ 중첩 보안규칙(BlockAttacks 등)과 AllowValid 본문은 자동으로 안 건드림 → 검토 후 사람이 반영.
"""
import argparse
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 콘솔에서 ✓/⚠ 출력용
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spec as S  # noqa: E402


# ── HCL 생성기 (들여쓰기는 terraform fmt가 정리) ──
def _bm(search, constraint, field, decode=False):
    tt = '"URL_DECODE"' if decode else '"NONE"'
    return (f'statement {{\nbyte_match_statement {{\nsearch_string         = "{search}"\n'
            f'positional_constraint = "{constraint}"\nfield_to_match {{\n{field}\n}}\n'
            f'text_transformation {{\npriority = 0\ntype     = {tt}\n}}\n}}\n}}')


def _regex_query(regex_key, argname):
    return (f'statement {{\nregex_match_statement {{\nregex_string = local.{regex_key}_regex\n'
            f'field_to_match {{\nsingle_query_argument {{\nname = "{argname}"\n}}\n}}\n'
            f'text_transformation {{\npriority = 0\ntype     = "URL_DECODE"\n}}\n}}\n}}')


def _and(*stmts):
    return "statement {\nand_statement {\n" + "\n".join(stmts) + "\n}\n}"


def gen_get():
    b = []
    for e in S.ENDPOINTS.values():
        if "get_query" not in e:
            continue
        parts = [_bm("GET", "EXACTLY", "method {}"), _bm(e["path"], "EXACTLY", "uri_path {}")]
        parts += [_regex_query(rk, a) for a, rk in e["get_query"].items()]
        b.append(_and(*parts))
    for pre in S.PREFIXES:
        b.append(_and(_bm("GET", "EXACTLY", "method {}"), _bm(pre, "STARTS_WITH", "uri_path {}")))
    return "\n".join(b)


def gen_post():
    b = []
    for e in S.ENDPOINTS.values():
        if "post_body" not in e:
            continue
        parts = [_bm("POST", "EXACTLY", "method {}"), _bm(e["path"], "EXACTLY", "uri_path {}")]
        parts += [_bm(f, "CONTAINS", 'body {\noversize_handling = "CONTINUE"\n}') for f in e["post_body"]]
        b.append(_and(*parts))
    return "\n".join(b)


def gen_put():
    ands = [_and(_bm("PUT", "EXACTLY", "method {}"), _bm(p, "EXACTLY", "uri_path {}")) for p in S.put_paths()]
    return ands[0] if len(ands) == 1 else "or_statement {\n" + "\n".join(ands) + "\n}"


def gen_paths():
    def lst(xs):
        return "[" + ", ".join(f'"{x}"' for x in xs) + "]"
    return (f'waf_get_exact  = {lst(S.get_paths())}\n'
            f'waf_post_exact = {lst(S.post_paths())}\n'
            f'waf_put_exact  = {lst(S.put_paths())}\n'
            f'waf_prefix     = {lst(S.PREFIXES)}')


def replace_region(text, tag, body):
    b, e = f"# SPEC:{tag}:BEGIN", f"# SPEC:{tag}:END"
    pat = re.compile(re.escape(b) + r".*?" + re.escape(e), re.DOTALL)
    if not pat.search(text):
        raise SystemExit(f"✗ waf.tf에 마커 없음: {b} ... {e}")
    return pat.sub(b + "\n" + body + "\n" + e, text)


def _current_paths(waf, key):
    m = re.search(rf'{key}\s*=\s*\[([^\]]*)\]', waf)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else None


def _paths_match(waf):
    return (_current_paths(waf, "waf_get_exact") == S.get_paths()
            and _current_paths(waf, "waf_post_exact") == S.post_paths()
            and _current_paths(waf, "waf_put_exact") == S.put_paths()
            and _current_paths(waf, "waf_prefix") == S.PREFIXES)


def drift_check(waf):
    """spec의 경로·쿼리arg·바디필드가 waf.tf AllowValid에 있는지 검사. 누락 목록 반환."""
    missing = []
    for name, e in S.ENDPOINTS.items():
        if "get_query" in e:
            if f'"{e["path"]}"' not in waf:
                missing.append(f"GET {name}: 경로 '{e['path']}' byte_match 없음")
            for arg in e["get_query"]:
                if f'name = "{arg}"' not in waf:
                    missing.append(f"GET {name}: 쿼리검증 '{arg}' 없음 → 정상요청 차단 위험")
        if "post_body" in e:
            for fld in e["post_body"]:
                if f'search_string         = "{fld}"' not in waf and f'"{fld}"' not in waf:
                    missing.append(f"POST {name}: 바디필드 '{fld}' 검사 없음 → 정상 POST 차단 위험")
        if e.get("put") and f'"{e["path"]}"' not in waf:
            missing.append(f"PUT {name}: 경로 '{e['path']}' 없음")
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="경로 locals 자동 갱신 + 생성 + fmt (기본은 검사만)")
    ap.add_argument("--tf", default=os.path.join(os.path.dirname(__file__), "..", "terraform"))
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    waf_path = os.path.join(args.tf, "waf.tf")
    with open(waf_path, encoding="utf-8") as f:
        orig = f.read()

    # regex 키 존재 검사
    for e in S.ENDPOINTS.values():
        for rk in e.get("get_query", {}).values():
            if f"{rk}_regex" not in orig:
                print(f"⚠ local.{rk}_regex 가 waf.tf에 없음 → spec.REGEX['{rk}'] 대응 local 추가 필요")

    # 1) 경로 locals (의미 비교: 리스트 내용이 다를 때만 갱신 필요. 들여쓰기는 fmt가 처리)
    new = replace_region(orig, "PATHS", gen_paths())
    paths_changed = not _paths_match(orig)

    # 2) AllowValid HCL 생성 (검토파일)
    genfile = os.path.join(here, "generated_waf_rules.tf.txt")
    with open(genfile, "w", encoding="utf-8") as f:
        f.write("# spec.py 기준 생성 — waf.tf AllowValidGET/POST/PUT의 해당 or_statement/statement 본문과 비교/반영용.\n")
        f.write("# (terraform fmt로 들여쓰기 정리됨. 자동 반영 아님 — 검토 후 붙여넣기.)\n\n")
        f.write("## AllowValidGET or_statement 내부:\n" + gen_get() + "\n\n")
        f.write("## AllowValidPOST or_statement 내부:\n" + gen_post() + "\n\n")
        f.write("## AllowValidPUT statement 내부:\n" + gen_put() + "\n")

    # 3) 정합성 검사
    miss = drift_check(orig)

    print("── 스펙 정합성 검사 ──")
    print(f"경로 locals: {'갱신 필요' if paths_changed else '일치 ✓'}")
    if miss:
        print("⚠ AllowValid 규칙 누락(정상 트래픽 차단/보안구멍 위험):")
        for m in miss:
            print("   -", m)
    else:
        print("AllowValid 경로·쿼리·바디: spec과 일치 ✓")
    print(f"생성된 규칙 HCL: {genfile}")

    if not args.apply:
        if paths_changed or miss:
            print("\n→ `--apply`로 경로 locals 자동 갱신. AllowValid 규칙은 생성파일 참고해 반영 후 `terraform validate`.")
            sys.exit(1)
        print("\n✓ 전부 일치. 할 것 없음.")
        return

    if paths_changed:
        with open(waf_path, "w", encoding="utf-8") as f:
            f.write(new)
        print("\n✓ 경로 locals(SPEC:PATHS) 갱신함.")
        try:
            subprocess.run(["terraform", "fmt", "waf.tf"], cwd=args.tf, check=False)
        except FileNotFoundError:
            pass
    if miss:
        print("⚠ AllowValid 규칙은 생성파일 보고 수동 반영 필요 (보안규칙이라 자동수정 안 함).")
    print("→ 반영 후 `terraform validate` 로 확인.")


if __name__ == "__main__":
    main()
