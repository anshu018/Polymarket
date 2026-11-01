"""
risk/fill_simulator.py — A5: book-aware paper fill simulation (List A.md Step 5).

Pure Python. No I/O, no LLM, no network: book snapshots are passed in by the caller
(coordinator/pipeline.py Step D, copytrade/executor.py Class A, and the
execution/paper_fills.py maker monitor all read the live CLOB book). Deterministic,
under 1ms, independently unit-testable.

Why this exists: paper mode previously assumed idealized fills at the decision-time
price. That inflates every paper metric — the honest gate (Step 5) must judge on
fills the book would actually have given:

    taker  : cross the spread — buy YES at the ask, buy NO at 1 − bid;
             rejected when visible depth cannot carry the size.
    maker  : rest at the touch on our side — bid for YES at best_bid, bid for NO
             at 1 − best_ask; filled only when the book trades THROUGH the resting
             level inside the wait window (the monitor checks fresh snapshots);
             otherwise the Step 1 fallback rule applies (taker if still
             net-positive, else cancel).

Line conventions (matches cost_model.compute_cost_breakdown):
    - `decision_price` and all YES-line prices are probabilities in [0, 1].
    - `fill_price` / `maker_resting_price` are on the TRADED token's line: the YES
      line for YES orders, the NO line (1 − YES price) for NO orders — the same
      line open_positions.entry_price/exit_price accounting uses.
    - Slippage is signed so that POSITIVE = paid worse than the decision reference:
      fill − reference on the traded token's line, where the reference is the YES
      mid for YES buys and 1 − the YES mid for NO buys.
    - Sells (List-B.md Step 5 — B5) reverse the direction: a taker exit sells
      INTO the book — selling YES realizes the YES bid, selling NO realizes the
      NO bid (1 − the YES ask), the mirror of the NO-entry convention. Exit
      slippage stays POSITIVE = adverse, which for a sell means RECEIVING less
      than the own-side mark: mark − fill on the traded token's line.
    - Fail-closed: an invalid/crossed book yields None (caller must not fill).
"""

from typing import Optional

from risk.cost_model import BookSnapshot, FeeConfig, net_edge

_SIDES: frozenset[str] = frozenset({"YES", "NO"})


def _book_ok(book: BookSnapshot) -> bool:
    """True when the snapshot is a usable, non-crossed YES-line book."""
    return 0.0 < book.best_bid <= book.best_ask < 1.0


def _side_ok(side: str) -> bool:
    """True when `side` is a known trade direction."""
    return side in _SIDES


# ── Taker fills ───────────────────────────────────────────────────────────────

def taker_fill_price(book: BookSnapshot, side: str) -> Optional[float]:
    """
    Price a taker (crossing) fill pays per share, on the traded token's line.

    Buying YES lifts the YES ask; buying NO lifts the NO ask, which mirrors the
    YES book at 1 − best_bid (same convention as cost_model's taker exec price).

    Args:
        book: Live CLOB book snapshot (YES line).
        side: Trade direction ("YES" or "NO").

    Returns:
        Fill price per share in (0, 1), or None when the book or side is invalid
        (fail-closed: the caller must not fill).
    """
    if not _side_ok(side) or not _book_ok(book):
        return None
    if side == "YES":
        return book.best_ask
    return 1.0 - book.best_bid


def taker_depth_ok(book: BookSnapshot, size_usd: float) -> bool:
    """
    Whether visible book depth can carry a taker order of `size_usd`.

    Mirrors cost_model.expected_slippage_units' feasibility rule: a non-positive
    size vacuously fits; a non-positive depth can fill nothing; a size above the
    (conservative, thinner-side) depth is rejected rather than optimistically
    averaged in.

    Args:
        book:      Live CLOB book snapshot.
        size_usd:  Order size in USDC (<= 0 = nothing to fill → True).

    Returns:
        True when the taker order is fillable within visible depth.
    """
    if size_usd <= 0.0:
        return True
    if book.depth_usd <= 0.0:
        return False
    return size_usd <= book.depth_usd


