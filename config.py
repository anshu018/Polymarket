import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
_env_file = ".env" if os.environ.get("ENVIRONMENT") == "production" else ".env.test"
load_dotenv(dotenv_path=_env_file, override=False)

# LLM MODEL IDENTIFIERS AND PROVIDERS
PROVIDER_OPENROUTER = "https://openrouter.ai/api/v1"
PROVIDER_NVIDIA = "https://integrate.api.nvidia.com/v1"
PROVIDER_DEEPSEEK = "https://api.deepseek.com/v1"
PROVIDER_SILICONFLOW = "https://api.siliconflow.com/v1"
PROVIDER_TOKENROUTER = "https://api.tokenrouter.com"

FAIL_FAST_HTTP_CODES = [401, 402, 403, 429]

# News Analyst
MODEL_NEWS_ANALYST = "typesafe/jev-1.13"                      # Primary via TokenRouter / OpenRouter
MODEL_NEWS_ANALYST_FALLBACK_SF = "Qwen/Qwen3-32B"            # SiliconFlow fallback
MODEL_NEWS_ANALYST_FALLBACK = "meta/llama-3.3-70b-instruct"  # NVIDIA NIM
MODEL_NEWS_ANALYST_FALLBACK_2 = "gemini-2.0-flash"           # Google Gemini (free tier)
MODEL_NEWS_ANALYST_FALLBACK_TR = "qwen/qwen3.5-flash"        # TokenRouter generative fallback (thinking always OFF)


# Contract Parser
MODEL_CONTRACT_PARSER = "qwen/qwen3.8-flash"                  # Primary via TokenRouter
MODEL_CONTRACT_PARSER_FALLBACK_TR = "qwen/qwen3.5-flash"     # TokenRouter fallback (Nemotron was failing)
MODEL_CONTRACT_PARSER_FALLBACK_DS = "deepseek-chat"
MODEL_CONTRACT_PARSER_FALLBACK_NV = "meta/llama-3.1-8b-instruct"
MODEL_CONTRACT_PARSER_FALLBACK_OR = "qwen/qwen3-next-80b-a3b-instruct:free"


# Trade Decision
MODEL_TRADE_DECISION = "deepseek/deepseek-v4.1-flash"         # Primary via TokenRouter
MODEL_TRADE_DECISION_FALLBACK = "qwen/qwen3.5-flash"          # Fallback via TokenRouter
MODEL_TRADE_DECISION_FALLBACK_OR = "qwen/qwen3-235b-a22b"

# Coordinator
MODEL_COORDINATOR = "qwen/qwen3.5-flash"                      # Primary via TokenRouter

# LLM HARD LIMITS
MAX_TOKENS_TRADE_DECISION = 900
THINKING_BUDGET_TRADE_DECISION = 600          # For Qwen3-235B (coordinator/fallback), NOT DeepSeek
MAX_TOKENS_DEEPSEEK_TRADE = 300               # Hard cap for DeepSeek trade decision (structured prompt reasoning)
NEWS_ANALYST_TIMEOUT_SECONDS = 25  # Raised from 15: non-thinking Qwen3 needs ~2-6s; buffer for cold start

# TELEGRAM
TELEGRAM_TIMEOUT_SECONDS = 10
TELEGRAM_API_DOWN_ALERT_DELAY_SECONDS = 300

# STARTUP AND RECONCILIATION
RECONCILIATION_RETRY_INTERVAL_SECONDS = 60
POLYMARKET_API_TIMEOUT_SECONDS = 10

# SILICONFLOW HEALTH CHECK
SILICONFLOW_HEALTH_CHECK_INTERVAL_SECONDS = 300
SILICONFLOW_HEALTH_CHECK_LATENCY_THRESHOLD_SECONDS = 20

# POST-TRADE MONITORING
POSITION_MONITOR_INTERVAL_MINUTES = 15
PRICE_TARGET_EXIT_THRESHOLD_CENTS = 0.03
TIME_DECAY_EXIT_HOURS_REMAINING = 72
TIME_DECAY_POSITION_REDUCTION_PCT = 0.50

# STRATEGY PROBATION
STRATEGY_PROBATION_TRADE_COUNT = 20
STRATEGY_PROBATION_EDGE_THRESHOLD_CENTS = 0.04

