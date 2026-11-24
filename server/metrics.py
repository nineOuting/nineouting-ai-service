# -*- coding: utf-8 -*-
"""
토큰 사용량 지표 (OpenTelemetry)

지표 이름: gen_ai.client.token.usage (OTel GenAI 규약), 단위 {token}, 히스토그램
속성
  gen_ai.operation.name  chat
  gen_ai.provider.name   gcp.gemini / anthropic / openai
  gen_ai.request.model   모델 이름
  gen_ai.token.type      input / output
  ai.task                작업 종류: live_summary(회의 중 요약), draft(끝난 회의 요약), agenda_classification(안건 분류), agenda_summary(안건별 요약),
                         agenda_status(안건 상태 판정), agenda_opinions(안건별 의견 정리), agenda_todos(안건별 할 일 추출),
                         agenda_offtopic(새 안건 제목 제안).
                         챗봇(chatbot)은 해당 기능이 생기면 추가

내보내기
  GET /metrics               Prometheus 형식 (항상)
  OTEL_EXPORTER_OTLP_ENDPOINT  설정하면 OTLP(HTTP)로도 보낸다
"""
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

PROVIDER_NAMES = {"gemini": "gcp.gemini", "anthropic": "anthropic", "openai": "openai"}
BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864]
DURATION_BUCKETS = [0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120]


class Metrics:
    def __init__(self, readers, service_name="nineouting-ai-service"):
        self.provider = MeterProvider(resource=Resource.create({"service.name": service_name}),
                                      metric_readers=list(readers))
        meter = self.provider.get_meter("nineouting.ai_service")
        self.token_usage = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}",
            description="LLM 호출의 입력·출력 토큰 수",
            explicit_bucket_boundaries_advisory=BUCKETS)
        self.operation_duration = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s",
            description="LLM 호출 응답 시간",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS)

    def record_token_usage(self, provider, model, task, input_tokens, output_tokens):
        base = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": PROVIDER_NAMES.get(provider, provider),
            "gen_ai.request.model": model or "unknown",
            "ai.task": task or "unknown",
        }
        self.token_usage.record(int(input_tokens), {**base, "gen_ai.token.type": "input"})
        self.token_usage.record(int(output_tokens), {**base, "gen_ai.token.type": "output"})

    def record_operation(self, provider, model, task, duration_seconds, error_type=None):
        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": PROVIDER_NAMES.get(provider, provider),
            "gen_ai.request.model": model or "unknown",
            "ai.task": task or "unknown",
        }
        if error_type:
            attributes["error.type"] = error_type
        self.operation_duration.record(float(duration_seconds), attributes)

    def watch_call_limiter(self, limiter):
        """모델 호출 전체 동시 수 상한(summary/call_limit.py)의 현재 값을 지표로 내보낸다. 상한을 켜기 전에도 세기 때문에
        DEV 에서 동시에 몇 개가 나가는지(in_flight_peak) 재서 상한을 정하는 데 쓴다."""
        from opentelemetry.metrics import Observation
        meter = self.provider.get_meter("nineouting.ai_service")
        for name, key, desc in (("ai.llm.calls.in_flight", "inFlight", "지금 나가 있는 회의 중 모델 호출 수"),
                                ("ai.llm.calls.waiting", "waiting", "동시 수 상한 때문에 기다리는 모델 호출 수"),
                                ("ai.llm.calls.in_flight_peak", "peak", "서버를 켠 뒤 동시에 나가 있던 모델 호출의 최대 수"),
                                ("ai.llm.calls.limit", "limit", "모델 호출 전체 동시 수 상한 (0 이면 상한 없음)")):
            meter.create_observable_gauge(name, callbacks=[lambda options, k=key: [Observation(limiter.snapshot()[k])]],
                                          unit="{call}", description=desc)

    def shutdown(self):
        self.provider.shutdown()


def default_readers(env):
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    readers = [PrometheusMetricReader()]
    if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))     # 주소는 환경변수에서 읽는다
    return readers
