_RATES = {
    "text-embedding-3-small": (0.02 / 1_000_000, 0.0),
    "gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
    "gpt-4o": (2.50 / 1_000_000, 10.0 / 1_000_000),
}


def estimate_cost(model, tokens_in, tokens_out=0):
    if model not in _RATES:
        raise ValueError(f"no cost rate for model: {model}")
    rate_in, rate_out = _RATES[model]
    return tokens_in * rate_in + tokens_out * rate_out
