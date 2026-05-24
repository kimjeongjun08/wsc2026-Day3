import http from 'k6/http';
import { check, sleep } from 'k6';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

// 테스트 설정 - 즉시 VU 적용
export const options = {
  stages: [
    { duration: '0s', target: 200 },    // 즉시 200명으로 시작
    { duration: '5m', target: 200 },    // 5분간 200명 유지
    { duration: '0s', target: 500 },    // 즉시 500명으로 변경
    { duration: '5m', target: 500 },    // 5분간 500명 유지
    { duration: '0s', target: 200 },   // 즉시 200명으로 변경
    { duration: '5m', target: 200 },  // 10분간 200명 유지
    { duration: '0s', target: 1000 },   // 즉시 1000명으로 변경
    { duration: '5m', target: 1000 },   // 5분간 1000명 유지
    { duration: '0s', target: 500 },   // 즉시 500명으로 변경
    { duration: '5m', target: 500 },   // 5분간 500명 유지
    { duration: '0s', target: 300 },    // 즉시 300명으로 감소
    { duration: '5m', target: 300 },    // 5분간 300명 유지
  ],
  thresholds: {
    http_req_duration: ['p(95)<200'], // 95%의 요청이 0.2초 이내 완료
    http_req_failed: ['rate<0.1'],     // 에러율 10% 미만
  },
};

// 기본 URL 설정 (환경변수 또는 기본값 사용)
const BASE_URL = __ENV.BASE_URL || 'http://dv7x7l3k87prk.cloudfront.net';

export default function () {
  // UUID 자동 생성
  const uuid = uuidv4();
  
  // requestid 자동 생성 (13자리 숫자)
  const requestid = Math.floor(Math.random() * 9000000000000) + 1000000000000;
  
  // userid와 username을 동일한 값으로 설정 (user_ 접두사 포함)
  const userid = `user_${requestid}`;
  const username = `user_${requestid}`; // userid와 동일한 값
  
  // 이메일 생성
  const email = `${username}@example.org`;
  
  // POST 요청 데이터
  const payload = JSON.stringify({
    requestid: requestid.toString(),
    uuid: uuid,
    username: username,
 //   userid: userid,
    email: email,
    status_message: "I'm happy"
  });

  // POST 요청 헤더
  const headers = {
    'Content-Type': 'application/json',
    'User-Agent': 'curl/8.7.1',
  };

  // POST 요청 URL (쿼리 파라미터 포함)
  const url = `${BASE_URL}/v1/user`;

  // POST 요청 실행 (사용자 생성)
  const postResponse = http.post(url, payload, { headers });

  // POST 응답 검증
  check(postResponse, {
    'POST status is 200 or 201': (r) => r.status === 200 || r.status === 201,
    'POST response time < 200ms (SUCCESS)': (r) => r.timings.duration < 200,
    'POST response time >= 200ms (SLOW)': (r) => r.timings.duration >= 200,
    'POST response has correct structure': (r) => {
      try {
        const body = JSON.parse(r.body);
        return body && typeof body === 'object';
      } catch (e) {
        return false;
      }
    },
  });

  // POST 결과 로그 (상태 코드와 응답 시간 모두 고려)
  const postTimeOk = postResponse.timings.duration < 200;
  const postStatusOk = postResponse.status === 200 || postResponse.status === 201;
  const postTimeStatus = postTimeOk ? 'FAST' : 'SLOW';
  const postOverallStatus = postStatusOk && postTimeOk ? 'SUCCESS' : 'FAIL';
  
  console.log(`POST: ${requestid} | Status: ${postResponse.status} | Time: ${postResponse.timings.duration}ms | Result: ${postOverallStatus} (${postTimeStatus})`);
  
  // POST 성공 여부와 관계없이 GET 요청 실행
  sleep(0.2);
  
  // GET 요청 URL (생성된 사용자 확인)
  const getUrl = `${BASE_URL}/v1/user?email=${encodeURIComponent(email)}&requestid=${requestid}&uuid=${uuid}`;
  
  // GET 요청 실행
  const getResponse = http.get(getUrl, { headers });
  
  // GET 응답 검증
  check(getResponse, {
    'GET status is 200': (r) => r.status === 200,
    'GET response time < 200ms (SUCCESS)': (r) => r.timings.duration < 200,
    'GET response time >= 200ms (SLOW)': (r) => r.timings.duration >= 200,
    'GET response contains created user data': (r) => {
      try {
        const body = JSON.parse(r.body);
        return body && body.email === email && body.requestid === requestid.toString();
      } catch (e) {
        return false;
      }
    },
  });
  
  // GET 결과 로그 (상태 코드와 응답 시간 모두 고려)
  const getTimeOk = getResponse.timings.duration < 200;
  const getStatusOk = getResponse.status === 200;
  const getTimeStatus = getTimeOk ? 'FAST' : 'SLOW';
  const getOverallStatus = getStatusOk && getTimeOk ? 'SUCCESS' : 'FAIL';
  
  console.log(`GET: ${requestid} | Status: ${getResponse.status} | Time: ${getResponse.timings.duration}ms | Result: ${getOverallStatus} (${getTimeStatus})`);
  
  // 전체 요약
  console.log(`SUMMARY: ${requestid} | POST: ${postOverallStatus} | GET: ${getOverallStatus}`);

  sleep(1);
}