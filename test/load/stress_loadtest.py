"""
stress_loadtest.py
stress API 부하 테스트 — 짧고 강한 CPU 부하 패턴
여러 번의 짧은 스파이크로 HPA 스케일링 반응 확인
비정상 트래픽(WAF) + 채점 + HTML 리포트
"""
import asyncio, aiohttp, argparse, time, uuid, random, statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich import box
from rich.rule import Rule

try:
    import boto3
    AWS_AVAILABLE = True
except ImportError:
    AWS_AVAILABLE = False

console = Console()

# 짧고 강한 스파이크 반복 패턴 — HPA 반응 테스트
STAGES = [
    (60,  5),    # 1m → 5  VU  워밍업
    (60,  250),  # 1m → 250 VU 1차 스파이크 (강하게)
    (120, 250),  # 2m → 250 유지
    (60,  10),   # 1m → 10 VU  급격히 DROP
    (60,  5),    # 1m → 5  VU  잠시 쉬기
    (60,  200),  # 1m → 200 VU 2차 스파이크
    (120, 200),  # 2m → 200 유지
    (60,  10),   # 1m → 10 VU  DROP
    (60,  5),    # 1m → 5  VU  잠시 쉬기
    (60,  300),  # 1m → 300 VU 3차 스파이크 (최대)
    (120, 300),  # 2m → 300 유지
    (120, 50),   # 2m → 50 VU  감소
    (120, 10),   # 2m → 10 VU  감소
    (300, 5),    # 5m → 5  VU  스케일인 대기 (HPA stabilization window)
    (300, 0),    # 5m → 0  VU  노드 draining 대기
]

SLO_AVAIL = 5.0
SLO_PERF  = 1.0

ATTACK_CATEGORIES = {
    "wrong_method":  ("잘못된 HTTP 메소드",   403),
    "missing_param": ("파라미터 누락/형식 오류", 403),
    "sqli":          ("SQL 인젝션",           403),
    "bad_body":      ("잘못된 body",           403),
    "bad_header":    ("비정상 헤더",           403),
    "bad_ctype":     ("잘못된 Content-Type",   403),
    "wrong_path":    ("잘못된 경로",           404),
}

def rid(): return str(random.randint(100000000000, 999999999999))
def uid(): return str(uuid.uuid4())
def jh():  return {"Content-Type": "application/json"}
def fmt_ms(ms): return f"{ms:.0f}ms" if ms < 1000 else f"{ms/1000:.2f}s"
def pc(v):
    if v >= 90: return "green"
    if v >= 80: return "yellow"
    if v >= 50: return "orange1"
    return "red"
def pc_html(v):
    if v >= 90: return "good"
    if v >= 80: return "warn"
    if v >= 50: return "low"
    return "bad"

@dataclass
class Stats:
    latencies: list = field(default_factory=list)
    codes: dict = field(default_factory=lambda: defaultdict(int))

    def record(self, status, lat):
        self.latencies.append(lat)
        self.codes[status] += 1

    @property
    def total(self): return len(self.latencies)
    @property
    def availability(self):
        if not self.total: return 0.0
        return 100.0 * sum(v for k,v in self.codes.items() if 200<=k<300) / self.total
    def performance(self, slo):
        if not self.total: return 0.0
        return 100.0 * sum(1 for l in self.latencies if l <= slo*1000) / self.total
    def pct(self, p):
        if not self.latencies: return 0.0
        s = sorted(self.latencies)
        return s[min(int(len(s)*p/100), len(s)-1)]
    @property
    def avg(self): return statistics.mean(self.latencies) if self.latencies else 0.0

@dataclass
class AttackStats:
    category: str
    expected_status: int = 403
    total: int = 0
    correct: int = 0
    wrong: int = 0
    status_dist: dict = field(default_factory=lambda: defaultdict(int))

    def record(self, status: int):
        self.total += 1
        self.status_dist[status] += 1
        if status == self.expected_status:
            self.correct += 1
        else:
            self.wrong += 1

    @property
    def correct_rate(self):
        return 100.0 * self.correct / self.total if self.total else 0.0

