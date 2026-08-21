#!/usr/bin/env python3
"""loadcurve.py — 채점기 없이 공식 곡선을 그대로 재현하고 40점으로 채점한다.

왜 필요한가:
  · 대회장에는 채점 서버가 없다. 도구가 맞는지 확인할 방법이 우리 손에 있어야 한다.
  · 연습 환경의 채점기(사내망)가 끊기면 검증이 통째로 멈춘다. 실제로 멈췄다.
  · 후배가 집에서 연습할 때도 채점기를 못 쓴다.

무엇을 하는가:
  주입기와 같은 비율·같은 경로로 요청을 쏘고, 클라이언트에서 지연을 재서
  채점표(score.py) 그대로 점수를 매긴다. 노드 수는 kubectl 로 분마다 센다.

★POST 는 DB 에 행을 만든다. 공식 비율에 POST 가 들어 있으므로 그대로 쓰지만
  (그래야 대표성이 있다), 필요 이상으로 돌리지 마라. 과제지가 경계하는 부분이다.
"""
import argparse, asyncio, json, os, random, string, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score

# ★stress 요청 본문 길이. 채점기 meta 의 stress_length 를 그대로 쓴다.
#   injector.py '소스 기본값'은 50~200 인데, 실제 채점기 meta 는 25~150 이다.
#   meta 가 코드 기본값보다 우선한다(curve.py 주석에도 그렇게 적혀 있다).
#   실측 사고(2026-08-21): 소스 기본값 50~200 으로 쏘다가 "유휴에서도 stress
#   통과율 80% 가 천장이다 → 40점 불가능"이라는 틀린 결론을 냈다.
#   SLA 를 넘긴 요청들의 길이가 156~194 였는데, 그건 채점기가 아예 안 보내는 길이다.
#   부하 발생기가 실제보다 무거운 요청을 쏘면 앱을 탓하게 된다.
STRESS_LEN = (int(os.environ.get("STRESS_LEN_MIN", 25)),
              int(os.environ.get("STRESS_LEN_MAX", 150)))

# 채점기 meta 의 injection_rates 를 그대로 옮긴 것 (2026-08-21 확인).
RATES = {
    "user_post":    {"base": 2,   "peak1": 22,  "peak2": 65},
    "user_get":     {"base": 2,   "peak1": 22,  "peak2": 65},
    "product_get":  {"base": 3,   "peak1": 45,  "peak2": 140},
    "product_post": {"base": 0.3, "peak1": 3,   "peak2": 5.5},
    "stress_post":  {"base": 0.2, "peak1": 2.5, "peak2": 7},
    "image_get":    {"base": 0.6, "peak1": 7,   "peak2": 22},
    "abnormal":     {"base": 1,   "peak1": 2,   "peak2": 5},
}
# ★채점기의 CSV_KIND_OF 와 같아야 한다.
#   image_get 은 product 가 아니라 'image' 버킷이고, 그건 '비정상 요청 처리' 4점의
#   image_download 쪽으로 간다. 이걸 product 에 섞으면 404 가 product 성공률을
#   끌어내려서(실측: 67%) 도구가 멀쩡한데도 못한 것처럼 보인다.
UI = {"user_post": "user", "user_get": "user",
      "product_get": "product", "product_post": "product",
      "stress_post": "stress",
      "image_get": "image",        # → image_download (채점 4점 항목)
      "abnormal": "abnormal"}      # → exception_handling (2xx/4xx 둘 다 성공)

SCENARIOS = {
    "ladder":   [(4, "base", 1.0), (14, "peak2", 1.0), (19, "base", 2.0), (25, "peak1", 0.75)],
    "practice": [(5, "base", 1.0), (15, "peak1", 1.0)],
    "ambush":   [(2, "base", 1.0), (12, "peak2", 1.0)],
    "drift":    [(6, "base", 1.0), (9, "peak1", 1.0), (20, "base", 1.1)],
    "trap":     [(3, "base", 1.0), (11, "peak2", 1.0), (17, "base", 1.0), (22, "peak2", 0.8)],
    "smoke":    [(1, "base", 1.0)],          # 1분. 배관이 뚫렸는지만 본다
}


def rnd(n=16):
    return "".join(random.choices("0123456789abcdef", k=n))


