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

FAIL_FAST_HTTP_CODES = [401, 402, 403, 429]

# News Analyst
MODEL_NEWS_ANALYST = "Qwen/Qwen3-32B"           # SiliconFlow exact slug (case-sensitive)
MODEL_NEWS_ANALYST_FALLBACK = "meta/llama-3.3-70b-instruct"  # NVIDIA NIM
MODEL_NEWS_ANALYST_FALLBACK_2 = "gemini-2.0-flash"           # Google Gemini (free tier)


# Contract Parser
MODEL_CONTRACT_PARSER = "meta-llama/llama-3.3-70b-instruct:free"
MODEL_CONTRACT_PARSER_FALLBACK_DS = "deepseek-chat"
MODEL_CONTRACT_PARSER_FALLBACK_NV = "meta/llama-3.1-8b-instruct"
MODEL_CONTRACT_PARSER_FALLBACK_OR = "qwen/qwen3-next-80b-a3b-instruct:free"


# Trade Decision
MODEL_TRADE_DECISION = "qwen/qwen3-235b-a22b"
MODEL_TRADE_DECISION_FALLBACK = "qwen/qwen3-next-80b-a3b-instruct:free"

# Coordinator
MODEL_COORDINATOR = "meta/llama-3.3-70b-instruct"

# LLM HARD LIMITS
MAX_TOKENS_TRADE_DECISION = 900
THINKING_BUDGET_TRADE_DECISION = 600
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
MIN_MARKET_LIQUIDITY_USDC = 5000
AUTO_EXIT_LIQUIDITY_FLOOR_USDC = 3000
DAILY_DRAWDOWN_HALT_PCT = 0.08
WEEKLY_DRAWDOWN_HALT_PCT = 0.15
MONTHLY_DRAWDOWN_SHUTDOWN_PCT = 0.25
HEALTH_SCORE_DEFENSIVE_THRESHOLD = 65
HEALTH_SCORE_HALT_THRESHOLD = 40
RESOLUTION_CACHE_TTL_HOURS = 24
RSS_POLL_INTERVAL_SECONDS = 10
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # Optional: free tier fallback (1M tokens/day)

_required_vars = [
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "OPENROUTER_API_KEY",
    "NVIDIA_API_KEY",
    "DEEPSEEK_API_KEY",
    "POLYMARKET_PRIVATE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SILICONFLOW_API_KEY"
]

_PLACEHOLDER_VALUES = {"placeholder", "your_polygon_wallet_private_key_here", "", None}
_missing_vars = [
    var for var in _required_vars
    if globals().get(var) in _PLACEHOLDER_VALUES
]

if _missing_vars:
    raise ValueError(f"CRITICAL: Missing required environment variables: {', '.join(_missing_vars)}")
