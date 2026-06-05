import os
import logging
import config

logger = logging.getLogger(__name__)

DOMAIN_ALLOWLIST = [
    "FOMC", "Federal Reserve", "basis points", "bps", "rate cut", "rate hike", 
    "rate pause", "quantitative easing", "quantitative tightening", "QE", "QT", 
    "tapering", "taper", "pivot", "hawkish", "dovish", "Fed funds rate", 
    "overnight rate", "repo rate", "reverse repo", "IOER", "balance sheet", 
    "reserve requirement", "stress test", "forward guidance", "dot plot", 
    "beige book", "FOMC minutes",
    "CPI", "PCE", "NFP", "GDP", "inflation", "deflation", "recession", 
    "stagflation", "unemployment rate", "jobs report", "payrolls", 
    "trade deficit", "trade surplus", "current account", "fiscal deficit", 
    "national debt", "debt ceiling", "default", "credit rating", "downgrade", 
    "upgrade", "yield curve", "inversion", "spread", "Treasury yield", 
    "SOFR", "LIBOR",
    "circuit breaker", "halt trading", "flash crash", "market crash", "correction", 
    "bear market", "bull market", "volatility", "VIX", "short squeeze", 
    "margin call", "liquidation cascade", "dark pool", "high frequency trading", 
    "market maker", "liquidity crisis", "contagion", "systemic risk", "bailout", 
    "backstop",
    "merger", "acquisition", "takeover", "hostile bid", "IPO", "spin-off", 
    "bankruptcy", "Chapter 11", "Chapter 7", "restructuring", 
    "debt restructuring", "writedown", "impairment", "restatement", 
    "earnings miss", "earnings beat", "guidance cut", "guidance raise", 
    "dividend cut", "buyback", "secondary offering", "SEC filing", "8-K", 
    "10-Q", "10-K", "proxy statement", "shareholder vote", "activist investor", 
    "poison pill",
    "slip opinion", "certiorari", "cert granted", "cert denied", "injunction", 
    "stay", "preliminary injunction", "TRO", "temporary restraining order", 
    "summary judgment", "motion to dismiss", "class action", "settlement", 
    "verdict", "acquittal", "mistrial", "hung jury", "appeal", "remand", 
    "affirmed", "reversed", "en banc", "per curiam", "dissent", 
    "majority opinion", "concurrence", "PACER", "docket", "filing",
    "NRC ruling", "FDA approval", "FDA advisory", "antitrust", "monopoly", 
    "price fixing", "cartel", "consent decree", "fine", "penalty", 
    "enforcement action", "cease and desist", "subpoena", "grand jury", 
    "indictment", "arraignment", "plea deal", "plea agreement", "contempt", 
    "perjury", "obstruction", "whistleblower", "inspector general", 
    "special counsel", "independent counsel",
    "cloture", "filibuster", "reconciliation bill", "budget reconciliation", 
    "appropriations", "continuing resolution", "government shutdown", 
    "debt limit", "executive order", "presidential memorandum", "veto", 
    "veto override", "pocket veto", "signing statement", "executive privilege", 
    "impeachment", "resignation", "recall", "confirmation", "nomination", 
    "quorum", "unanimous consent", "roll call", "sanctions", "tariff", 
    "trade war", "export control", "entity list", "blacklist", "travel ban", 
    "visa restriction",
    "hash rate", "mempool", "hard fork", "soft fork", "51% attack", "halving", 
    "mining difficulty", "block reward", "on-chain", "off-chain", "Layer 2", 
    "lightning network", "smart contract", "DeFi", "DEX", "CEX", "stablecoin", 
    "depeg", "liquidation", "open interest", "funding rate", "perpetual", 
    "futures", "spot ETF", "ETF approval", "ETF rejection", "exchange hack", 
    "exploit", "rug pull", "protocol upgrade", "token launch", "airdrop", 
    "staking", "slashing",
    "resolution criteria", "resolution date", "settlement date", "resolves YES", 
    "resolves NO", "CLOB", "order book", "Polymarket", "Kalshi", "Metaculus", 
    "prediction market", "at-the-money", "probability", "implied probability",
    "phase 3 trial", "phase 2 trial", "clinical trial", "FDA advisory", 
    "advisory committee", "PDUFA date", "breakthrough designation", 
    "emergency use authorization", "EUA", "accelerated approval", 
    "priority review", "complete response letter", "CRL", "NDA", "BLA", 
    "ANDA", "biosimilar", "drug approval", "drug rejection", 
    "vaccine efficacy", "efficacy data", "safety signal", "adverse event", 
    "black box warning", "market withdrawal",
    "ceasefire", "peace talks", "escalation", "military strike", "drone attack", 
    "missile launch", "nuclear", "SWIFT ban", "correspondent banking", 
    "de-dollarization", "BRICS", "IMF bailout", "World Bank", "WTO ruling", 
    "border closure", "oil embargo", "energy crisis"
]
# Remove duplicates (e.g., filibuster)
DOMAIN_ALLOWLIST = list(dict.fromkeys(DOMAIN_ALLOWLIST))

nlp = None

if config.ENVIRONMENT == "production":
    try:
        import spacy
        try:
            nlp = spacy.load(config.SPACY_MODEL)
        except OSError:
            logger.critical("Model not downloaded")
            logger.critical(f"Run: python -m spacy download {config.SPACY_MODEL}")
            raise
    except ImportError:
        logger.critical("production env missing spaCy")
        raise

async def filter_signal(headline: str, source: str) -> bool:
    """Returns True to proceed, False to block."""
    env = os.environ.get("ENVIRONMENT", config.ENVIRONMENT)
    
    if env != "production":
        logger.info(f"[SPACY_FILTER] DEV MODE passthrough: {headline[:60]}")
        return True

    headline_lower = headline.lower()
    for term in DOMAIN_ALLOWLIST:
        if term.lower() in headline_lower:
            logger.info(f"[SPACY_FILTER] Passed (fast path allowlist): matched '{term}'")
            return True

    if nlp is None:
        logger.critical("spaCy not loaded but in production mode")
        raise RuntimeError("spaCy error")

    doc = nlp(headline)
    valid_types = {"ORG", "PERSON", "GPE", "LAW", "DATE", "MONEY", "PERCENT", "EVENT"}
    
    found_entities = [(e.text, e.label_) for e in doc.ents if e.label_ in valid_types]
    if found_entities:
        logger.info(f"[SPACY_FILTER] Passed (spaCy): {found_entities}")
        return True

    log_ents = [(e.text, e.label_) for e in doc.ents]
    logger.info(f"[SPACY_FILTER] Blocked (spaCy): {log_ents}")
    return False
