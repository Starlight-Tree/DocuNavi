import statistics


def summarize(samples_ns: list[int]) -> dict:
    """ナノ秒の標本を、マイクロ秒の p50 / p95 / p99 / 平均 / 件数にまとめる。"""
    xs = sorted(samples_ns)
    n = len(xs)

    def pct(p: float) -> float:
        return round(xs[min(n - 1, int(n * p))] / 1000, 1)

    return {
        "n": n,
        "p50_us": pct(0.50),
        "p95_us": pct(0.95),
        "p99_us": pct(0.99),
        "mean_us": round(statistics.fmean(xs) / 1000, 1),
    }
