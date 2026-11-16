#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
종료 처리 시간 계산기 (모델을 부르지 않는다. 비용 0)

이미 잰 "묶음 하나를 처리하는 시간"(평균, p95)으로 대기열을 흉내 내서, 회의 길이와 설정(묶음 합치기 상한)에 따라
종료 신호 -> 최종 보드까지 얼마나 걸리는지 어림한다. 실제 측정(python -m server.try_timing)을 하기 전에 방향을 보거나,
짧게 잰 처리 시간(try_timing 결과의 batchPasses)으로 긴 회의를 예측하는 데 쓴다.

모델
  - 묶음은 period 초마다 하나씩 온다. 일꾼 하나가 대기열에서 최대 coalesce 개를 한꺼번에 꺼내 한 번(패스)에 처리한다.
  - 묶음 중 모델을 부르는 비율(call_prob)만 시간이 걸리고, 나머지는 거의 바로(nocall 초) 끝난다. 모델을 부르는 패스의 시간은
    로그정규분포로 놓고 평균(mean)과 95번째 백분위수(p95)에 맞춘다. 묶음 k개를 합친 패스는 하나라도 부르면 모델을 부른다
    (1 - (1 - call_prob)^k). 합쳐도 패스 시간은 같다고 본다(합치면 입력이 커져 더 느려질 수 있으니 --scale 로 키워 본다).
  - 기본값은 SCRUM-157 의 병렬 측정(19분, 묶음 56개 중 호출 있는 묶음 36개의 평균 32.8초, p95 55.9초)이다.
    같은 방식으로 병렬 전 측정(평균 56.5초, p95 100초, 전체 2,032초)을 맞춰 보면 종료 후 약 900초가 나와 실측과 비슷하다.
  - 종료 신호는 마지막 묶음 뒤에 들어가고, 앞의 묶음을 다 처리한 뒤 종료 패스(end 배수 x 패스 시간)로 처리한다.
한계: 실제 호출 실패, 재시도, 429, 안건 수에 따른 시간 차이는 없다. 어림값이다.

사용
  python -m server.sim_queue                                  # 기본: 평균 32.8초, p95 55.9초 (SCRUM-157 측정)
  python -m server.sim_queue --mean 40 --p95 70 --scale 1.3   # 느린 상황을 가정
"""
import argparse
import math
import random
import statistics


def lognormal_params(mean, p95):
    """평균과 p95 에 맞는 로그정규분포의 (mu, sigma)."""
    r = math.log(p95 / mean)
    d = 1.645 ** 2 - 2 * r
    if d < 0:
        raise SystemExit("p95 가 평균보다 너무 커서 맞출 수 없습니다")
    sigma = 1.645 - math.sqrt(d)
    return math.log(mean) - sigma ** 2 / 2, sigma


def one_run(n_batches, period, coalesce, draw, end_mult, rng, call_prob=1.0, nocall=1.0):
    arrivals = [period * (k + 1) for k in range(n_batches)]
    t_end = arrivals[-1]
    i, t = 0, 0.0                              # i: 처리한 묶음 수, t: 일꾼이 한가해지는 시각
    max_queued = 0
    while i < n_batches:
        t = max(t, arrivals[i])                # 대기열이 비어 있으면 다음 묶음이 올 때까지 기다린다
        ready = sum(1 for a in arrivals[i:] if a <= t)
        max_queued = max(max_queued, ready)
        take = min(coalesce, ready)
        has_call = rng.random() < 1 - (1 - call_prob) ** take
        t += draw(rng) if has_call else nocall
        i += take
    t = max(t, t_end)
    t += draw(rng) * end_mult                  # 종료 패스
    return t - t_end, max_queued


def main():
    ap = argparse.ArgumentParser(description="종료 처리 시간 계산기 (비용 0)")
    ap.add_argument("--mean", type=float, default=32.8, help="패스 평균 시간(초). 기본은 SCRUM-157 의 병렬 측정값")
    ap.add_argument("--p95", type=float, default=55.9, help="패스 95번째 백분위수(초)")
    ap.add_argument("--scale", type=float, default=1.0, help="패스 시간에 곱할 값 (합친 입력이 커지거나 API 가 느려진 상황)")
    ap.add_argument("--period", type=float, default=20.0, help="묶음 간격(초)")
    ap.add_argument("--call-prob", type=float, default=36 / 56, help="묶음 하나가 모델 호출을 일으킬 확률 (병렬 측정: 56개 중 36개)")
    ap.add_argument("--nocall", type=float, default=1.0, help="모델을 부르지 않는 패스 시간(초). 가정값(미측정)")
    ap.add_argument("--end", type=float, default=1.0, help="종료 패스 시간 = 패스 시간 x 이 값")
    ap.add_argument("--minutes", default="5,10,15,20,30", help="회의 길이(분), 쉼표로 여러 개")
    ap.add_argument("--coalesce", default="1,4,8,100", help="묶음 합치기 상한, 쉼표로 여러 개 (1 = 합치지 않음, 100 = 사실상 제한 없음)")
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--limit", type=float, default=600.0, help="minutes-service 의 재료 대기 한도(초)")
    a = ap.parse_args()

    mu, sigma = lognormal_params(a.mean, a.p95)
    draw = lambda rng: rng.lognormvariate(mu, sigma) * a.scale
    print(f"패스 시간: 평균 {a.mean * a.scale:.1f}초, p95 {a.p95 * a.scale:.1f}초 (로그정규), 묶음 간격 {a.period:g}초, 시행 {a.trials}번")
    print("종료 신호 -> 최종 보드 (중앙값 / p95 / 600초 초과 비율), 재생 중 최대 대기 묶음 수 (중앙값)\n")
    coal = [int(x) for x in a.coalesce.split(",")]
    print(f"{'회의':>6} | " + " | ".join(f"합치기 {c:>3}".rjust(30) for c in coal))
    for m in [float(x) for x in a.minutes.split(",")]:
        n = int(m * 60 / a.period)
        cells = []
        for c in coal:
            rng = random.Random(1234)
            res = [one_run(n, a.period, c, draw, a.end, rng, a.call_prob, a.nocall) for _ in range(a.trials)]
            ts = sorted(r[0] for r in res)
            over = sum(1 for t in ts if t > a.limit) / len(ts)
            q = statistics.median(r[1] for r in res)
            cells.append(f"{ts[len(ts) // 2]:6.0f}s / {ts[int(len(ts) * 0.95)]:6.0f}s / {over:4.0%} / q{q:>3.0f}")
        print(f"{m:>5g}분 | " + " | ".join(x.rjust(30) for x in cells))


if __name__ == "__main__":
    main()
