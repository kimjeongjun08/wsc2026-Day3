"""
Light Load Test + Validation
EKS + CloudFront + ALB 환경 검증용 (가벼운 테스트)

사용법: python main.py <endpoint>
예시:   python main.py https://d1234abcdef.cloudfront.net

테스트 항목:
1. GET /v1/user (email 조회)
2. POST /v1/user (생성)
3. GET /v1/product (id 조회)
4. POST /v1/product (생성)
5. PUT /v1/product (이미지 업로드)
6. GET /images/<path> (이미지 다운로드 확인)
7. POST /v1/stress
8. 비정상 요청 → 403/404 확인
"""
import asyncio
import aiohttp
import time
import uuid
import random
import sys
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class Stats:
    total: int = 0
    success: int = 0
    fail: int = 0
    response_times: List[float] = field(default_factory=list)

    @property
    def error_rate(self):
        return self.fail / self.total if self.total else 0

    @property
    def avg_ms(self):
        return sum(self.response_times) / len(self.response_times) if self.response_times else 0

    @property
    def p95_ms(self):
        if not self.response_times:
            return 0
        s = sorted(self.response_times)
        return s[int(len(s) * 0.95)]

    @property
    def under_200ms_pct(self):
        if not self.response_times:
            return 0
        return len([t for t in self.response_times if t <= 200]) / len(self.response_times) * 100

    @property
    def under_1s_pct(self):
        if not self.response_times:
            return 0
        return len([t for t in self.response_times if t <= 1000]) / len(self.response_times) * 100


def rid():
    return str(random.randint(100000000000, 999999999999))


def uid():
    return str(uuid.uuid4())


async def timed_request(session, method, url, **kwargs):
    start = time.time()
    try:
        async with session.request(method, url, **kwargs) as resp:
            body = await resp.read()
            elapsed = (time.time() - start) * 1000
            return resp.status, elapsed, body
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return 0, elapsed, str(e).encode()


def print_stats(name, stats):
    print(f"  {'✅' if stats.error_rate < 0.1 else '❌'} {name:<25} "
          f"avg={stats.avg_ms:.0f}ms  p95={stats.p95_ms:.0f}ms  "
          f"≤200ms={stats.under_200ms_pct:.0f}%  err={stats.error_rate:.0%}  "
          f"({stats.success}/{stats.total})")


