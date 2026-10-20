"""prices - per-model API prices, and the token weights the savings math uses.

$ per million tokens, first-party API rates (2026-10). Cache writes are 1.25x input (5-minute
TTL) or 2x input (1-hour TTL, what Claude Code uses). A 2026-10 telemetry check on Opus 5.5
matched: input 3.97, output 18.9, cache read 0.197, 1h cache write 7.99.

Cache reads are NOT a fixed 0.1x of input: 0.025x on Fable 5.1, 0.05x on Opus 5.5, 0.1x on
Sonnet. That ratio decides how much a prune saves per later call, so the weights are per model.
Subscription plans don't bill per token; there the dollars are what the same usage would cost
on the API, a way to compare models, not a bill.
"""
import re

#        input  output  cache read
PRICES = {
    "fable-5-1": (10.0, 50.0, 0.25),
    "fable":    (10.0, 50.0, 1.00),   # Fable 5
    "mythos-5-1": (10.0, 50.0, 0.25),
    "mythos":   (10.0, 50.0, 1.00),
    "opus-5-5": (4.0, 20.0, 0.20),
    "opus":     (5.0, 25.0, 0.50),    # Opus 5 and 4.x
    "sonnet-5-5": (2.0, 10.0, 0.20),
    "sonnet-5": (2.0, 10.0, 0.20),
    "sonnet":   (3.0, 15.0, 0.30),    # Sonnet 4.x
    "haiku-5-5": (0.10, 0.50, 0.01),
    "haiku":    (1.0, 5.0, 0.10),     # Haiku 4.5
}
DEFAULT = "opus-5-5"
W5M, W1H = 1.25, 2.0


def family(model):
    """'claude-fable-5-1' -> 'fable', 'claude-opus-5-5' -> 'opus-5-5', unknown -> DEFAULT."""
    m = (model or "").lower()
    for key in sorted(PRICES, key=len, reverse=True):
        base = key.split("-")[0]
        ver = key[len(base):]
        if re.search(rf"{base}{re.escape(ver)}(?![0-9])", m) and (ver or base in m):
            return key
    return DEFAULT


def label(model):
    """'claude-fable-5-1' -> 'Fable 5.1'."""
    m = re.match(r"claude-([a-z]+)-([0-9]+)(?:-([0-9]+))?", (model or "").lower())
    if not m:
        return model or "?"
    return f"{m.group(1).title()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) and len(m.group(3)) < 3 else "")


def weights(model):
    """(input, cache read, cache write 1h, output) relative to that model's input price."""
    i, o, r = PRICES[family(model)]
    return 1.0, r / i, W1H, o / i


def dollars(model, u):
    """API-price dollars for one usage record (input, output, cache read, 5m/1h writes)."""
    i, o, r = PRICES[family(model)]
    cw = u.get("cache_creation") or {}
    w5, w1 = cw.get("ephemeral_5m_input_tokens"), cw.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = 0, u.get("cache_creation_input_tokens") or 0
    return ((u.get("input_tokens") or 0) * i + (u.get("output_tokens") or 0) * o
            + (u.get("cache_read_input_tokens") or 0) * r + (w5 or 0) * i * W5M + (w1 or 0) * i * W1H) / 1e6


if __name__ == "__main__":
    for m in ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5-5",
              "claude-sonnet-4-6", "claude-haiku-5-5", "claude-haiku-4-5", "x"):
        print(m, family(m), label(m), [round(w, 3) for w in weights(m)])