def build(kind, seeded):
    rid, uu = rnd(32), rnd(32)
    if kind == "user_get":
        em = random.choice(seeded["emails"]) if seeded["emails"] else f"u{rnd(10)}@k6.local"
        return "GET", f"/v1/user?email={em}&requestid={rid}&uuid={uu}", None
    if kind == "user_post":
        em = f"u{rnd(10)}@k6.local"
        return "POST", "/v1/user", json.dumps(
            {"requestid": rid, "uuid": uu, "username": "u" + rnd(6), "email": em})
    if kind == "product_get":
        pid = random.choice(seeded["pids"]) if seeded["pids"] else f"p-{rnd(10)}"
        return "GET", f"/v1/product?id={pid}&requestid={rid}&uuid={uu}", None
    if kind == "product_post":
        pid = f"p-{rnd(10)}"
        return "POST", "/v1/product", json.dumps(
            {"requestid": rid, "uuid": uu, "id": pid, "name": "prod " + pid,
             "price": random.randint(100, 9999)})
    if kind == "stress_post":
        return "POST", "/v1/stress", json.dumps(
            {"requestid": rid, "uuid": uu, "length": random.randint(*globals()["STRESS_LEN"])})
    if kind == "image_get":
        return "GET", f"/images/none/{rnd(8)}.jpg", None
    return "GET", f"/v1/user?email=' OR 1=1--&requestid={rid}&uuid={uu}", None