# MEMORY SYSTEM
MEMORY_DECAY_STEP = 0.10
MEMORY_RETIREMENT_THRESHOLD = 0.30
MEMORY_RELEVANT_TRADES_DECAY_TRIGGER = 20
MEMORY_VALIDATION_WINDOW_DAYS = 90
MEMORY_MAX_LESSONS_PER_QUERY = 5

# PAPER TRADING GATES
PAPER_TRADING_MIN_WEEKS = 2
PAPER_TRADING_MIN_RESOLVED_TRADES = 20
BRIER_SCORE_THRESHOLD = 0.23

# POLYMARKET CONSTANTS
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
POLYGON_CHAIN_ID = 137
SUPABASE_TIMEOUT_SECONDS = 2
LLM_TIMEOUT_SECONDS = 18
FAIL_FAST_HTTP_CODES = [401, 402, 403, 429]  # 429 = rate limit, treat same as auth failure → failover
MIN_CONFIDENCE_THRESHOLD = 0.75
FAST_PATH_CONFIDENCE_THRESHOLD = 0.87
CONFIDENCE_CEILING = 0.88
MIN_EDGE_CENTS = 0.07
MAX_SINGLE_TRADE_PCT = 0.05
MAX_RESOLUTION_TRADE_PCT = 0.08
MAX_CATEGORY_EXPOSURE_PCT = 0.30
MAX_CORRELATED_EXPOSURE_PCT = 0.20
MAX_MARKET_TRADE_PCT = 0.08        # ceiling on cumulative (market_id, direction) exposure
REPEAT_ENTRY_PCT = 0.03            # max size of a 2nd entry = 3% of TOTAL PORTFOLIO
REPEAT_MIN_CONFIDENCE = 0.87       # min confidence for a 2nd entry (ceiling is 0.88)
MAX_MARKET_TRANCHES = 2            # 1st entry + 1 repeat. 3rd always blocked.
MIN_ADD_TICKET_USDC = 25.0         # floor for REPEAT attempts only — never for 1st entries
MIN_MARKET_LIQUIDITY_USDC = 5000
AUTO_EXIT_LIQUIDITY_FLOOR_USDC = 3000
DAILY_DRAWDOWN_HALT_PCT = 0.08
WEEKLY_DRAWDOWN_HALT_PCT = 0.15
MONTHLY_DRAWDOWN_SHUTDOWN_PCT = 0.25
MIN_CASH_RESERVE_PCT = 0.20
HEALTH_SCORE_DEFENSIVE_THRESHOLD = 65
HEALTH_SCORE_HALT_THRESHOLD = 40
RESOLUTION_CACHE_TTL_HOURS = 24
RSS_POLL_INTERVAL_SECONDS = 10
# Fallback time-to-resolution (hours) for the Trade Decision prompt when a market's
# end_date_iso is missing or unparseable. 720h = 30 days, conservative long horizon.
# Each fallback use is tagged with the "estimate:ttr_fallback" drop counter (List A.md Step 0).
DEFAULT_TTR_HOURS = 720

# ── NET-EDGE GATE & COST MODEL (List A.md Step 1 — A2) ───────────────────────
# Fee schedule VERIFIED 2026-10-06 from the official docs
# (https://docs.polymarket.com/polymarket-learn/trading/fees):
#   fee = shares × feeRate × p × (1 − p); makers are NEVER charged (they receive
#   15-25% rebates); the taker feeRate is per market category. Sanity check:
#   politics at 2¢ → 0.04 × 0.02 × 0.98 ≈ 3.9% of trade value, matching the
#   operator's reported figure (Decision Log D-07/D-09). Schedule changes must be
#   re-verified and updated here only — never hardcoded in modules.
TAKER_FEE_RATE = 0.05                   # default taker feeRate ("Other / General")
TAKER_FEE_RATE_BY_CATEGORY = {          # verified per-category taker feeRates
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}
MAKER_FEE_RATE = 0.0                    # docs: "Makers are never charged fees."
MAKER_FILL_HAIRCUT = 0.01               # queue-risk haircut (probability units) charged
                                        # against maker entries; conservative initial
                                        # value — refine with Step 5 decision-vs-fill data.
