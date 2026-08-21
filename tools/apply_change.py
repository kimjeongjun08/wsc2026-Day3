"""apply_change.py — spec.json 한 곳을 고치면 terraform(waf/alb/cloudfront) + 튜너를 그 스펙으로
자동 갱신하는 '변경(기존 앱)' 툴. terraform apply 전에 미리 돌려두고 한 번에 apply 한다.

★안전 설계 (테라폼/툴 오염·파손 없음):
  · 스냅샷(.spec_change_snapshot.json) 대비 '바뀐 리터럴만' **앵커드 치환**(정확한 문자열만).
  · 스펙이 안 바뀌면 **아무 파일도 안 바뀐다**(바이트 동일).
  · 쓰기 전 **.bak 백업** + 쓴 뒤 **terraform validate 게이트**(깨지면 자동 복구).
  · **dry-run 기본** — diff 만 보여주고, --apply 에서만 실제 기록.

무엇이 바뀌나 (기존 앱의 rename):
  경로  → waf.tf(경로 locals + AllowValid byte_match), alb.tf(path_pattern),
          cloudfront.tf(캐시 앱만), 튜너 *.sh(하드코딩 경로)
  쿼리키→ waf.tf(single_query_argument name)   (튜너 QKEY 는 위치 보고)
  바디필드→ waf.tf(AllowValid body byte_match)  (튜너 바디는 위치 보고)
  regex → waf.tf(id_regex/email_regex)

사용:
  python apply_change.py            # dry-run: 무엇이 바뀔지 diff 만 (파일 안 건드림)
  python apply_change.py --apply    # 실제 적용(+백업+validate+스냅샷 갱신)
  python apply_change.py --reset    # 현재 spec.json 을 기준선 스냅샷으로 저장(최초 1회/재기준)
  python apply_change.py --tf DIR   # terraform 경로(기본 ../terraform)

※ '구조 변경'(쿼리 파라미터를 새로 추가 등)은 rename 이 아니라 안전 자동치환 대상이 아니다 →
  apply_spec.py 가 생성하는 generated_waf_rules.tf.txt 를 참고해 반영(드묾).
"""
import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SNAP = os.path.join(HERE, ".spec_change_snapshot.json")


def load_spec():
    """spec.py(=spec.json 로더)를 import 캐시 없이 소스에서 직접 읽는다(stale pyc 방지)."""
    spec_py = os.path.join(HERE, "spec.py")
    ns = {"__name__": "spec_loaded", "__file__": spec_py}   # exec 에 없는 특수변수 공급
    with open(spec_py, encoding="utf-8") as f:
        exec(compile(f.read(), "spec.py", "exec"), ns)
    return ns  # ns["ENDPOINTS"], ns["REGEX"], ns["PREFIXES"], ns["APPS"], ns["RAW"], ns["app_conf"]


def snapshot_of(ns):
    eps = ns["ENDPOINTS"]
    return {
        "paths":     {a: eps[a]["path"] for a in ns["APPS"]},
        "get_query": {a: dict(eps[a].get("get_query", {})) for a in ns["APPS"]},
        "post_body": {a: list(eps[a].get("post_body", [])) for a in ns["APPS"]},
        "put":       {a: bool(eps[a].get("put")) for a in ns["APPS"]},
        "regex":     dict(ns["REGEX"]),
        "prefixes":  list(ns["PREFIXES"]),
    }


def load_snapshot():
    if not os.path.exists(SNAP):
        return None
    with open(SNAP, encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(snap):
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)


