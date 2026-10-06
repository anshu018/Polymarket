"""
risk/cost_model.py — A2: deterministic entry-cost model and net-edge gate (List A.md Step 1).

Pure Python. No I/O, no LLM, no network: the live CLOB book and fee config are passed in
by the caller (coordinator/pipeline.py fetches the book). Every function is deterministic,
under 1ms, and independently unit-testable; all thresholds come from config.

Why this exists: the pre-Step-1 gate compared |p_model − market_price| against a 7¢ gross
threshold — symmetric, direction-blind and cost-blind. At 2¢ a "7¢ edge" can be fiction:
the taker fee alone is ≈3.9% of trade value, before spread and slippage. This module
prices the full cost of entering and gates on the SIGNED net edge:

    net_edge = gross_edge − fee_units − spread_units − slippage_units − haircut_units

Fee schedule (VERIFIED 2026-10-06, docs.polymarket.com/polymarket-learn/trading/fees):
    fee = shares × feeRate × p × (1 − p); makers are never charged (15-25% rebates);
    taker feeRate is per category (politics 0.04, crypto 0.07, ...). Recorded in
    List A.md Decision Log D-09. Schedule changes are updated in config.py only.

Conventions (inherited from strategies/estimator.py):
    - YES line: p_model, book prices and the `price` reference are probabilities in [0, 1].
    - All cost units are decimal probability ("cents"): 0.01 = 1¢ per share.
    - `price` is the YES-line reference mid used for the gross-edge term; the pipeline
      passes the fresh book's mid so gross edge and costs come from one snapshot.
    - Fail-closed: any invalid input (NaN, crossed book, unfillable size) yields a
      net_edge of −1.0, which the gate always blocks.
"""

from dataclasses import dataclass

import config

_SIDES: frozenset[str] = frozenset({"YES", "NO"})
_ORDER_TYPES: frozenset[str] = frozenset({"maker", "taker"})

# Fail-closed net edge returned for invalid inputs — far below any MIN_NET_EDGE_CENTS.
_BLOCKED_NET_EDGE: float = -1.0


# ── Value types ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BookSnapshot:
    """
    One CLOB order-book snapshot, reduced to what the cost model needs.

    Attributes:
        best_bid:  Best bid price on the YES token (0, 1).
        best_ask:  Best ask price on the YES token (0, 1), >= best_bid.
        depth_usd: Conservative fill depth in USDC — the thinner side of the book,
                   summed over the top config.BOOK_DEPTH_TOP_LEVELS levels.
    """
    best_bid: float
    best_ask: float
    depth_usd: float

    @property
    def mid(self) -> float:
        """Midpoint price on the YES line."""
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def full_spread(self) -> float:
        """Full bid-ask spread in probability units."""
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class FeeConfig:
    """
    Fee/cost parameters for one entry evaluation.

    Built by `fee_config_for(category)` from config; passed into the pure math
    functions so they stay free of config lookups and trivially testable.
    """
    taker_rate: float
    maker_rate: float
    maker_haircut: float
    slippage_multiple: float


@dataclass(frozen=True)
class CostBreakdown:
    """
    Full cost decomposition for one entry evaluation (all probability units).

    Logged verbatim on every entry evaluation ([OBSERVABILITY][NET_EDGE]).
    net_edge = gross_edge − fee_units − spread_units − slippage_units − haircut_units.
    """
    gross_edge: float
    fee_units: float
    spread_units: float
    slippage_units: float
    haircut_units: float
    net_edge: float


def _blocked_breakdown() -> CostBreakdown:
    """Fail-closed breakdown for invalid inputs — the gate always blocks it."""
    return CostBreakdown(
        gross_edge=0.0,
        fee_units=0.0,
        spread_units=0.0,
        slippage_units=0.0,
        haircut_units=0.0,
        net_edge=_BLOCKED_NET_EDGE,
    )


# ── Config plumbing ───────────────────────────────────────────────────────────

def fee_config_for(category: str) -> FeeConfig:
    """
    Build the FeeConfig for one signal from config, using the verified per-category
    taker feeRate when known and the default rate otherwise (conservative "Other").

    Args:
        category: Event category from the News Analyst (case-insensitive).

    Returns:
        FeeConfig for the signal.
    """
    taker_rate = config.TAKER_FEE_RATE_BY_CATEGORY.get(
        (category or "").strip().lower(),
        config.TAKER_FEE_RATE,
    )
    return FeeConfig(
        taker_rate=taker_rate,
        maker_rate=config.MAKER_FEE_RATE,
        maker_haircut=config.MAKER_FILL_HAIRCUT,
        slippage_multiple=config.SLIPPAGE_SPREAD_MULTIPLE,
    )


# ── Individual cost components ────────────────────────────────────────────────