class State:
    def __init__(self):
        self.stats = Stats()
        self.attack_stats = {k: AttackStats(label, exp) for k, (label, exp) in ATTACK_CATEGORIES.items()}
        self.current_vus = 0; self.target_vus = 0
        self.rps_window = []; self.current_rps = 0.0
        self.phase = "대기"; self.phase_num = 0
        self.start_time = time.time()
        self.spike_count = 0
        self.max_rps = 0.0
        self.instance_counts: list = []
        self.base_instance_count: int = 0

    def elapsed(self): return time.time() - self.start_time
    def update_rps(self):
        now = time.time()
        self.rps_window.append(now)
        self.rps_window = [t for t in self.rps_window if t >= now-5.0]
        self.current_rps = len(self.rps_window) / 5.0
        if self.current_rps > self.max_rps:
            self.max_rps = self.current_rps

def get_ec2_instance_count(region: str, cluster: str) -> int:
    if not AWS_AVAILABLE: return 0
    try:
        ec2 = boto3.client("ec2", region_name=region)
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:aws:eks:cluster-name", "Values": [cluster]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        return sum(len(r["Instances"]) for r in resp["Reservations"])
    except Exception:
        return 0

async def ec2_sampler(state: State, region: str, cluster: str, stop: asyncio.Event):
    while not stop.is_set():
        count = await asyncio.get_event_loop().run_in_executor(
            None, get_ec2_instance_count, region, cluster)
        if count > 0:
            state.instance_counts.append(count)
        await asyncio.sleep(10)

PHASE_NAMES = ["워밍업","1차스파이크","1차유지","DROP","휴식","2차스파이크","2차유지","DROP","휴식","3차스파이크","3차유지","감소","감소","종료"]