async def main():
    if len(sys.argv) < 2:
        print("사용법: python main.py <endpoint>")
        print("예시:   python main.py https://d1234abcdef.cloudfront.net")
        sys.exit(1)

    base = sys.argv[1].rstrip("/")
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 10  # 기본 10개씩
    print(f"\n🚀 Light Load Test: {base} (count={count})")
    print(f"   {datetime.now().strftime('%H:%M:%S')}\n")

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:

        # === 1. POST /v1/user (데이터 생성) ===
        stats_user_post = Stats()
        test_users = []
        tasks = []
        for _ in range(min(count, 5)):
            uname = f"_test_{random.randint(10000000, 99999999)}"
            data = {"requestid": rid(), "uuid": uid(), "username": uname, "email": f"{uname}@test.org"}
            test_users.append(uname)
            tasks.append(timed_request(session, "POST", f"{base}/v1/user", json=data))
        results = await asyncio.gather(*tasks)
        for status, ms, _ in results:
            stats_user_post.total += 1
            stats_user_post.response_times.append(ms)
            if 200 <= status < 300:
                stats_user_post.success += 1
            else:
                stats_user_post.fail += 1

        # === 2. GET /v1/user (방금 생성한 데이터 조회) ===
        stats_user_get = Stats()
        tasks = []
        for uname in test_users:
            url = f"{base}/v1/user?email={uname}@test.org&requestid={rid()}&uuid={uid()}"
            tasks.append(timed_request(session, "GET", url))
        # 추가로 더 조회
        for _ in range(count - len(test_users)):
            uname = random.choice(test_users) if test_users else "_test_00000000"
            url = f"{base}/v1/user?email={uname}@test.org&requestid={rid()}&uuid={uid()}"
            tasks.append(timed_request(session, "GET", url))
        results = await asyncio.gather(*tasks)
        for status, ms, _ in results:
            stats_user_get.total += 1
            stats_user_get.response_times.append(ms)
            if 200 <= status < 300:
                stats_user_get.success += 1
            else:
                stats_user_get.fail += 1

        # === 3. POST /v1/product (데이터 생성) ===
        stats_prod_post = Stats()
        test_product_ids = []
        tasks = []
        for _ in range(min(count, 5)):
            pid = f"_test_{random.randint(10000000, 99999999)}"
            data = {"requestid": rid(), "uuid": uid(), "id": pid, "name": f"product_{pid}", "price": random.randint(1000, 50000)}
            test_product_ids.append(pid)
            tasks.append(timed_request(session, "POST", f"{base}/v1/product", json=data))
        results = await asyncio.gather(*tasks)
        for status, ms, _ in results:
            stats_prod_post.total += 1
            stats_prod_post.response_times.append(ms)
            if 200 <= status < 300:
                stats_prod_post.success += 1
            else:
                stats_prod_post.fail += 1

        # === 4. GET /v1/product (방금 생성한 데이터 조회) ===
        stats_prod_get = Stats()
        tasks = []
        for pid in test_product_ids:
            url = f"{base}/v1/product?id={pid}&requestid={rid()}&uuid={uid()}"
            tasks.append(timed_request(session, "GET", url))
        for _ in range(count - len(test_product_ids)):
            pid = random.choice(test_product_ids) if test_product_ids else "_test_00000000"
            url = f"{base}/v1/product?id={pid}&requestid={rid()}&uuid={uid()}"
            tasks.append(timed_request(session, "GET", url))
        results = await asyncio.gather(*tasks)
        for status, ms, _ in results:
            stats_prod_get.total += 1
            stats_prod_get.response_times.append(ms)
            if 200 <= status < 300:
                stats_prod_get.success += 1
            else:
                stats_prod_get.fail += 1

        # === 5. PUT /v1/product (이미지 업로드) ===
        # NOTE: PUT 형식은 채점 도구가 보내는 것과 동일해야 함. 테스트용 multipart.
        stats_put = Stats()
        test_product_id = test_product_ids[0] if test_product_ids else f"_test_{random.randint(10000000, 99999999)}"
        # 작은 테스트 이미지 생성 (1x1 PNG)
        png_data = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
                    b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
                    b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
                    b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')

        form = aiohttp.FormData()
        form.add_field("requestid", rid())
        form.add_field("uuid", uid())
        form.add_field("id", test_product_id)
        form.add_field("file", png_data, filename=f"{test_product_id}.png", content_type="image/png")

        status, ms, _ = await timed_request(session, "PUT", f"{base}/v1/product", data=form)
        stats_put.total += 1
        stats_put.response_times.append(ms)
        put_success = 200 <= status < 300
        if put_success:
            stats_put.success += 1
            print(f"  ✅ PUT /v1/product → {status} ({ms:.0f}ms) - 이미지 업로드 성공")
        else:
            stats_put.fail += 1
            print(f"  ⚠️  PUT /v1/product → {status} ({ms:.0f}ms) - 형식이 앱과 다를 수 있음 (대회 시 채점도구가 보냄)")

        # === 6. GET /images/ (이미지 다운로드) ===
        if put_success:
            await asyncio.sleep(2)  # S3 전파 대기
            img_url = f"{base}/images/{test_product_id}.png"
            status, ms, body = await timed_request(session, "GET", img_url)
            if status == 200 and len(body) > 0:
                print(f"  ✅ GET {img_url} → {status} ({ms:.0f}ms, {len(body)} bytes)")
            else:
                print(f"  ❌ GET {img_url} → {status} ({ms:.0f}ms) - 이미지 다운로드 실패")
        else:
            print(f"  ⏭️  이미지 다운로드 테스트 건너뜀 (PUT 실패)")

        # === 7. POST /v1/stress ===
        stats_stress = Stats()
        tasks = []
        for _ in range(count):
            data = {"requestid": rid(), "uuid": uid(), "length": 256}
            tasks.append(timed_request(session, "POST", f"{base}/v1/stress", json=data))
        results = await asyncio.gather(*tasks)
        for status, ms, _ in results:
            stats_stress.total += 1
            stats_stress.response_times.append(ms)
            if 200 <= status < 300:
                stats_stress.success += 1
            else:
                stats_stress.fail += 1

        # === 8. 비정상 요청 테스트 ===
        print(f"\n  --- 비정상 요청 테스트 ---")

        waf_tests = [
            # (method, path, headers, body, expect, description)
            ("DELETE", f"{base}/v1/user", None, None, 403, "잘못된 메소드 DELETE"),
            ("PATCH", f"{base}/v1/product", None, None, 403, "잘못된 메소드 PATCH"),
            ("GET", f"{base}/v1/user?requestid={rid()}&uuid={uid()}", None, None, 403, "GET /v1/user email 파라미터 누락"),
            ("GET", f"{base}/v1/product?requestid={rid()}&uuid={uid()}", None, None, 403, "GET /v1/product id 파라미터 누락"),
            ("GET", f"{base}/v1/stress", None, None, 403, "GET /v1/stress (POST만 허용)"),
            ("PUT", f"{base}/v1/user", None, None, 403, "PUT /v1/user (product만 PUT 허용)"),
            ("GET", f"{base}/v1/none", None, None, 403, "미등록 경로 /v1/none"),
            ("GET", f"{base}/admin", None, None, 403, "미등록 경로 /admin"),
            ("GET", f"{base}/.env", None, None, 403, "미등록 경로 /.env"),
            ("GET", f"{base}/wp-admin", None, None, 403, "미등록 경로 /wp-admin"),
        ]

        # SQLi 테스트 (화이트리스트라 쿼리 형식 안 맞으면 이미 WAF에서 403)
        sqli_tests = [
            ("GET", f"{base}/v1/user?email=' OR 1=1 --&requestid={rid()}&uuid={uid()}", None, None, 403, "SQLi in query (형식 불일치로 차단)"),
            ("POST", f"{base}/v1/user", None, {"requestid": "'; DROP TABLE", "uuid": uid(), "username": "test", "email": "test@test.com"}, 403, "SQLi in body (requestid 형식 불일치)"),
        ]

        # XSS 테스트
        xss_tests = [
            ("GET", f"{base}/v1/user?email=<script>alert(1)</script>&requestid={rid()}&uuid={uid()}", None, None, 403, "XSS in query (형식 불일치로 차단)"),
            ("POST", f"{base}/v1/user", None, {"requestid": "not_a_number", "uuid": "not-uuid", "username": "test", "email": "x@x.com"}, 403, "잘못된 requestid/uuid 형식"),
        ]

        # 비정상 헤더 테스트 (update_waf.py 실행 후에만 403)
        bad_header_tests = []  # 헤더 룰 적용 전이면 통과됨 - 두번째 테스트에서 확인

        # 비정상 바디 테스트
        bad_body_tests = [
            ("POST", f"{base}/v1/user", None, {"evil": "payload", "hack": True}, 403, "POST /v1/user requestid 없는 바디"),
            ("POST", f"{base}/v1/stress", None, {"wrong": "data"}, 403, "POST /v1/stress requestid 없는 바디"),
        ]

        all_tests = waf_tests + sqli_tests + xss_tests + bad_header_tests + bad_body_tests
        passed = 0
        failed = 0

        for method, url, headers, body, expect, desc in all_tests:
            kwargs = {}
            if headers:
                kwargs["headers"] = headers
            if body:
                kwargs["json"] = body
            status, ms, _ = await timed_request(session, method, url, **kwargs)
            ok = status == expect
            if ok:
                passed += 1
            else:
                failed += 1
            print(f"  {'✅' if ok else '❌'} {desc:<40} → {status} (expect {expect}) {ms:.0f}ms")

        print(f"\n  WAF 테스트 결과: {passed}/{passed+failed} 통과")

    # === 종합 ===
    print(f"\n{'='*60}")
    print(f"  📊 종합 결과")
    print(f"{'='*60}")
    print_stats("GET /v1/user", stats_user_get)
    print_stats("POST /v1/user", stats_user_post)
    print_stats("GET /v1/product", stats_prod_get)
    print_stats("POST /v1/product", stats_prod_post)
    print_stats("POST /v1/stress", stats_stress)
    print(f"\n  채점 기준 매칭:")
    print(f"    user  performance (≤200ms): {stats_user_get.under_200ms_pct:.1f}%  {'✅ PASS' if stats_user_get.under_200ms_pct >= 90 else '❌ FAIL'}")
    print(f"    product performance (≤200ms): {stats_prod_get.under_200ms_pct:.1f}%  {'✅ PASS' if stats_prod_get.under_200ms_pct >= 90 else '❌ FAIL'}")
    print(f"    stress performance (≤1s):   {stats_stress.under_1s_pct:.1f}%  {'✅ PASS' if stats_stress.under_1s_pct >= 90 else '❌ FAIL'}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
