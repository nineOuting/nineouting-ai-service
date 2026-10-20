# -*- coding: utf-8 -*-
"""
토큰 사용량 지표 (OpenTelemetry)

지표 이름: gen_ai.client.token.usage (OTel GenAI 규약), 단위 {token}, 히스토그램
속성
  gen_ai.operation.name  chat
  gen_ai.provider.name   gcp.gemini / anthropic / openai
  gen_ai.request.model   모델 이름
  gen_ai.token.type      input / output
  ai.task                작업 종류: live_summary(회의 중 요약). 초안·챗봇은 해당 기능이 생기면 추가

내보내기
  GET /metrics               Prometheus 형식 (항상)
  OTEL_EXPORTER_OTLP_ENDPOINT  설정하면 OTLP(HTTP)로도 보낸다
"""
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

PROVIDER_NAMES = {"gemini": "gcp.gemini", "anthropic": "anthropic", "openai": "openai"}
BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864]


class Metrics:
    def __init__(self, readers, service_name="nineouting-ai-service"):
        self.provider = MeterProvider(resource=Resource.create({"service.name": service_name}),
                                      metric_readers=list(readers))
        meter = self.provider.get_meter("nineouting.ai_service")
        self.token_usage = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}",
            description="LLM 호출의 입력·출력 토큰 수",
            explicit_bucket_boundaries_advisory=BUCKETS)

    def record_token_usage(self, provider, model, task, input_tokens, output_tokens):
        base = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": PROVIDER_NAMES.get(provider, provider),
            "gen_ai.request.model": model or "unknown",
            "ai.task": task or "unknown",
        }
        self.token_usage.record(int(input_tokens), {**base, "gen_ai.token.type": "input"})
        self.token_usage.record(int(output_tokens), {**base, "gen_ai.token.type": "output"})

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