MIN_NET_EDGE_CENTS = 0.02               # entry requires net_edge strictly ABOVE 2¢
TRADEABLE_PRICE_BAND = (0.10, 0.90)     # taker entries must price inside the band;
                                        # maker orders bypass it (they set their level).
MAKER_FALLBACK_SECONDS = 20             # unfilled maker order falls back to taker after
                                        # this window if still net-positive as taker
                                        # (enforced by the execution layer, Phase 3).
SLIPPAGE_SPREAD_MULTIPLE = 1.2          # conservative default slippage = full_spread ×
                                        # this multiple; replaced by the empirical curve
                                        # from Step 5 decision-vs-fill logs.
BOOK_DEPTH_TOP_LEVELS = 10              # levels summed per side for book depth (USDC)
MAKER_ORDER_STRATEGIES = {"recalibration", "resolution", "copy_edge_class_b"}  # rest → taker

KELLY_FRACTION_VELOCITY = 0.15
KELLY_FRACTION_RECALIBRATION = 0.25
KELLY_FRACTION_CORRELATION = 0.25
KELLY_FRACTION_RESOLUTION = 0.35
PIPELINE_QUEUE_MAXSIZE = 100

# ── STRATEGY 5: COPY EDGE (CopyTrade) ────────────────────────────────────────
# PRD source: CopyTrade.md §7.1, §9.2
KELLY_FRACTION_COPY = 0.10              # 10% fractional Kelly for Class B sizing
COPY_CLASS_A_MAX_SIZE_USDC = 10.0      # Fixed hard cap for Class A (speed) trades
COPY_CLASS_B_MAX_SIZE_USDC = 50.0      # Max cap for Class B (macro) trades
COPY_CLASS_A_SLIPPAGE_THRESHOLD = 0.010  # 1.0 cent max slippage for Class A
COPY_CLASS_B_SLIPPAGE_THRESHOLD = 0.015  # 1.5 cent max slippage for Class B
COPY_MIN_MARKET_VOLUME_USD = 25000.0   # Minimum market volume to copy any trade
COPY_POLL_INTERVAL_SECONDS = 5         # How often to poll Gamma API per wallet
COPY_WALLET_RELOAD_INTERVAL_SECONDS = 300  # How often to reload tracked_wallets from DB
COPY_SIGNAL_QUEUE_MAXSIZE = 50         # Max in-flight unclassified signals
COPY_EXECUTION_QUEUE_MAXSIZE = 20      # Max in-flight signals per execution class
COPY_LIMIT_PRICE_BUFFER = 0.005        # +0.5 cents above tracker price for limit orders
GAMMA_API_TIMEOUT_SECONDS = 8          # HTTP timeout for Gamma API calls
SPACY_MODEL = "en_core_web_lg"            # Aligned with GEMINI.md (lg = higher NER accuracy)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
MIN_MARKET_VOLUME_USD = 500.0
MARKET_MATCH_THRESHOLD = 0.10  # 1 entity match out of 3 capped denominator = 0.33, well above this

MARKET_CACHE_REFRESH_INTERVAL_SECONDS = 300
MAX_SPREAD_THRESHOLD = 0.15
PAPER_TRADING_PORTFOLIO_USDC = float(os.environ.get("PAPER_TRADING_PORTFOLIO_USDC", "10000"))


ENVIRONMENT = os.environ.get(
    "ENVIRONMENT", "development"
)

PAPER_TRADING = os.environ.get(
    "PAPER_TRADING", "true"
).lower() == "true"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
POLYMARKET_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY")
TOKENROUTER_API_KEY = os.environ.get("TOKENROUTER_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # Optional: free tier fallback (1M tokens/day)

_required_vars = [
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "TOKENROUTER_API_KEY",
    "POLYMARKET_PRIVATE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

_PLACEHOLDER_VALUES = {"placeholder", "your_polygon_wallet_private_key_here", "", None}
_missing_vars = [
    var for var in _required_vars
    if globals().get(var) in _PLACEHOLDER_VALUES
]

if _missing_vars:
    raise ValueError(f"CRITICAL: Missing required environment variables: {', '.join(_missing_vars)}")