# ── Maker fills ───────────────────────────────────────────────────────────────

def maker_resting_price(book: BookSnapshot, side: str) -> Optional[float]:
    """
    Price a maker order rests at per share, on the traded token's line.

    The maker joins the touch on their side of the book: a YES buy rests at the
    YES best bid; a NO buy rests at the NO best bid, which mirrors the YES book
    at 1 − best_ask. Resting inside the spread (improving the touch) would fill
    differently; joining the touch is the conservative queue model.

    Args:
        book: Live CLOB book snapshot (YES line).
        side: Trade direction ("YES" or "NO").

    Returns:
        Resting price per share in (0, 1), or None when the book or side is
        invalid (fail-closed).
    """
    if not _side_ok(side) or not _book_ok(book):
        return None
    if side == "YES":
        return book.best_bid
    return 1.0 - book.best_ask


def maker_is_filled(current_book: BookSnapshot, side: str, resting_price: float) -> bool:
    """
    Whether a resting maker order would have filled, given a fresh book snapshot.

    "Price trades through the level" (List A.md Step 5): with only book snapshots
    available, the observable proxy for our bid being hit is the opposite touch
    reaching our level —

        YES buy  at level L: filled when the YES ask trades down to L
                              (a seller crossed to us):  best_ask <= L.
        NO buy   at level L: L mirrors YES at 1 − L, so filled when the YES bid
                              rises through 1 − L:      best_bid >= 1 − L.

    This is deliberately conservative in one direction and honest in the other:
    it never declares a fill the book cannot justify, and it credits a fill only
    when the market actually came to our price.

    Args:
        current_book:  Fresh CLOB book snapshot (YES line).
        side:          Trade direction ("YES" or "NO").
        resting_price: The order's resting price from maker_resting_price().

    Returns:
        True when the book shows a fill at the resting level.

    Raises:
        ValueError: If resting_price is outside (0, 1) — a corrupt level must
                    never silently read as "no fill".
    """
    if not 0.0 < resting_price < 1.0:
        raise ValueError(f"resting_price must be in (0, 1), got {resting_price}")
    if not _side_ok(side) or not _book_ok(current_book):
        return False
    if side == "YES":
        return current_book.best_ask <= resting_price
    return current_book.best_bid >= 1.0 - resting_price


def maker_fallback_net_edge(
    p_model: float,
    current_book: BookSnapshot,
    side: str,
    fee_cfg: FeeConfig,
) -> float:
    """
    Net edge of abandoning the maker queue and taking at the CURRENT book.

    The Step 1 fallback rule: an unfilled maker order falls back to taker only
    if still net-positive as taker. This recomputes the signed taker net edge
    against the fresh book (fresh mid as the gross-edge reference — the market
    has moved since the entry evaluation, so the old breakdown is stale).

    Args:
        p_model:      YES-line model probability (unchanged since entry).
        current_book: Fresh CLOB book snapshot (YES line).
        side:         Trade direction ("YES" or "NO").
        fee_cfg:      Fee parameters for this signal.

    Returns:
        Signed taker net edge in probability units; −1.0 on invalid inputs
        (fail-closed: the gate then never falls back).
    """
    if not (0.0 <= p_model <= 1.0) or not _side_ok(side) or not _book_ok(current_book):
        return -1.0
    return net_edge(
        p_model=p_model,
        price=current_book.mid,
        side=side,
        order_type="taker",
        book=current_book,
        fee_cfg=fee_cfg,
        size_usd=0.0,
    )


# ── Taker exits (List-B.md Step 5 — B5) ───────────────────────────────────────