# ── 앵커드 치환쌍 만들기 (파일별) ─────────────────────────────────────────────
def build_edits(old, new, tf, ns):
    """반환: {filepath: [(old_str, new_str, 설명), ...]}, warnings, tuner_reports"""
    edits = {}
    warn = []
    reports = []
    waf = os.path.join(tf, "waf.tf")
    alb = os.path.join(tf, "alb.tf")
    cf = os.path.join(tf, "cloudfront.tf")

    def add(path, o, n, why):
        edits.setdefault(path, []).append((o, n, why))

    apps = list(new["paths"])

    # 1) 경로 변경
    tuner_files = sorted(glob.glob(os.path.join(tf, "..", "tools", "tuner", "*.sh")))
    for a in apps:
        op, np_ = old.get("paths", {}).get(a), new["paths"][a]
        if not op or op == np_:
            continue
        # waf.tf: AllowValid path byte_match (정확 앵커)
        add(waf, f'search_string         = "{op}"', f'search_string         = "{np_}"', f"waf AllowValid 경로 {a}")
        # alb.tf: path_pattern glob
        add(alb, f'["{op}*"]', f'["{np_}*"]', f"alb path_pattern {a}")
        # cloudfront.tf: 경로별 behavior 있는 앱만 해당(예: product). '"경로"'(따옴표포함)는 cf 에선
        #   ordered_cache_behavior 의 path_pattern 에만 나오므로 안전. 없는 앱은 no-op.
        add(cf, f'"{op}"', f'"{np_}"', f"cloudfront path {a}")
        # 튜너: 리터럴 경로(pretune 시드 등). '/v1/user'→'/v2/user' 같은 정확 리터럴만.
        for fp in tuner_files:
            add(fp, op, np_, f"tuner 경로 리터럴 {a}")
    # 튜너: '/<prefix>/$APP'(제네릭) 프리픽스는 '전체 앱이 균일하게' 이동할 때만 치환(아니면 경고)
    _tuner_prefix_edits(add, tuner_files, old.get("paths", {}), new["paths"], warn)

    # 2) 쿼리키 변경 (waf single_query_argument name)
    for a in apps:
        oq = old.get("get_query", {}).get(a, {})
        nq = new["get_query"].get(a, {})
        for ok in oq:
            if ok not in nq:  # 이름이 바뀜 (또는 제거) — 새 이름 추정: nq 에 있는 것 중 oq 에 없던 것
                added = [k for k in nq if k not in oq]
                nk = added[0] if len(added) == 1 else None
                if nk:
                    add(waf, f'name = "{ok}"', f'name = "{nk}"', f"waf GET 쿼리키 {a}: {ok}→{nk}")
                    reports.append(f"튜너: {a} GET 쿼리키 {ok}→{nk} — tuner *.sh 의 QKEY/쿼리 확인(자동치환 안 함)")
                else:
                    warn.append(f"{a} GET 쿼리키 '{ok}' 제거/불명확 — waf.tf single_query_argument 수동 확인")

    # 3) 바디필드 변경 (waf AllowValid body byte_match)
    for a in apps:
        ob = set(old.get("post_body", {}).get(a, []))
        nb = set(new["post_body"].get(a, []))
        removed = [x for x in ob - nb if x.strip("_@").isalnum()]
        added = [x for x in nb - ob if x.strip("_@").isalnum()]
        if len(removed) == 1 and len(added) == 1:
            add(waf, f'search_string         = "{removed[0]}"', f'search_string         = "{added[0]}"',
                f"waf POST 바디필드 {a}: {removed[0]}→{added[0]}")
            reports.append(f"튜너: {a} POST 바디필드 {removed[0]}→{added[0]} — tuner *.sh 바디 확인(자동치환 안 함)")
        elif removed or added:
            warn.append(f"{a} 바디필드 변경 복잡({sorted(ob)}→{sorted(nb)}) — waf.tf AllowValid + 튜너 수동 확인")

    # 4) regex 변경
    for k in new["regex"]:
        ov = old.get("regex", {}).get(k)
        nv = new["regex"][k]
        if ov and ov != nv:
            # waf.tf 의 <k>_regex = "..." (HCL 은 \\ 이스케이프)
            add(waf, f'{k}_regex = "{_hcl(ov)}"', f'{k}_regex = "{_hcl(nv)}"', f"waf {k}_regex")

    return edits, warn, reports


def _hcl(rx):
    """python 정규식 문자열을 waf.tf HCL 리터럴 형태로(백슬래시 이스케이프)."""
    return rx.replace("\\", "\\\\")


def _tuner_prefix_edits(add, files, old_paths, new_paths, warn):
    """튜너의 '/<prefix>/$APP'(제네릭, 전 앱 공용)는 **전체 앱이 균일하게** 프리픽스 이동할 때만
    치환한다. 일부 앱만 바뀌면 이 제네릭을 못 바꾸므로(다른 앱 깨짐) 경고만. k8s /api/v1/nodes 안 건드림."""
    changed = [a for a in new_paths if old_paths.get(a) != new_paths[a]]
    if not changed:
        return

    def pref(paths):
        pm = {}
        for a, p in paths.items():
            suf = "/" + a
            pm[a] = p[:-len(suf)] if p.endswith(suf) else None
        return pm

    op_all, np_all = pref(old_paths), pref(new_paths)
    old_set, new_set = set(op_all.values()), set(np_all.values())
    uni_old = None not in old_set and len(old_set) == 1
    uni_new = None not in new_set and len(new_set) == 1
    if uni_old and uni_new and next(iter(old_set)) != next(iter(new_set)):
        po, pn = next(iter(old_set)), next(iter(new_set))
        for fp in files:
            add(fp, f"{po}/$APP", f"{pn}/$APP", "tuner /$APP 프리픽스(전체 균일 이동)")
            add(fp, f"{po}/${{APP}}", f"{pn}/${{APP}}", "tuner /${APP} 프리픽스")
    else:
        warn.append("튜너 concurrency/profile/pretune 는 경로를 '/<prefix>/$APP'(제네릭)로 만든다. "
                    "일부 앱만 경로가 바뀌어(비균일) 이 제네릭은 자동치환 불가 → 바뀐 앱은 그 파일에서 수동 확인.")


