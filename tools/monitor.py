#!/usr/bin/env python3
"""
AWS WAF 로그에서 헤더 정보를 실시간으로 모니터링하고 1분 단위 통계를 생성하는 스크립트
"""

import boto3
import json
import time
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict, Counter
import threading

class WAFHeaderMonitor:
    def __init__(self, region_name: str = 'us-east-1'):
        """
        WAF 헤더 모니터링 클래스 초기화
        
        Args:
            region_name: AWS 리전 이름
        """
        self.client = boto3.client('logs', region_name=region_name)
        self.log_group = 'aws-waf-logs-cloudwatch'
        self.log_stream = 'cloudfront_apdev-waf_0'
        self.last_timestamp = None
        
        # 통계 수집용 변수들
        self.header_stats = defaultdict(lambda: defaultdict(int))  # {minute: {header_key_value: count}}
        self.current_minute_headers = Counter()  # 현재 분의 헤더 카운트 (키=값 형태)
        self.last_minute = None
        self.stats_lock = threading.Lock()
        
    def get_log_events(self, start_time: Optional[int] = None) -> List[Dict]:
        """
        CloudWatch Logs에서 로그 이벤트를 가져옴
        
        Args:
            start_time: 시작 타임스탬프 (밀리초)
            
        Returns:
            로그 이벤트 리스트
        """
        try:
            params = {
                'logGroupName': self.log_group,
                'logStreamName': self.log_stream,
                'startFromHead': False  # 최신 로그부터 가져오기
            }
            
            if start_time:
                params['startTime'] = start_time
                
            response = self.client.get_log_events(**params)
            return response.get('events', [])
            
        except Exception as e:
            print(f"로그 이벤트 가져오기 실패: {e}")
            return []
    
    def parse_waf_log(self, log_message: str) -> Optional[Dict]:
        """
        WAF 로그 메시지를 파싱하여 JSON 객체로 변환
        
        Args:
            log_message: 로그 메시지 문자열
            
        Returns:
            파싱된 JSON 객체 또는 None
        """
        try:
            return json.loads(log_message)
        except json.JSONDecodeError as e:
            print(f"JSON 파싱 실패: {e}")
            return None
    
    def extract_headers(self, waf_log: Dict) -> List[Dict]:
        """
        WAF 로그에서 헤더 정보 추출
        
        Args:
            waf_log: 파싱된 WAF 로그 JSON
            
        Returns:
            헤더 리스트
        """
        try:
            http_request = waf_log.get('httpRequest', {})
            headers = http_request.get('headers', [])
            return headers
        except Exception as e:
            print(f"헤더 추출 실패: {e}")
            return []
    
    def update_header_stats(self, headers: List[Dict], timestamp: int):
        """
        헤더 통계 업데이트
        
        Args:
            headers: 헤더 리스트
            timestamp: 타임스탬프 (밀리초)
        """
        dt = datetime.fromtimestamp(timestamp / 1000)
        current_minute_key = dt.strftime('%Y-%m-%d %H:%M')
        
        with self.stats_lock:
            # 새로운 분이 시작되면 이전 분 통계를 저장하고 초기화
            if self.last_minute and self.last_minute != current_minute_key:
                if self.current_minute_headers:
                    self.header_stats[self.last_minute] = dict(self.current_minute_headers)
                    self.save_minute_stats(self.last_minute, dict(self.current_minute_headers))
                self.current_minute_headers.clear()
            
            # 현재 분의 헤더 카운트 증가 (키=값 형태로 저장)
            for header in headers:
                header_name = header.get('name', 'unknown')
                header_value = header.get('value', '')
                # 헤더 키=값 형태로 저장
                header_key_value = f"{header_name}={header_value}"
                self.current_minute_headers[header_key_value] += 1
            
            self.last_minute = current_minute_key
    
    def save_minute_stats(self, minute_key: str, header_counts: Dict[str, int]):
        """
        1분 단위 통계를 텍스트 파일로 저장
        
        Args:
            minute_key: 분 단위 키 (YYYY-MM-DD HH:MM)
            header_counts: 헤더별 카운트 (키=값 형태)
        """
        try:
            # 통계 디렉토리 생성
            stats_dir = "waf_header_stats"
            if not os.path.exists(stats_dir):
                os.makedirs(stats_dir)
            
            # 파일명 생성 (콜론을 언더스코어로 변경)
            safe_minute = minute_key.replace(':', '_').replace(' ', '_')
            filename = f"{stats_dir}/stats_{safe_minute}.txt"
            
            # 통계 데이터 준비
            sorted_headers = sorted(header_counts.items(), key=lambda x: x[1], reverse=True)
            total_requests = sum(header_counts.values())
            unique_headers = len(header_counts)
            
            # 텍스트 파일로 저장
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(f"시간: {minute_key}\n")
                f.write(f"총요청: {total_requests}\n")
                f.write(f"유니크헤더: {unique_headers}\n")
                f.write("\n=== 헤더 통계 (키=값) ===\n")
                
                for header_key_value, count in sorted_headers:
                    f.write(f"{header_key_value}: {count}\n")
            
            # 콘솔에 통계 출력
            stats_data = {
                'timestamp': minute_key,
                'total_requests': total_requests,
                'unique_headers': unique_headers,
                'most_common': sorted_headers[:5],
                'least_common': sorted_headers[-5:] if len(sorted_headers) >= 5 else sorted_headers,
                'all_headers': dict(sorted_headers)
            }
            self.print_minute_stats(minute_key, stats_data)
            
        except Exception as e:
            print(f"통계 저장 실패: {e}")
    
    def print_minute_stats(self, minute_key: str, stats_data: Dict):
        """
        1분 단위 통계를 콘솔에 예쁘게 출력
        
        Args:
            minute_key: 분 단위 키
            stats_data: 통계 데이터
        """
        # 색상 코드
        BLUE = '\033[94m'
        GREEN = '\033[92m'
        YELLOW = '\033[93m'
        RED = '\033[91m'
        PURPLE = '\033[95m'
        CYAN = '\033[96m'
        WHITE = '\033[97m'
        BOLD = '\033[1m'
        RESET = '\033[0m'
        
        print(f"\n{BOLD}{PURPLE}{'📊' * 20} 1분 통계 {'📊' * 20}{RESET}")
        print(f"{BOLD}{CYAN}⏰ 시간:{RESET} {YELLOW}{minute_key}{RESET}")
        print(f"{BOLD}{CYAN}📈 총 요청:{RESET} {GREEN}{stats_data['total_requests']}{RESET}")
        print(f"{BOLD}{CYAN}🔢 유니크 헤더=값:{RESET} {GREEN}{stats_data['unique_headers']}{RESET}")
        
        print(f"\n{BOLD}{GREEN}🔥 가장 많이 사용된 헤더=값 TOP 5:{RESET}")
        for i, (header_key_value, count) in enumerate(stats_data['most_common'], 1):
            icon = ['🥇', '🥈', '🥉', '🏅', '🎖️'][i-1] if i <= 5 else '🔸'
            # 헤더=값이 너무 길면 잘라내기
            display_header = header_key_value[:60] + "..." if len(header_key_value) > 60 else header_key_value
            print(f"  {icon} {YELLOW}{display_header}:{RESET} {WHITE}{count}회{RESET}")
        
        if len(stats_data['least_common']) > 0:
            print(f"\n{BOLD}{RED}❄️  가장 적게 사용된 헤더=값:{RESET}")
            for header_key_value, count in stats_data['least_common']:
                display_header = header_key_value[:60] + "..." if len(header_key_value) > 60 else header_key_value
                print(f"  🔹 {YELLOW}{display_header}:{RESET} {WHITE}{count}회{RESET}")
        
        print(f"{PURPLE}{'─' * 60}{RESET}")
    
    def format_headers_output(self, headers: List[Dict], timestamp: int, 
                            client_ip: str = None, method: str = None, uri: str = None) -> str:
        """
        헤더 정보를 보기 좋게 포맷
        
        Args:
            headers: 헤더 리스트
            timestamp: 타임스탬프
            client_ip: 클라이언트 IP
            method: HTTP 메소드
            uri: URI
            
        Returns:
            포맷된 출력 문자열
        """
        dt = datetime.fromtimestamp(timestamp / 1000)
        
        # 색상 코드
        BLUE = '\033[94m'
        GREEN = '\033[92m'
        YELLOW = '\033[93m'
        RED = '\033[91m'
        PURPLE = '\033[95m'
        CYAN = '\033[96m'
        WHITE = '\033[97m'
        BOLD = '\033[1m'
        RESET = '\033[0m'
        
        # 메소드별 색상
        method_color = {
            'GET': GREEN,
            'POST': BLUE,
            'PUT': YELLOW,
            'DELETE': RED,
            'PATCH': PURPLE
        }.get(method, WHITE)
        
        output = [
            f"\n{CYAN}{'═' * 80}{RESET}",
            f"{BOLD}{WHITE}🕒 시간:{RESET} {BLUE}{dt.strftime('%Y-%m-%d %H:%M:%S')}{RESET}",
            f"{BOLD}{WHITE}🌐 클라이언트 IP:{RESET} {GREEN}{client_ip or 'N/A'}{RESET}",
            f"{BOLD}{WHITE}📨 메소드:{RESET} {method_color}{BOLD}{method or 'N/A'}{RESET}",
            f"{BOLD}{WHITE}🔗 URI:{RESET} {CYAN}{uri or 'N/A'}{RESET}",
            f"{BOLD}{WHITE}📋 헤더:{RESET}",
        ]
        
        if headers:
            for i, header in enumerate(headers, 1):
                name = header.get('name', 'unknown')
                value = header.get('value', '')
                
                # 헤더명별 아이콘
                icon = {
                    'host': '🏠',
                    'user-agent': '🤖',
                    'content-type': '📄',
                    'authorization': '🔐',
                    'referer': '🔙',
                    'accept': '✅',
                    'cookie': '🍪',
                    'x-forwarded-for': '🔀'
                }.get(name.lower(), '🔸')
                
                # 값이 너무 길면 잘라내기
                display_value = value[:100] + "..." if len(value) > 100 else value
                
                output.append(f"   {icon} {YELLOW}{name}:{RESET} {WHITE}{display_value}{RESET}")
        else:
            output.append(f"   {RED}📭 (헤더 없음){RESET}")
            
        output.append(f"{CYAN}{'─' * 80}{RESET}")
        return "\n".join(output)
    
    def monitor_headers(self, poll_interval: int = 1):
        """
        헤더 정보를 실시간으로 모니터링
        
        Args:
            poll_interval: 폴링 간격 (초)
        """
        # 색상 코드
        BLUE = '\033[94m'
        GREEN = '\033[92m'
        YELLOW = '\033[93m'
        BOLD = '\033[1m'
        RESET = '\033[0m'
        
        print(f"\n{BOLD}{BLUE}🚀 WAF 로그 헤더 모니터링 + 통계 분석 시작...{RESET}")
        print(f"{YELLOW}📁 로그 그룹:{RESET} {self.log_group}")
        print(f"{YELLOW}📊 로그 스트림:{RESET} {self.log_stream}")
        print(f"{YELLOW}⏱️  폴링 간격:{RESET} {poll_interval}초")
        print(f"{YELLOW}📈 통계 저장:{RESET} waf_header_stats/ 디렉토리")
        print(f"{GREEN}{'═' * 80}{RESET}")
        print(f"{BOLD}📡 실시간 모니터링 중... (Ctrl+C로 종료){RESET}")
        
        # 통계 디렉토리 미리 생성
        stats_dir = "waf_header_stats"
        if not os.path.exists(stats_dir):
            os.makedirs(stats_dir)
            print(f"{GREEN}📁 통계 디렉토리 생성: {stats_dir}{RESET}")
        
        # 현재 시간에서 5분 전부터 모니터링 시작
        start_time = int((datetime.now() - timedelta(minutes=5)).timestamp() * 1000)
        last_stats_save = time.time()
        
        while True:
            try:
                # 새로운 로그 이벤트 가져오기
                events = self.get_log_events(start_time)
                print(f"🔍 로그 이벤트 {len(events)}개 조회됨")
                
                processed_count = 0
                for event in events:
                    event_timestamp = event.get('timestamp')
                    
                    # 이미 처리한 로그는 스킵
                    if self.last_timestamp and event_timestamp <= self.last_timestamp:
                        continue
                    
                    processed_count += 1
                    log_message = event.get('message', '')
                    waf_log = self.parse_waf_log(log_message)
                    
                    if waf_log:
                        # 헤더 추출
                        headers = self.extract_headers(waf_log)
                        
                        # 통계 업데이트
                        self.update_header_stats(headers, event_timestamp)
                        
                        # 추가 정보 추출
                        http_request = waf_log.get('httpRequest', {})
                        client_ip = http_request.get('clientIp')
                        method = http_request.get('httpMethod')
                        uri = http_request.get('uri')
                        
                        # 헤더 출력
                        formatted_output = self.format_headers_output(
                            headers, event_timestamp, client_ip, method, uri
                        )
                        print(formatted_output)
                        
                        # 마지막 처리 타임스탬프 업데이트
                        self.last_timestamp = event_timestamp
                
                # 다음 폴링을 위한 시작 시간 업데이트
                if events:
                    new_start_time = max(event.get('timestamp', 0) for event in events) + 1
                    if new_start_time != start_time:
                        start_time = new_start_time
                
                # 30초마다 강제로 통계 저장 (로그가 없어도)
                current_time = time.time()
                if current_time - last_stats_save >= 30:
                    with self.stats_lock:
                        if self.current_minute_headers and self.last_minute:
                            print(f"⏰ 30초 경과 - 강제 통계 저장: {self.last_minute}")
                            self.save_minute_stats(self.last_minute, dict(self.current_minute_headers))
                            last_stats_save = current_time
                        else:
                            print(f"⏰ 30초 경과 - 저장할 통계 없음")
                
                # 폴링 간격만큼 대기
                time.sleep(poll_interval)
                
            except KeyboardInterrupt:
                print(f"\n{BOLD}{YELLOW}👋 모니터링을 종료합니다.{RESET}")
                # 마지막 분의 통계가 있다면 저장
                with self.stats_lock:
                    if self.current_minute_headers and self.last_minute:
                        self.save_minute_stats(self.last_minute, dict(self.current_minute_headers))
                break
            except Exception as e:
                print(f"\n{BOLD}❌ 모니터링 중 오류 발생: {e}{RESET}")
                time.sleep(poll_interval)

def main():
    """메인 함수"""
    try:
        # WAF 헤더 모니터링 시작
        monitor = WAFHeaderMonitor()
        monitor.monitor_headers(poll_interval=1)  # 1초마다 체크
        
    except Exception as e:
        print(f"스크립트 실행 실패: {e}")

if __name__ == "__main__":
    main()