def render(state: State) -> Layout:
    elapsed = state.elapsed()
    mins, secs = divmod(int(elapsed), 60)
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="attack", size=16),
        Layout(name="bottom", size=10),
    )
    layout["body"].split_row(Layout(name="main", ratio=3), Layout(name="cost", ratio=1))
    layout["bottom"].split_row(Layout(name="timeline"), Layout(name="spike_info"))

    layout["header"].update(Panel(
        f"[bold]stress_loadtest[/bold]  경과: [yellow]{mins:02d}:{secs:02d}[/yellow]  "
        f"VU: [cyan]{state.current_vus}[/cyan]/{state.target_vus}  "
        f"RPS: [green]{state.current_rps:.1f}[/green] (최대: [bold]{state.max_rps:.1f}[/bold])  "
        f"페이즈: [cyan]{state.phase}[/cyan]"
    ))

    st = state.stats
    avail = st.availability; perf = st.performance(SLO_PERF)
    c2 = sum(v for k,v in st.codes.items() if 200<=k<300)
    c4 = sum(v for k,v in st.codes.items() if 400<=k<500)
    c5 = sum(v for k,v in st.codes.items() if k>=500)

    t = Table(box=box.ROUNDED, expand=True, header_style="bold cyan")
    t.add_column("지표"); t.add_column("값", justify="right")
    t.add_column("지표"); t.add_column("값", justify="right")

    ca = pc(avail); cp = pc(perf)
    t.add_row("총 요청", f"{st.total:,}", "2xx", f"[green]{c2:,}[/green]")
    t.add_row("가용성", f"[{ca}]{avail:.2f}%[/{ca}]", "4xx", f"[yellow]{c4:,}[/yellow]" if c4 else "0")
    t.add_row("성능(≤1s)", f"[{cp}]{perf:.2f}%[/{cp}]", "5xx", f"[red]{c5:,}[/red]" if c5 else "0")
    t.add_row("avg", fmt_ms(st.avg), "P50", fmt_ms(st.pct(50)))
    t.add_row("P95", fmt_ms(st.pct(95)), "P99", fmt_ms(st.pct(99)))
    t.add_row("최대 RPS", f"[bold]{state.max_rps:.1f}[/bold]", "스파이크", f"{state.spike_count}회")

    layout["main"].update(Panel(t, title="[bold]stress API 성능[/bold]"))

    # 비용 사이드 패널
    avg_inst = statistics.mean(state.instance_counts) if state.instance_counts else 0
    base_cnt = state.base_instance_count
    cost_ratio_val = (avg_inst / base_cnt) if base_cnt > 0 and state.instance_counts else None
    cost_table = Table(box=box.SIMPLE, expand=True, show_header=False)
    cost_table.add_column("항목", style="cyan"); cost_table.add_column("값", justify="right")
    cost_table.add_row("기준 인스턴스", str(base_cnt))
    cost_table.add_row("현재 평균", f"{avg_inst:.1f}")
    cr_color = "green" if cost_ratio_val and 0.5 <= cost_ratio_val <= 1.5 else ("yellow" if cost_ratio_val and cost_ratio_val <= 2.5 else "red")
    cost_table.add_row("Cost Ratio", f"[{cr_color}]{cost_ratio_val:.2f}[/]" if cost_ratio_val else "[dim]-[/dim]")
    cost_table.add_row("샘플 수", str(len(state.instance_counts)))
    layout["cost"].update(Panel(cost_table, title="[bold]비용[/bold]"))

    # 비정상 트래픽 테이블
    atk_table = Table(box=box.ROUNDED, expand=True, header_style="bold red")
    atk_table.add_column("공격 유형", width=22)
    atk_table.add_column("총 요청", justify="right", width=7)
    atk_table.add_column("기대코드", justify="right", width=8)
    atk_table.add_column("정상처리", justify="right", width=8)
    atk_table.add_column("비정상", justify="right", width=7)
    atk_table.add_column("정상처리율", justify="right", width=9)
    atk_table.add_column("응답코드 분포")

    waf_cats = [a for k, a in state.attack_stats.items() if k != "wrong_path"]
    total_waf = sum(a.total for a in waf_cats)
    total_waf_blocked = sum(a.correct for a in waf_cats)
    total_waf_rate = 100.0 * total_waf_blocked / total_waf if total_waf else 0.0

    for cat, ast in state.attack_stats.items():
        if ast.total == 0:
            atk_table.add_row(ast.category, "0", str(ast.expected_status), "-", "-", "-", "-")
            continue
        cr = ast.correct_rate
        dist = ", ".join(f"{k}:{v}" for k,v in sorted(ast.status_dist.items()))
        atk_table.add_row(ast.category, str(ast.total),
            str(ast.expected_status),
            f"[green]{ast.correct}[/]",
            f"[red]{ast.wrong}[/]" if ast.wrong else "0",
            f"[{pc(cr)}]{cr:.1f}%[/]", dist)

    atk_table.add_section()
    atk_table.add_row("[bold]WAF합계[/bold]", f"[bold]{total_waf}[/bold]",
        "403",
        f"[bold green]{total_waf_blocked}[/bold green]",
        f"[bold red]{total_waf-total_waf_blocked}[/bold red]",
        f"[bold {pc(total_waf_rate)}]{total_waf_rate:.1f}%[/bold {pc(total_waf_rate)}]", "차단율 기준")
    layout["attack"].update(Panel(atk_table, title="[bold red]비정상 트래픽 처리[/bold red]"))

    total_dur = sum(d for d,_ in STAGES)
    ec = min(elapsed, total_dur)
    bw = 50; filled = int(bw * ec / total_dur)
    bar = "[green]"+"█"*filled+"[/green][dim]"+"░"*(bw-filled)+"[/dim]"
    layout["timeline"].update(Panel(
        bar + f"\n[yellow]단계 {state.phase_num}/{len(STAGES)}  {mins:02d}:{secs:02d} / {total_dur//60:02d}:00[/yellow]",
        title="타임라인"
    ))

    spike_info = (
        f"[bold]스파이크 패턴[/bold]\n"
        f"1차: 250VU × 2분\n"
        f"2차: 200VU × 2분\n"
        f"3차: 300VU × 2분 (최대)\n"
        f"\n[dim]HPA 반응: 부하 증가 후\n약 60~90초 내 파드 증가[/dim]"
    )
    layout["spike_info"].update(Panel(spike_info, title="스파이크 정보"))
    return layout