def taker_fee_units(exec_price: float, fee_cfg: FeeConfig) -> float:
    """
    Taker fee in probability units for one share executed at `exec_price`.

    Verified formula: fee = feeRate × p × (1 − p) per share (symmetric around 50¢).
    At price 0.02 with the politics rate 0.04 this is ≈3.9% of trade value.

    Args:
        exec_price: Execution price of the traded token in (0, 1).
        fee_cfg:    Fee parameters for this signal.

    Returns:
        Fee per share in probability units (cents).

    Raises:
        ValueError: If exec_price is outside [0, 1].
    """
    if not 0.0 <= exec_price <= 1.0:
        raise ValueError(f"exec_price must be in [0, 1], got {exec_price}")
    return fee_cfg.taker_rate * exec_price * (1.0 - exec_price)


def maker_fee_units(exec_price: float, fee_cfg: FeeConfig) -> float:
    """
    Maker fee in probability units for one share rested at `exec_price`.

    Per the verified schedule makers are never charged (rate 0.0 in config);
    the function exists so a future maker-fee change is a config edit only.

    Args:
        exec_price: Resting price of the traded token in [0, 1].
        fee_cfg:    Fee parameters for this signal.

    Returns:
        Fee per share in probability units (cents).

    Raises:
        ValueError: If exec_price is outside [0, 1].
    """
    if not 0.0 <= exec_price <= 1.0:
        raise ValueError(f"exec_price must be in [0, 1], got {exec_price}")
    return fee_cfg.maker_rate * exec_price * (1.0 - exec_price)


def half_spread_units(best_bid: float, best_ask: float) -> float:
    """
    Half-spread cost in probability units: the distance from the mid to the touch.

    A taker crossing from the mid reference pays (ask − mid) on YES buys and
    (mid − bid) on NO buys — both equal half the full spread.

    Args:
        best_bid: Best bid price in (0, 1).
        best_ask: Best ask price in (0, 1), >= best_bid.

    Returns:
        Half-spread in probability units (cents).
    """
    return (best_ask - best_bid) / 2.0


def expected_slippage_units(
    size_usd: float,
    book_depth_usd: float,
    full_spread: float,
    fee_cfg: FeeConfig,
) -> float:
    """
    Expected slippage in probability units — conservative default until Step 5's
    empirical decision-vs-fill curve replaces it.

    Rules (fail-closed):
        - Empty/unknown book depth (<= 0)          → 1.0 (100¢: treat as unfillable).
        - Requested size exceeds visible depth     → 1.0 (the book cannot fill it).
        - Otherwise                                → full_spread × slippage_multiple.

    Args:
        size_usd:        Intended order size in USDC (0 = size unknown, skip the
                         depth-feasibility check).
        book_depth_usd:  Visible book depth in USDC at the touch side.
        full_spread:     Full bid-ask spread in probability units.
        fee_cfg:         Fee parameters for this signal.

    Returns:
        Expected slippage in probability units (cents).
    """
    if book_depth_usd <= 0.0:
        return 1.0
    if size_usd > book_depth_usd:
        return 1.0
    return full_spread * fee_cfg.slippage_multiple


def maker_fill_haircut_units(fee_cfg: FeeConfig) -> float:
    """
    Queue-risk haircut in probability units charged against maker entries.

    A resting order only fills when price trades through it — adverse selection
    the taker never faces. Conservative flat value from config
    (MAKER_FILL_HAIRCUT); to be refined with Step 5 fill data.

    Args:
        fee_cfg: Fee parameters for this signal.

    Returns:
        Haircut in probability units (cents).
    """
    return fee_cfg.maker_haircut


# ── Net edge ──────────────────────────────────────────────────────────────────

