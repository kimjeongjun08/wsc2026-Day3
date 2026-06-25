import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';
import { htmlReport } from 'https://raw.githubusercontent.com/benc-uk/k6-reporter/main/dist/bundle.js';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.0.2/index.js';

const userPerf   = new Rate('user_under_200ms');
const wafBlocked = new Counter('waf_blocked_count');
const wafPassed  = new Counter('waf_false_pass');

const BASE_URL = __ENV.BASE_URL || 'http://d15e7v31k9pt8k.cloudfront.net';

// 패턴 원본 유지
export const options = {
  stages: [
    { duration: '0s', target: 100 },
    { duration: '5m', target: 100 },
    { duration: '0s', target: 300 },
    { duration: '5m', target: 300 },
    { duration: '0s', target: 200 },
    { duration: '5m', target: 200 },
    { duration: '0s', target: 300 },
    { duration: '5m', target: 300 },
    { duration: '0s', target: 1000 },
    { duration: '5m', target: 1000 },
    { duration: '0s', target: 1000 },
    { duration: '5m', target: 1000 },
  ],
  thresholds: {
    'user_under_200ms': ['rate>0.9'],
    http_req_duration: ['p(95)<200'],
    http_req_failed: ['rate<0.1'],
  },
};

function rid() {
  return String(Math.floor(Math.random() * 900000000000) + 100000000000);
}

function validHeaders() {
  return { 'Content-Type': 'application/json' };
}

export default function () {
  const requestid = rid();
  const uuid = uuidv4();
  const uname = `k6user_${requestid}`;
  const email = `${uname}@example.org`;

  // POST /v1/user
  const postRes = http.post(`${BASE_URL}/v1/user`, JSON.stringify({
    requestid, uuid, username: uname, email
  }), { headers: validHeaders() });

  check(postRes, { 'user POST 2xx': (r) => r.status >= 200 && r.status < 300 });
  userPerf.add(postRes.timings.duration < 200);

  sleep(0.2);

  // GET /v1/user
  const getRes = http.get(
    `${BASE_URL}/v1/user?email=${encodeURIComponent(email)}&requestid=${rid()}&uuid=${uuidv4()}`,
    { headers: validHeaders() }
  );
  check(getRes, { 'user GET 2xx': (r) => r.status >= 200 && r.status < 300 });
  userPerf.add(getRes.timings.duration < 200);

  // 비정상 트래픽 (일부 VU에서만)
  if (Math.random() < 0.1) {
    const badId  = rid();
    const badUuid = uuidv4();
    const badName = `k6bad_${badId}`;
    const badEmail = `${badName}@example.org`;

    const attacks = [
      // 비정상 헤더
      () => http.post(`${BASE_URL}/v1/user`, JSON.stringify({ requestid: badId, uuid: badUuid, username: badName, email: badEmail }),
        { headers: { 'Content-Type': 'application/json', 'X-Hacker': 'true' } }),
      () => http.post(`${BASE_URL}/v1/user`, JSON.stringify({ requestid: badId, uuid: badUuid, username: badName, email: badEmail }),
        { headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer fake' } }),
      () => http.post(`${BASE_URL}/v1/user`, JSON.stringify({ requestid: badId, uuid: badUuid, username: badName, email: badEmail }),
        { headers: { 'Content-Type': 'text/html' } }),
      () => http.get(`${BASE_URL}/v1/user?email=${encodeURIComponent(badEmail)}&requestid=${badId}&uuid=${badUuid}`,
        { headers: { 'Content-Type': 'application/json', 'X-Attack': 'payload' } }),
      // 비정상 body
      () => http.post(`${BASE_URL}/v1/user`, JSON.stringify({ foo: 'bar' }), { headers: validHeaders() }),
      () => http.post(`${BASE_URL}/v1/user`, 'not json', { headers: validHeaders() }),
      () => http.post(`${BASE_URL}/v1/user`, JSON.stringify({ requestid: "1 OR 1=1", uuid: badUuid, username: badName, email: badEmail }),
        { headers: validHeaders() }),
      () => http.get(`${BASE_URL}/v1/user?email=${encodeURIComponent(badEmail)}&requestid=hack&uuid=${badUuid}`,
        { headers: validHeaders() }),
      () => http.get(`${BASE_URL}/v1/user?email=${encodeURIComponent(badEmail)}&requestid=${badId}&uuid=not-a-uuid`,
        { headers: validHeaders() }),
      () => http.del(`${BASE_URL}/v1/user`, null, { headers: validHeaders() }),
      () => http.put(`${BASE_URL}/v1/user`, '{}', { headers: validHeaders() }),
    ];

    const res = attacks[Math.floor(Math.random() * attacks.length)]();
    if (res.status === 403) {
      wafBlocked.add(1);
    } else {
      wafPassed.add(1);
    }
  }

  sleep(1);
}

export function handleSummary(data) {
  const now = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  return {
    [`./report_${now}.html`]: htmlReport(data),
    stdout: textSummary(data, { indent: ' ', enableColors: true }),
    [`./report_${now}.json`]: JSON.stringify(data, null, 2),
  };
}