def build_attacks(base: str):
    async def req(session, method, url, **kwargs):
        async with session.request(method, url, **kwargs) as r:
            await r.read()
            return r.status, r

    attacks = []

    # 잘못된 메소드
    for method in ["GET", "DELETE", "PATCH", "OPTIONS", "PUT"]:
        url = f"{base}/v1/stress"
        attacks.append(("wrong_method",
            lambda s, m=method, u=url: req(s, m, u, headers=jh(), json={"requestid": rid(), "uuid": uid()})))

    # 파라미터 누락/오류 (body 기반이므로 필드 누락)
    attacks += [
        ("missing_param", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"requestid": rid(), "uuid": uid()})),  # length 누락
        ("missing_param", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"uuid": uid(), "length": 256})),  # requestid 누락
        ("missing_param", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"requestid": "abc", "uuid": uid(), "length": 256})),  # requestid 형식 오류
        ("missing_param", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"requestid": rid(), "uuid": "not-uuid", "length": 256})),  # uuid 형식 오류
    ]

    # SQLi
    attacks += [
        ("sqli", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"requestid": "1 OR 1=1", "uuid": uid(), "length": 256})),
        ("sqli", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"requestid": "'; DROP TABLE stress;--", "uuid": uid(), "length": 256})),
        ("sqli", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(),
            json={"requestid": rid(), "uuid": "' UNION SELECT 1,2,3--", "length": 256})),
    ]

    # 잘못된 body
    attacks += [
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(), json={"wrong": "data"})),
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(), data="")),
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(), data="not json")),
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/stress", headers=jh(), json={})),
    ]

    # 비정상 헤더
    attacks += [
        ("bad_header", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={**jh(), "X-Attack": "payload"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
        ("bad_header", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={**jh(), "X-Hacker": "true"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
        ("bad_header", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={**jh(), "Authorization": "Bearer fake"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
    ]

    # 잘못된 Content-Type
    attacks += [
        ("bad_ctype", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={"Content-Type": "text/html"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
        ("bad_ctype", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={"Content-Type": "application/xml"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
    ]

    # 잘못된 경로 (404 기대)
    for path in ["/v1/none", "/v1/attack", "/v1/hack", "/v1/admin",
                 "/v1/delete", "/v1/drop", "/api/stress", "/v2/stress"]:
        attacks.append(("wrong_path",
            lambda s, u=f"{base}{path}": req(s, "GET", u, headers=jh(),
                params={"requestid": rid(), "uuid": uid()})))

    return attacks


async def abnormal_worker(session, base, state, stop):
    attacks = build_attacks(base)
    while not stop.is_set():
        cat, fn = random.choice(attacks)
        try:
            status, _ = await fn(session)
        except Exception:
            status = 0
        state.attack_stats[cat].record(status)
        await asyncio.sleep(random.uniform(0.2, 0.5))


async def worker(session, base, state, stop):
    while not stop.is_set():
        length = random.choice([64, 128, 256, 512])
        t0 = time.time()
        try:
            async with session.post(f"{base}/v1/stress",
                json={"requestid": rid(), "uuid": uid(), "length": length},
                headers=jh()) as resp:
                await resp.read()
                state.stats.record(resp.status, (time.time()-t0)*1000)
        except Exception:
            state.stats.record(0, (time.time()-t0)*1000)
        state.update_rps()
        await asyncio.sleep(random.uniform(0.05, 0.15))


async def scheduler(base, state, stop, done):
    conn = aiohttp.TCPConnector(limit=500)
    session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=30))
    tasks = []
    prev_target = 0

    stop_abnormal = asyncio.Event()
    abnormal_tasks = []

    async def delayed_abnormal():
        await asyncio.sleep(300)  # 5분 후 시작
        for _ in range(8):
            abnormal_tasks.append(
                asyncio.create_task(abnormal_worker(session, base, state, stop_abnormal)))

    asyncio.create_task(delayed_abnormal())

    for idx, (dur, target) in enumerate(STAGES):
        state.phase = PHASE_NAMES[idx] + f" → {target}VU"
        state.phase_num = idx+1; state.target_vus = target
        if target > prev_target and prev_target <= 10:
            state.spike_count += 1
        cur = len(tasks)
        if target > cur:
            for _ in range(target-cur):
                tasks.append(asyncio.create_task(worker(session, base, state, stop)))
        elif target < cur:
            for t in tasks[:cur-target]: t.cancel()
            tasks = tasks[cur-target:]
        state.current_vus = len(tasks)
        prev_target = target
        await asyncio.sleep(dur)

    stop_abnormal.set()
    stop.set()
    for t in tasks + abnormal_tasks: t.cancel()
    await session.close()
    done.set()


def _calc_cost_ratio(state: State) -> float | None:
    if not state.instance_counts or state.base_instance_count == 0: return None
    return statistics.mean(state.instance_counts) / state.base_instance_count

def _waf_block_rate(state: State):
    waf_cats = [a for k, a in state.attack_stats.items() if k != "wrong_path"]
    total = sum(a.total for a in waf_cats)
    blocked = sum(a.correct for a in waf_cats)
    return total, blocked, (100.0 * blocked / total if total else 0.0)

def _calc_scores(state: State, _unused=None):
    # WAF 차단율 (2pt)
    _, _, waf_rate = _waf_block_rate(state)
    waf_score = sum(0.5 for th in [90, 85, 80, 50] if waf_rate >= th)

    # stress 가용성/성능 (각 4pt)
    avail = state.stats.availability
    perf  = state.stats.performance(SLO_PERF)
    avail_score = sum(0.5 for th in [90, 87.5, 85, 82.5, 80, 70, 50, 30] if avail >= th)
    perf_score  = sum(0.5 for th in [90, 87.5, 85, 82.5, 80, 70, 50, 30] if perf >= th)

    # 비용 최적화 (12pt) — 전제조건: stress 성능 30% 이상
    cost_ratio = _calc_cost_ratio(state)
    perf_prereq = perf >= 30
    if cost_ratio and perf_prereq:
        cost_score = sum(1.0 for upper in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75]
                         if 0.5 <= cost_ratio <= upper)
    else:
        cost_score = 0.0

    return {
        "waf":         waf_score,
        "avail":       avail_score,
        "perf":        perf_score,
        "cost":        cost_score,
        "cost_ratio":  cost_ratio,
        "perf_prereq": perf_prereq,
        "total":       waf_score + avail_score + perf_score + cost_score,
        "max":         22.0,
    }


def print_report(state: State):
    console.print(Rule("[bold yellow]최종 결과[/bold yellow]"))

    total_waf, total_waf_blocked, total_block_rate = _waf_block_rate(state)

    st = state.stats
    avail = st.availability; perf = st.performance(SLO_PERF)
    avail_sc = sum(0.5 for th in [90,87.5,85,82.5,80,70,50,30] if avail>=th)
    perf_sc  = sum(0.5 for th in [90,87.5,85,82.5,80,70,50,30] if perf>=th)
    dist = " / ".join(f"HTTP {k}:{v}건" for k,v in sorted(st.codes.items()))
    ca = pc(avail); cp = pc(perf)
    console.print(Panel(
        f"총 요청: {st.total:,}  스파이크: {state.spike_count}회  최대 RPS: {state.max_rps:.1f}\n"
        f"가용성: [{ca}]{avail:.2f}%[/{ca}]  성능(≤1.0s): [{cp}]{perf:.2f}%[/{cp}]\n"
        f"avg: {fmt_ms(st.avg)}  P50: {fmt_ms(st.pct(50))}  P95: {fmt_ms(st.pct(95))}  P99: {fmt_ms(st.pct(99))}\n"
        f"응답코드: {dist}\n"
        f"채점 예상 — 가용: {avail_sc:.1f}pt / 4pt  성능: {perf_sc:.1f}pt / 4pt",
        title="[bold]stress API 결과[/bold]"
    ))

    scores = _calc_scores(state)
    cr_str = f"{scores['cost_ratio']:.3f}" if scores['cost_ratio'] else "N/A"
    avg_inst_str = f"{statistics.mean(state.instance_counts):.1f}" if state.instance_counts else "N/A"
    wp = state.attack_stats.get("wrong_path")
    wp_rate = wp.correct_rate if wp and wp.total else 0.0

    score_table = Table(title="채점 예상 (stress 측정분 22pt 만점)", box=box.DOUBLE_EDGE, header_style="bold yellow")
    score_table.add_column("항목"); score_table.add_column("배점", justify="right")
    score_table.add_column("획득", justify="right"); score_table.add_column("비고")
    score_table.add_row("WAF 차단 (비정상요청)", "2pt",
        f"[{pc(scores['waf']/2*100)}]{scores['waf']:.1f}pt[/]",
        f"차단율 {total_block_rate:.1f}%  ({total_waf_blocked}/{total_waf})  ※wrong_path 제외")
    score_table.add_row("stress 가용성", "4pt",
        f"[{pc(scores['avail']/4*100)}]{scores['avail']:.1f}pt[/]",
        "채점기준표 8단계")
    score_table.add_row("stress 성능(≤1.0s)", "4pt",
        f"[{pc(scores['perf']/4*100)}]{scores['perf']:.1f}pt[/]",
        "채점기준표 8단계")
    score_table.add_row("비용 최적화", "12pt",
        f"[{pc(scores['cost']/12*100)}]{scores['cost']:.1f}pt[/]",
        f"ratio={cr_str}  기준 {state.base_instance_count}대 / 평균 {avg_inst_str}대  전제조건={'충족' if scores['perf_prereq'] else '미충족'}")
    score_table.add_section()
    score_table.add_row("[bold]합계[/bold]", "[bold]22pt[/bold]",
        f"[bold]{scores['total']:.1f}pt[/bold]",
        "[dim]user/product/image는 각 스크립트에서 측정[/dim]")
    console.print(score_table)

    fname = f"report_stress_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    _save_html_report(state, scores, fname)
    console.print(f"\nHTML 리포트: [cyan]{fname}[/cyan]")


def _save_html_report(state: State, scores, fname):
    now_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")
    total_dur = sum(d for d,_ in STAGES)
    cr_str = f"{scores['cost_ratio']:.3f}" if scores['cost_ratio'] else "N/A"
    avg_inst_str = f"{statistics.mean(state.instance_counts):.1f}" if state.instance_counts else "N/A"
    total_waf, total_waf_blocked, total_block_rate = _waf_block_rate(state)
    total_atk = sum(a.total for a in state.attack_stats.values())
    wp = state.attack_stats.get("wrong_path")
    wp_rate = wp.correct_rate if wp and wp.total else 0.0
    st = state.stats
    avail = st.availability; perf = st.performance(SLO_PERF)
    c2 = sum(v for k,v in st.codes.items() if 200<=k<300)
    c4 = sum(v for k,v in st.codes.items() if 400<=k<500)
    c5 = sum(v for k,v in st.codes.items() if k>=500)
    code_dist = " / ".join(f"HTTP {k}: {v:,}건" for k,v in sorted(st.codes.items()))

    atk_rows = ""
    for cat, ast in state.attack_stats.items():
        cr = ast.correct_rate
        dist = " / ".join(f"HTTP {k}: {v}건" for k,v in sorted(ast.status_dist.items()))
        atk_rows += f"""
        <tr>
          <td>{ast.category}</td><td>{ast.total:,}</td>
          <td style="color:var(--sub)">{ast.expected_status}</td>
          <td class="good">{ast.correct:,}</td>
          <td class="{'bad' if ast.wrong else ''}">{ast.wrong:,}</td>
          <td class="{pc_html(cr)}">{cr:.1f}%</td>
          <td>{dist}</td>
        </tr>"""

    waf_cls = pc_html(total_block_rate)

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stress_loadtest Report</title>
<style>
  :root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--sub:#8b949e;
         --good:#3fb950;--warn:#d29922;--low:#f0883e;--bad:#f85149}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:14px}}
  h1{{padding:24px 32px 4px;font-size:22px}}
  .meta{{padding:4px 32px 20px;color:var(--sub);font-size:12px}}
  .section{{margin:0 32px 28px}}
  .section h2{{font-size:15px;border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px}}
  table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}}
  th{{background:#21262d;padding:10px 12px;text-align:left;font-size:12px;color:var(--sub)}}
  td{{padding:9px 12px;border-top:1px solid var(--border);font-size:13px;vertical-align:middle}}
  tr:hover td{{background:#1c2128}}
  .good{{color:var(--good);font-weight:600}}
  .warn{{color:var(--warn);font-weight:600}}
  .low{{color:var(--low);font-weight:600}}
  .bad{{color:var(--bad);font-weight:600}}
  .total-row td{{background:#21262d;font-size:15px}}
  .summary-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:8px}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px 20px}}
  .card .label{{font-size:11px;color:var(--sub);margin-bottom:6px}}
  .card .value{{font-size:26px;font-weight:700}}
  .card .sub{{font-size:11px;color:var(--sub);margin-top:4px}}
</style>
</head>
<body>
<h1>⚡ stress_loadtest Report</h1>
<p class="meta">생성: {now_str} &nbsp;|&nbsp; 총 소요: {total_dur//60}분 &nbsp;|&nbsp; 측정 범위: stress API + WAF + 비용 (22pt / 40pt)</p>

<div class="section">
  <h2>🏆 채점 예상 (stress 측정분 22pt)</h2>
  <table>
    <tr><th>항목</th><th>배점</th><th>획득 점수</th><th>세부 내역</th></tr>
    <tr><td>WAF 차단 (비정상요청처리)</td><td>2pt</td>
        <td class="{pc_html(scores['waf']/2*100)}">{scores['waf']:.1f}pt</td>
        <td>차단율 {total_block_rate:.1f}% ({total_waf_blocked:,}/{total_waf:,})  ※wrong_path 제외</td></tr>
    <tr><td>stress 가용성</td><td>4pt</td>
        <td class="{pc_html(scores['avail']/4*100)}">{scores['avail']:.1f}pt</td>
        <td>8단계 기준 (90/87.5/85/82.5/80/70/50/30%)</td></tr>
    <tr><td>stress 성능 (≤1.0s)</td><td>4pt</td>
        <td class="{pc_html(scores['perf']/4*100)}">{scores['perf']:.1f}pt</td>
        <td>8단계 기준</td></tr>
    <tr><td>비용 최적화</td><td>12pt</td>
        <td class="{pc_html(scores['cost']/12*100)}">{scores['cost']:.1f}pt</td>
        <td>ratio={cr_str} / 기준 {state.base_instance_count}대 / 평균 {avg_inst_str}대 / 전제조건: {"충족" if scores["perf_prereq"] else "미충족"}</td></tr>
    <tr class="total-row"><td><b>합계</b></td><td><b>22pt</b></td>
        <td class="{pc_html(scores['total']/22*100)}"><b>{scores['total']:.1f}pt</b></td>
        <td style="color:var(--sub);font-size:12px">user/product/image는 각 스크립트에서 측정</td></tr>
  </table>
</div>

<div class="section">
  <h2>📊 요약</h2>
  <div class="summary-grid">
    <div class="card"><div class="label">총 정상 요청</div>
      <div class="value">{st.total:,}</div>
      <div class="sub">스파이크 {state.spike_count}회 / 최대 {state.max_rps:.0f} RPS</div></div>
    <div class="card"><div class="label">총 비정상 요청</div>
      <div class="value">{total_atk:,}</div></div>
    <div class="card"><div class="label">WAF 차단율</div>
      <div class="value {waf_cls}">{total_block_rate:.1f}%</div>
      <div class="sub">차단 {total_waf_blocked:,} / {total_waf:,} (wrong_path 제외)</div></div>
    <div class="card"><div class="label">Cost Ratio</div>
      <div class="value">{cr_str}</div>
      <div class="sub">평균 {avg_inst_str}대 / 기준 {state.base_instance_count}대</div></div>
    <div class="card"><div class="label">예상 점수</div>
      <div class="value">{scores['total']:.1f}pt</div>
      <div class="sub">/ 22pt 측정 가능</div></div>
  </div>
</div>

<div class="section">
  <h2>🚀 API 성능</h2>
  <table>
    <tr><th>API</th><th>총 요청</th><th>가용성</th><th>성능(SLO)</th><th>SLO기준</th>
        <th>avg</th><th>P50</th><th>P95</th><th>P99</th>
        <th>2xx</th><th>4xx</th><th>5xx</th>
        <th>가용 점수</th><th>성능 점수</th><th>응답코드 분포</th></tr>
    <tr>
      <td><b>POST /v1/stress</b></td><td>{st.total:,}</td>
      <td class="{pc_html(avail)}">{avail:.2f}%</td>
      <td class="{pc_html(perf)}">{perf:.2f}%</td>
      <td>≤1.0s</td>
      <td>{fmt_ms(st.avg)}</td><td>{fmt_ms(st.pct(50))}</td>
      <td>{fmt_ms(st.pct(95))}</td><td>{fmt_ms(st.pct(99))}</td>
      <td class="good">{c2:,}</td><td class="warn">{c4:,}</td>
      <td class="{'bad' if c5 else ''}">{c5:,}</td>
      <td>{scores['avail']:.1f}pt / 4pt</td><td>{scores['perf']:.1f}pt / 4pt</td>
      <td style="font-size:11px;color:#8b949e">{code_dist}</td>
    </tr>
  </table>
</div>

<div class="section">
  <h2>🛡️ 비정상 요청 처리 (WAF)</h2>
  <table>
    <tr><th>공격 유형</th><th>총 요청</th><th>기대코드</th><th>정상처리</th><th>비정상</th><th>정상처리율</th><th>응답코드 분포</th></tr>
    {atk_rows}
    <tr style="border-top:2px solid var(--border)">
      <td><b>WAF 합계</b></td><td><b>{total_waf:,}</b></td>
      <td style="color:var(--sub)">403</td>
      <td class="good"><b>{total_waf_blocked:,}</b></td>
      <td class="{'bad' if total_waf-total_waf_blocked else ''}">{total_waf-total_waf_blocked:,}</td>
      <td class="{waf_cls}"><b>{total_block_rate:.1f}%</b></td>
      <td>채점 예상: <b>{scores['waf']:.1f}pt / 2.0pt</b> &nbsp;|&nbsp; 잘못된 경로(404): <b>{wp_rate:.1f}%</b></td>
    </tr>
  </table>
</div>

<div class="section">
  <h2>💰 비용 최적화</h2>
  <table>
    <tr><th>항목</th><th>값</th><th>설명</th></tr>
    <tr><td>기준 인스턴스</td><td>{state.base_instance_count}대</td><td>테스트 시작 시점 EC2 수</td></tr>
    <tr><td>평균 인스턴스</td><td>{avg_inst_str}대</td><td>10초 간격 샘플 {len(state.instance_counts)}회 평균</td></tr>
    <tr><td>Cost Ratio</td><td>{cr_str}</td><td>평균 인스턴스 / 기준 인스턴스 (0.5 이상 필요)</td></tr>
    <tr><td>성능 전제조건</td><td>{"충족" if scores["perf_prereq"] else "미충족"}</td><td>stress 성능 30% 이상이어야 비용 점수 인정</td></tr>
    <tr><td><b>채점 예상</b></td><td><b>{scores["cost"]:.0f}pt / 12pt</b></td><td>ratio 0.5~1.0이면 12pt 만점</td></tr>
  </table>
</div>
</body></html>"""

    with open(fname, "w", encoding="utf-8") as f:
        f.write(html)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint")
    parser.add_argument("--aws-region", default="ap-northeast-2")
    parser.add_argument("--cluster-name", default="apdev-cluster")
    parser.add_argument("--base-instances", type=int, default=0,
                        help="기준 인스턴스 수 (0이면 시작 시 자동 조회)")
    args = parser.parse_args()
    base = args.endpoint.rstrip("/")
    console.print(f"[bold green]stress_loadtest 시작[/bold green] → {base}")
    console.print("[dim]짧고 강한 스파이크 3회 — HPA 스케일링 반응 관찰[/dim]")
    console.print("[dim]5분 후 비정상 트래픽(WAF 테스트) 자동 시작[/dim]")

    state = State()

    base_count = args.base_instances
    if base_count == 0 and AWS_AVAILABLE:
        base_count = get_ec2_instance_count(args.aws_region, args.cluster_name)
        console.print(f"EC2 조회 기준 인스턴스: [cyan]{base_count}[/cyan]대")
    if base_count == 0:
        try:
            base_count = int(input("기준 인스턴스 수 입력 (최소 노드 수, 보통 1): ").strip() or "1")
        except Exception:
            base_count = 1
    state.base_instance_count = base_count
    console.print(f"기준 인스턴스: [cyan]{base_count}[/cyan]대")

    stop = asyncio.Event(); done = asyncio.Event(); ec2_stop = asyncio.Event()
    asyncio.create_task(ec2_sampler(state, args.aws_region, args.cluster_name, ec2_stop))
    asyncio.create_task(scheduler(base, state, stop, done))
    with Live(render(state), refresh_per_second=2, console=console) as live:
        while not done.is_set():
            live.update(render(state))
            await asyncio.sleep(0.5)
    ec2_stop.set()
    print_report(state)

if __name__ == "__main__":
    asyncio.run(main())