def compute_cost_breakdown(
    p_model: float,
    price: float,
    side: str,
    order_type: str,
    book: BookSnapshot,
    fee_cfg: FeeConfig,
    size_usd: float = 0.0,
) -> CostBreakdown:
    """
    Full signed cost decomposition for one entry evaluation.

    Math on the YES line (`price` is the YES-line reference mid):
        taker YES: gross = p − price; spread = half_spread;
                   exec YES price = price + half_spread (the ask);
                   net = gross − spread − fee − slippage.
        taker NO:  gross = price − p; spread = half_spread;
                   NO token executed at 1 − (price − half_spread);
                   fee computed on that NO price; net = gross − spread − fee − slippage.
        maker:     rest at the reference mid: no spread, no slippage;
                   net = gross − maker fee − haircut.

    Fail-closed: any invalid input (p_model/side/order_type nonsense, crossed or
    out-of-range book, exec price outside (0, 1)) yields net_edge = −1.0, which
    `check_net_edge` always blocks. NaN inputs fail the range checks and block.

    Args:
        p_model:    YES-line model probability (from strategies.estimator).
        price:      YES-line reference mid for the gross-edge term.
        side:       Proposed trade direction ("YES" or "NO").
        order_type: "maker" or "taker" (from decide_order_type).
        book:       Live CLOB book snapshot.
        fee_cfg:    Fee parameters for this signal.
        size_usd:   Intended order size in USDC for the depth-feasibility check
                    (0 = unknown; the pipeline passes a worst-case bound because
                    Kelly sizing happens later).

    Returns:
        CostBreakdown with the signed net_edge.
    """
    if not (0.0 <= p_model <= 1.0):
        return _blocked_breakdown()
    if side not in _SIDES or order_type not in _ORDER_TYPES:
        return _blocked_breakdown()
    if not (0.0 < book.best_bid <= book.best_ask < 1.0):
        return _blocked_breakdown()

    half_spread = half_spread_units(book.best_bid, book.best_ask)
    bid_ref = price - half_spread
    ask_ref = price + half_spread

    if order_type == "taker":
        spread_units = half_spread
        slippage_units = expected_slippage_units(
            size_usd=size_usd,
            book_depth_usd=book.depth_usd,
            full_spread=book.full_spread,
            fee_cfg=fee_cfg,
        )
        haircut_units = 0.0
        if side == "YES":
            gross_edge = p_model - price
            exec_price = ask_ref
        else:
            gross_edge = price - p_model
            exec_price = 1.0 - bid_ref  # NO token bought at the complement of the bid
        if not 0.0 < exec_price < 1.0:
            return _blocked_breakdown()
        fee_units = taker_fee_units(exec_price, fee_cfg)
    else:  # maker — rests at the reference mid
        spread_units = 0.0
        slippage_units = 0.0
        haircut_units = maker_fill_haircut_units(fee_cfg)
        gross_edge = p_model - price if side == "YES" else price - p_model
        exec_price = price
        fee_units = maker_fee_units(exec_price, fee_cfg)

    net_edge_value = (
        gross_edge - fee_units - spread_units - slippage_units - haircut_units
    )
    return CostBreakdown(
        gross_edge=gross_edge,
        fee_units=fee_units,
        spread_units=spread_units,
        slippage_units=slippage_units,
        haircut_units=haircut_units,
        net_edge=net_edge_value,
    )


def net_edge(
    p_model: float,
    price: float,
    side: str,
    order_type: str,
    book: BookSnapshot,
    fee_cfg: FeeConfig,
    size_usd: float = 0.0,
) -> float:
    """
    Signed net edge for one entry evaluation (List A.md Step 1 contract).

    YES buy: p − price − costs; NO buy: (1 − p) − (1 − price) − costs — equivalently
    `price − p − costs` on the YES line — where costs are fees, spread, slippage and
    the maker haircut as applicable. Fail-closed: −1.0 on any invalid input.

    Args:
        p_model:    YES-line model probability.
        price:      YES-line reference mid for the gross-edge term.
        side:       "YES" or "NO".
        order_type: "maker" or "taker".
        book:       Live CLOB book snapshot.
        fee_cfg:    Fee parameters for this signal.
        size_usd:   Intended order size in USDC (0 = unknown).

    Returns:
        Net edge in probability units (cents); −1.0 when inputs are invalid.
    """
    return compute_cost_breakdown(
        p_model=p_model,
        price=price,
        side=side,
        order_type=order_type,
        book=book,
        fee_cfg=fee_cfg,
        size_usd=size_usd,
    ).net_edge


# ── Gate + order-type rule ────────────────────────────────────────────────────

def check_net_edge(net_edge_value: float, price_mid: float, order_type: str) -> str:
    """
    Gate one entry on net edge and (for takers) the tradeable price band.

    Requires net_edge strictly ABOVE config.MIN_NET_EDGE_CENTS (a breakeven trade
    is a loss after variance) and, for taker orders only, the reference mid inside
    config.TRADEABLE_PRICE_BAND (extreme prices carry structural adverse selection;
    maker orders set their own level and bypass the band).

    NaN net_edge fails closed (comparison is False → BLOCK).

    Args:
        net_edge_value: Signed net edge from net_edge()/compute_cost_breakdown().
        price_mid:      YES-line reference mid for the band check.
        order_type:     "maker" or "taker".

    Returns:
        'ALLOW' | 'BLOCK_NET_EDGE' | 'BLOCK_PRICE_BAND'.
    """
    if not net_edge_value > config.MIN_NET_EDGE_CENTS:
        return "BLOCK_NET_EDGE"
    band_low, band_high = config.TRADEABLE_PRICE_BAND
    if order_type == "taker" and not (band_low <= price_mid <= band_high):
        return "BLOCK_PRICE_BAND"
    return "ALLOW"


def decide_order_type(strategy: str) -> str:
    """
    Maker/taker decision rule per strategy (config map — List A.md Step 1).

    velocity / copy_edge_class_a → taker: speed is the edge; a queue wait exceeds
    the signal half-life. recalibration / resolution / copy_edge_class_b → maker:
    the edge persists over config.MAKER_FALLBACK_SECONDS, so paying the spread and
    taker fee is burning edge. Unknown strategies → taker (fail-safe: the gate then
    charges the worst-case costs and blocks more readily).

    Args:
        strategy: Strategy key for this signal.

    Returns:
        "maker" or "taker".
    """
    if strategy in config.MAKER_ORDER_STRATEGIES:
        return "maker"
    return "taker"