def taker_exit_price(book: BookSnapshot, side: str) -> Optional[float]:
    """
    Price a taker SELL realizes per share, on the traded token's line.

    Selling YES hits the YES bid (the buyer's side of the book); selling NO
    hits the NO bid, which mirrors the YES book at 1 − best_ask — the exact
    mirror of the NO-entry convention (buying NO pays 1 − best_bid).

    Args:
        book: Live CLOB book snapshot (YES line).
        side: Position direction being exited ("YES" or "NO").

    Returns:
        Exit price per share in (0, 1), or None when the book or side is
        invalid (fail-closed: the caller must not sell).
    """
    if not _side_ok(side) or not _book_ok(book):
        return None
    if side == "YES":
        return book.best_bid
    return 1.0 - book.best_ask


def taker_exit_depth_ok(book: BookSnapshot, size_shares: float, side: str) -> bool:
    """
    Whether visible book depth can absorb a taker sell of `size_shares`.

    Mirrors the entry-side rule (taker_depth_ok) with sell semantics: the
    shares are converted to USDC notional AT THE EXIT PRICE the book would
    actually give (a sell's proceeds, not its cost) and compared against the
    conservative thinner-side depth. An unpriceable book cannot carry the
    sell (fail-closed).

    Args:
        book:         Live CLOB book snapshot (YES line).
        size_shares:  Shares to sell (<= 0 = nothing to sell → True).
        side:         Position direction being exited ("YES" or "NO").

    Returns:
        True when the sell is fillable within visible depth.
    """
    if size_shares <= 0.0:
        return True
    exit_price = taker_exit_price(book, side)
    if exit_price is None:
        return False
    if book.depth_usd <= 0.0:
        return False
    return size_shares * exit_price <= book.depth_usd


# ── Slippage measurement ──────────────────────────────────────────────────────

def signed_slippage_units(
    fill_price: float,
    decision_price: float,
    side: str,
) -> Optional[float]:
    """
    Signed slippage of a fill vs the decision-time reference, in probability units.

    `decision_price` is the YES-line reference the entry math used
    (open_positions.market_price_at_entry — the book mid). POSITIVE = adverse
    (the fill paid more than the traded token's reference price: the YES ask
    above the YES mid, or the NO ask above 1 − the YES mid); negative = price
    improved. This is the per-trade observation that accumulates into the
    empirical slippage curve replacing the conservative full_spread × multiple
    default.

    Args:
        fill_price:     Actual fill price on the traded token's line (0, 1).
        decision_price: YES-line decision reference in [0, 1].
        side:           Trade direction ("YES" or "NO").

    Returns:
        Signed slippage in probability units, or None on invalid inputs.
    """
    if not _side_ok(side):
        return None
    if not (0.0 <= decision_price <= 1.0):
        return None
    if not 0.0 < fill_price < 1.0:
        return None
    reference = decision_price if side == "YES" else 1.0 - decision_price
    return fill_price - reference


def signed_exit_slippage(
    fill_price: float,
    mark_price: float,
    side: str,
) -> Optional[float]:
    """
    Signed slippage of an EXIT fill vs the position's own-side mark.

    `mark_price` is the own-side reference the exit rule triggered on (the
    traded token's mid: the YES mid for a YES position, 1 − the YES mid for a
    NO position). For a sell, POSITIVE = ADVERSE: the fill realized LESS than
    the mark (sold into the bid below the mid) — `mark − fill` on the traded
    token's line; negative = the sell price-improved over the mark. This is
    the exit-side observation that pairs with signed_slippage_units (entries)
    so the empirical slippage curve covers both sides of the round trip.

    Args:
        fill_price: Actual exit fill price on the traded token's line (0, 1).
        mark_price: Own-side mark in [0, 1] the rule triggered on.
        side:       Position direction being exited ("YES" or "NO").

    Returns:
        Signed slippage in probability units (positive = adverse), or None on
        invalid inputs.
    """
    if not _side_ok(side):
        return None
    if not (0.0 <= mark_price <= 1.0):
        return None
    if not 0.0 < fill_price < 1.0:
        return None
    return mark_price - fill_price