# ── waf.tf SPEC:PATHS locals 재생성 (마커 구간; BEGIN 주석·들여쓰기 보존) ─────
def regen_paths_local(waf_text, ns):
    def lst(xs):
        return "[" + ", ".join(f'"{x}"' for x in xs) + "]"
    body = ("\n".join([
        f'  waf_get_exact  = {lst(ns["get_paths"]())}',
        f'  waf_post_exact = {lst(ns["post_paths"]())}',
        f'  waf_put_exact  = {lst(ns["put_paths"]())}',
        f'  waf_prefix     = {lst(ns["PREFIXES"])}',
    ]) + "\n")
    b, e = "# SPEC:PATHS:BEGIN", "# SPEC:PATHS:END"
    # BEGIN(+주석 꼬리) 줄은 보존하고, 그 다음~END 앞까지의 본문만 교체
    pat = re.compile(r"(" + re.escape(b) + r"[^\n]*\n)(.*?)([ \t]*" + re.escape(e) + r")", re.DOTALL)
    m = pat.search(waf_text)
    if not m:
        return waf_text
    return waf_text[:m.start()] + m.group(1) + body + m.group(3) + waf_text[m.end():]


def apply_edits_to_text(text, pairs):
    changed = []
    for o, n, why in pairs:
        if o in text and o != n:
            cnt = text.count(o)
            text = text.replace(o, n)
            changed.append((why, cnt))
    return text, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--tf", default=os.path.join(HERE, "..", "terraform"))
    args = ap.parse_args()
    tf = os.path.abspath(args.tf)

    ns = load_spec()
    cur = snapshot_of(ns)

    if args.reset:
        save_snapshot(cur)
        print("✓ 현재 spec.json 을 기준선 스냅샷으로 저장했다 (.spec_change_snapshot.json).")
        return

    old = load_snapshot()
    if old is None:
        save_snapshot(cur)
        print("✓ 최초 실행 — 현재 spec.json 을 기준선 스냅샷으로 저장했다.")
        print("  이제 spec.json 을 고치고 다시 실행하면 '바뀐 것'만 잡아 terraform/튜너에 반영한다.")
        return

    edits, warn, reports = build_edits(old, cur, tf, ns)

    # waf.tf 경로 locals(마커) 도 갱신 대상에 포함 — 변경 여부는 '의미 비교'로 판정(오탐 방지)
    waf_path = os.path.join(tf, "waf.tf")
    with open(waf_path, encoding="utf-8") as f:
        waf_orig = f.read()
    local_changed = (old.get("paths") != cur["paths"] or old.get("put") != cur["put"]
                     or old.get("prefixes") != cur.get("prefixes"))
    waf_after_local = regen_paths_local(waf_orig, ns) if local_changed else waf_orig

    if not edits and not local_changed:
        print("✓ spec.json 이 스냅샷과 동일 — 바꿀 것 없음(terraform/튜너 그대로).")
        return

    # 파일별 diff 생성
    print("== 변경 계획 (dry-run) ==" if not args.apply else "== 적용 ==")
    file_new = {}   # path -> new_text
    for path, pairs in edits.items():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            before = f.read()
        after = before
        if path == waf_path:
            after = waf_after_local  # locals 먼저 반영
        after, ch = apply_edits_to_text(after, pairs)
        if after == before and (path != waf_path or not local_changed):
            continue
        file_new[path] = after
        rel = os.path.relpath(path, HERE)
        print(f"\n--- {rel} ---")
        for why, cnt in ch:
            print(f"   · {why}  ({cnt}곳)")
        diff = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                    fromfile="a/" + rel, tofile="b/" + rel, n=1)
        sys.stdout.writelines(list(diff)[:60])
    # waf.tf 가 edits 엔 없지만 locals 만 바뀐 경우
    if waf_path not in file_new and local_changed:
        file_new[waf_path] = waf_after_local
        print(f"\n--- {os.path.relpath(waf_path, HERE)} (경로 locals) ---")

    if warn:
        print("\n⚠ 수동 확인:")
        for w in warn:
            print("   -", w)
    if reports:
        print("\nℹ 튜너 필드(자동치환 안 함) — 위치 확인:")
        for r in reports:
            print("   -", r)

    if not args.apply:
        print("\n※ dry-run. 맞으면 `--apply` 로 적용(+백업+terraform validate).")
        return

    # ── 적용: 백업 → 쓰기 → terraform validate → 실패 시 복구 ──
    backups = {}
    for path, after in file_new.items():
        with open(path, encoding="utf-8") as f:
            backups[path] = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(backups[path])
        with open(path, "w", encoding="utf-8") as f:
            f.write(after)
    # fmt + validate
    ok = True
    try:
        subprocess.run(["terraform", "fmt"], cwd=tf, check=False, capture_output=True)
        r = subprocess.run(["terraform", "validate"], cwd=tf, capture_output=True, text=True)
        print("\n" + (r.stdout or "") + (r.stderr or ""))
        ok = (r.returncode == 0)
    except FileNotFoundError:
        print("\n(terraform 없음 — validate 스킵. 대회 환경에서 반드시 terraform validate 확인.)")
    if not ok:
        print("✗ terraform validate 실패 → 백업으로 자동 복구한다(오염 방지).")
        for path, orig in backups.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(orig)
        sys.exit(1)
    save_snapshot(cur)
    print("✓ 적용 완료 + terraform validate 통과. 스냅샷 갱신. .bak 백업 남김.")
    print("  이제 terraform apply 로 한 번에 반영하면 된다.")


if __name__ == "__main__":
    main()