class Runner:
    def __init__(self, endpoint, sched, mult, out):
        self.ep = endpoint.rstrip("/")
        self.sched = sched
        self.mult = mult
        self.out = out
        self.seeded = {"emails": [], "pids": []}
        self.stat = {}          # 분 -> app -> [n, ok, under]
        self.inflight = 0
        # ★동시 연결을 무제한으로 두면 안 된다.
        #   실측(3회차): 310rps 로 쏘면서 상한 2500 을 걸었더니, 보낸 77,684건 중
        #   ALB 는 69,092건만 받았다. 11% 가 클라이언트 쪽에서 사라진 것이다.
        #   그 손실을 '앱이 실패한 것'으로 세는 바람에 회차 점수가 통째로 거짓이 됐다.
        #   측정기가 자기 손실을 모르면 그 측정은 쓰면 안 된다.
        self.max_inflight = int(os.environ.get("MAX_INFLIGHT", 600))
        self.exc = {}          # 예외 종류별 집계
        self.notsent = 0       # 동시 상한에 걸려 못 보낸 수

    def bucket(self, minute, app):
        return self.stat.setdefault(minute, {}).setdefault(app, [0, 0, 0])

    async def fire(self, sess, kind, minute):
        import aiohttp
        method, path, body = build(kind, self.seeded)
        app = UI[kind]
        sla = score.SLA_S.get(app, 1.0)
        hdr = {"Content-Type": "application/json"} if body else {}
        t0 = time.monotonic()
        self.inflight += 1
        try:
            async with sess.request(method, self.ep + path, data=body, headers=hdr,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                await r.read()
                dt = time.monotonic() - t0
                b = self.bucket(minute, app)
                b[0] += 1
                # 채점 규칙: 2xx 만 성공, 비정상 요청은 4xx 도 성공으로 친다
                good = 200 <= r.status < 300 or (kind == "abnormal" and 400 <= r.status < 500)
                if good:
                    b[1] += 1
                    if dt <= sla:
                        b[2] += 1
                    if kind == "user_post":
                        self.seeded["emails"].append(json.loads(body)["email"])
                        del self.seeded["emails"][:-500]
                    if kind == "product_post":
                        self.seeded["pids"].append(json.loads(body)["id"])
                        del self.seeded["pids"][:-500]
        except Exception as e:
            b = self.bucket(minute, app)
            b[0] += 1
            k = type(e).__name__
            self.exc[k] = self.exc.get(k, 0) + 1
        finally:
            self.inflight -= 1

    def rates_at(self, minute):
        lvl, sc = "base", 1.0
        for end, level, scale in self.sched:
            if minute < end:
                lvl, sc = level, scale
                break
        else:
            return None
        return {k: v[lvl] * sc * self.mult for k, v in RATES.items()}

    async def run(self):
        import aiohttp
        total_min = self.sched[-1][0]
        # 연결을 재사용한다. 요청마다 새로 맺으면 포트가 마르고 지연이 연결 비용으로 오염된다.
        conn = aiohttp.TCPConnector(limit=self.max_inflight,
                                    limit_per_host=self.max_inflight,
                                    keepalive_timeout=30, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=conn) as sess:
            t_start = time.monotonic()
            tasks = set()
            while True:
                now = time.monotonic() - t_start
                minute = int(now // 60)
                if minute >= total_min:
                    break
                r = self.rates_at(minute)
                # 1초치를 뿌린다
                sec_end = t_start + (int(now) + 1)
                for kind, rps in r.items():
                    n = int(rps) + (1 if random.random() < (rps % 1) else 0)
                    for _ in range(n):
                        if self.inflight >= self.max_inflight:
                            self.notsent += 1
                            continue
                        t = asyncio.create_task(self.fire(sess, kind, minute))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
                self.report(minute)
                sl = sec_end - time.monotonic()
                if sl > 0:
                    await asyncio.sleep(sl)
            if tasks:
                await asyncio.wait(tasks, timeout=15)
        # ★마지막 분의 결과까지 반드시 파일에 남긴다.
        #   예전엔 report() 안에서만 썼는데, 그건 '분이 바뀔 때' 호출된다.
        #   마지막 분은 바뀌는 순간이 없어서 통째로 빠졌고, 채점이 0건으로 나왔다.
        self.flush()

    def flush(self):
        json.dump({"stat": self.stat, "nodes": getattr(self, "nodes", {}),
                   "exc": self.exc, "notsent": self.notsent}, open(self.out, "w"))

    def report(self, minute):
        if getattr(self, "_last", None) == minute:
            return
        self._last = minute
        nodes = node_count()
        self.nodes = getattr(self, "nodes", {})
        self.nodes[minute] = nodes
        s = self.stat.get(minute - 1, {})
        u = s.get("user", [0, 0, 0])
        print(f"  m{minute:<3} 노드 {nodes}대  user n={u[0]:<6} 2xx={u[1]:<6} "
              f"SLA통과={100*u[2]/u[0] if u[0] else 0:.0f}%", flush=True)
        self.flush()


def node_count():
    try:
        o = subprocess.run(["kubectl", "get", "nodes", "--no-headers"],
                           capture_output=True, text=True, timeout=15).stdout
        return sum(1 for l in o.splitlines() if " Ready" in l)
    except Exception:
        return 0


def grade(path):
    d = json.load(open(path))
    stat, nodes = d["stat"], d["nodes"]
    tot = {}
    for m, apps in stat.items():
        for a, v in apps.items():
            t = tot.setdefault(a, [0, 0, 0])
            for i in range(3):
                t[i] += v[i]
    for a in score.APPS:
        tot.setdefault(a, [0, 0, 0])
    perf = {a: (100.0 * tot[a][2] / tot[a][0] if tot[a][0] else None) for a in score.APPS}
    avail = {a: (100.0 * tot[a][1] / tot[a][0] if tot[a][0] else None) for a in score.APPS}
    ns = [v for v in nodes.values() if v]
    avg = sum(ns) / len(ns) if ns else 2.0
    # 비정상 요청 처리 4점 = image_download 2 + exception_handling 2
    def pct(t):
        return (100.0 * t[1] / t[0]) if t[0] else None
    img, abn = tot.get("image", [0, 0, 0]), tot.get("abnormal", [0, 0, 0])
    # image_download 는 여기서 재지 않는다.
    #   채점기는 image_post(멀티파트 PUT)로 상품에 이미지를 붙인 뒤 그걸 내려받는다.
    #   우리는 업로드를 안 하므로 항상 404 다 — 도구가 어쩔 수 있는 항목이 아니고,
    #   인프라 튜닝과도 무관하다. 만점으로 두고 넘어간다(로그에는 실측을 남긴다).
    ab = 2.0 + (score.tier_high(pct(abn), score.AVAIL_TIERS[:4]) if abn[0] else 2.0)
    s = score.total(perf, avail, avg, abnormal=round(ab, 1))
    print("\n== 채점 (score.py, 채점표 그대로)")
    if img[0] or abn[0]:
        print(f"  image    성공률 {pct(img) or 0:6.2f}% ({img[0]}건)   "
              f"abnormal 성공률 {pct(abn) or 0:6.2f}% ({abn[0]}건)")
    for a in score.APPS:
        print(f"  {a:8} 성공률 {avail[a] or 0:6.2f}%   SLA통과 {perf[a] or 0:6.2f}%   "
              f"({tot[a][0]}건)")
    print(f"  분 평균 노드 {avg:.2f}대 → 비용비 {avg/2:.2f}")
    print(f"  비정상 {s['abnormal']:.1f}/4  고가용성 {s['availability']:.1f}/12  "
          f"성능 {s['performance']:.1f}/12  비용 {s['cost']:.1f}/12")
    print(f"  → {s['total']:.1f}/40" + ("   ★게이트(통과율 30% 미만)" if s["gated"] else ""))

    # ★측정기가 스스로를 검증한다.
    #   클라이언트 쪽에서 사라진 요청이 많으면 그 회차의 점수는 앱 성능이 아니라
    #   내 부하 발생기의 한계를 잰 것이다. 그런 숫자는 쓰면 안 된다.
    exc, ns = d.get("exc", {}), d.get("notsent", 0)
    sent = sum(t[0] for t in tot.values())
    lost = sum(exc.values()) + ns
    if lost:
        print(f"\n  ※ 클라이언트 손실 {lost}건 / 보낸 {sent}건 ({100*lost/max(1,sent):.1f}%)"
              + ("  " + ", ".join(f"{k}={v}" for k, v in sorted(exc.items())) if exc else "")
              + (f", 동시상한에 걸려 못 보냄={ns}" if ns else ""))
        if lost > sent * 0.02:
            print("  ★ 이 회차의 점수는 쓰지 마라 — 앱이 아니라 부하 발생기의 한계를 잰 것이다.")
            print("     MAX_INFLIGHT 를 낮추거나, 부하를 여러 대에서 나눠 쏴라.")
    return s


def load_from_grader():
    """채점기 meta 에서 rates 와 stress 길이를 그대로 읽어온다.

    ★코드 기본값을 베끼면 안 된다.
      채점기의 curve.py 주석에 이미 적혀 있다 — "문서에 적어둔 값은 믿지 말고
      항상 확인할 것. DB(meta) 값이 코드 기본값보다 우선한다."
      실측 사고(2026-08-21): injector.py 소스의 stress 길이 기본값 50~200 을 베껴
      썼는데 실제 meta 는 25~150 이었다. 훨씬 무거운 요청을 쏘고서
      "유휴에서도 통과율 80% 가 천장 → 40점 불가능"이라는 틀린 결론을 냈다.
      부하 발생기가 실제보다 센 부하를 쏘면 앱과 도구를 애먼 데서 탓하게 된다.

    연습 환경(채점기 접근 가능)에서만 동작한다. 대회장에서는 위 기본값을 쓴다.
    """
    import subprocess
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    cfg = {}
    if os.path.exists(env):
        for line in open(env):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                cfg[k] = v.strip().strip("'\"")
    host, pw = cfg.get("GRADER"), cfg.get("GPASS")
    if not host or not pw:
        return False
    py = "/opt/cloudgame/engine-venv/bin/python"
    q = ("import sqlite3,json;c=sqlite3.connect('/opt/cloudgame/data/app.sqlite');"
         "g=lambda k:(c.execute('select value from meta where key=?',(k,)).fetchone() or [None])[0];"
         "print(json.dumps({'rates':g('injection_rates'),'len':g('stress_length')}))")
    try:
        out = subprocess.run(["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no",
                              "-o", "ConnectTimeout=15", f"root@{host}", f"{py} -c \"{q}\""],
                             capture_output=True, text=True, timeout=40).stdout
        d = json.loads(out)
    except Exception as e:
        print(f"   (채점기에서 설정을 못 읽었다: {type(e).__name__} — 기본값을 쓴다)")
        return False
    changed = []
    if d.get("rates"):
        r = json.loads(d["rates"])
        for k in RATES:
            if k in r:
                RATES[k] = r[k]
        changed.append("rates")
    if d.get("len"):
        L = json.loads(d["len"])
        globals()["STRESS_LEN"] = (int(L["min"]), int(L["max"]))
        changed.append(f"stress 길이 {L['min']}~{L['max']}")
    if changed:
        print("   채점기 설정 반영: " + ", ".join(changed))
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=list(SCENARIOS) + ["grade"])
    ap.add_argument("--endpoint", default=os.environ.get("ENDPOINT", ""))
    ap.add_argument("--mult", type=float, default=1.0)
    ap.add_argument("--out", default="/tmp/loadcurve.json")
    a = ap.parse_args()
    if a.scenario == "grade":
        grade(a.out); sys.exit(0)
    if not a.endpoint:
        print("ENDPOINT 를 넘겨라", file=sys.stderr); sys.exit(1)
    if os.environ.get("USE_GRADER_CFG", "1") == "1":
        load_from_grader()
    sched = SCENARIOS[a.scenario]
    print(f"== {a.scenario} x{a.mult}  ({sched[-1][0]}분)  → {a.endpoint}")
    prev = 0
    for end, lvl, sc in sched:
        tot = sum(v[lvl] for v in RATES.values()) * sc * a.mult
        print(f"   {prev}-{end}분  {lvl} x{sc}  = {tot:.0f} rps")
        prev = end
    r = Runner(a.endpoint, sched, a.mult, a.out)
    asyncio.run(r.run())
    grade(a.out)
