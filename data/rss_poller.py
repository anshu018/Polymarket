import asyncio
import logging
from datetime import datetime
import aiohttp
import feedparser
import config

logger = logging.getLogger(__name__)

FEEDS = {
    # ── TIER 1: HIGHEST SIGNAL — directly produces Polymarket headlines ────────
    "NPR News":             "https://feeds.npr.org/1001/rss.xml",
    "NPR World":            "https://feeds.npr.org/1004/rss.xml",
    "NPR Politics":         "https://feeds.npr.org/1014/rss.xml",
    "BBC News World":       "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Politico":             "https://rss.politico.com/politics-news.xml",
    "NYT World":            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "WSJ World":            "https://feeds.a.dj.com/rss/RSSWorldNews.xml",

    # ── TIER 2: CRYPTO — high-frequency Polymarket market coverage ─────────────
    "CoinDesk":             "https://feeds.feedburner.com/CoinDesk",
    "CoinTelegraph":        "https://cointelegraph.com/rss",

    # ── TIER 3: SPORTS — election/championship markets ─────────────────────────
    "ESPN":                 "https://www.espn.com/espn/rss/news",

    # ── TIER 4: POLITICS / POLICY ─────────────────────────────────────────────
    "The Hill":             "https://thehill.com/feed/",
    "Guardian World":       "https://www.theguardian.com/world/rss",
    "Al Jazeera":           "https://www.aljazeera.com/xml/rss/all.xml",

    # ── TIER 5: ECONOMICS / MARKETS ───────────────────────────────────────────
    "Federal Reserve FOMC": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "Financial Times":      "https://www.ft.com/rss/home",
    "Bloomberg Markets":    "https://feeds.bloomberg.com/markets/news.rss",

    # ── TIER 6: LEGAL / GOVERNMENT (SCOTUS markets, White House policy) ─────────
    "SCOTUSblog":           "https://www.scotusblog.com/feed/",
    "White House":          "https://www.whitehouse.gov/news/feed/",
    "Fox News":             "https://moxie.foxnews.com/google-publisher/latest.xml",

    # ── TIER 7: PLATFORM NEWS / SOURCE SEARCH (Google News Search queries) ──────
    "AP News (via Google)": "https://news.google.com/rss/search?q=source:%22Associated+Press%22&hl=en-US&gl=US&ceid=US:en",
    "Metaculus (via Google)": "https://news.google.com/rss/search?q=%22Metaculus%22&hl=en-US&gl=US&ceid=US:en",
    "Polymarket (via Google)": "https://news.google.com/rss/search?q=%22Polymarket%22&hl=en-US&gl=US&ceid=US:en",
    "Kalshi (via Google)":  "https://news.google.com/rss/search?q=%22Kalshi%22&hl=en-US&gl=US&ceid=US:en",
}

_seen_urls = set()

async def _fetch_and_parse_feed(session: aiohttp.ClientSession, name: str, rss_url: str) -> tuple[str, str, list[dict]]:
    """Fetch and parse a single feed."""
    if "{TODAY}" in rss_url:
        today = datetime.now().strftime("%Y-%m-%d")
        rss_url = rss_url.replace("{TODAY}", today)
        
    try:
        async with asyncio.timeout(15):
            async with session.get(rss_url, headers={'User-Agent': 'ZeroAlphaAgent/1.0'}) as response:
                if response.status != 200:
                    logger.warning(f"Feed {name} HTTP {response.status}")
                    return name, "error", []
                content = await response.read()
                
        loop = asyncio.get_event_loop()
        parsed = await loop.run_in_executor(None, feedparser.parse, content)
        
        articles = []
        for entry in parsed.get("entries", []):
            url = entry.get("link", "")
            if not url:
                continue
            
            published_at = entry.get("published", datetime.now().isoformat())
            title = entry.get("title", "")
            if not title:
                continue
                
            articles.append({
                "headline": title.strip(),
                "source_name": name,
                "source_url": rss_url,
                "article_url": url,
                "published_at": published_at
            })
        return name, "success", articles
        
    except asyncio.TimeoutError:
        logger.warning(f"Feed {name} timed out after 15 seconds")
        return name, "timeout", []
    except Exception as e:
        logger.warning(f"Feed {name} failed: {e}")
        return name, "error", []

async def poll_once() -> list[dict]:
    """Single poll of all 20 feeds concurrently. Returns a list of new unique article dicts."""
    results = []
    success_count = 0
    fail_count = 0
    timeout_count = 0
    
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_and_parse_feed(session, name, url) for name, url in FEEDS.items()]
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in completed:
            if isinstance(res, Exception):
                fail_count += 1
                continue
                
            name, status, articles = res
            if status == "success":
                success_count += 1
            elif status == "timeout":
                timeout_count += 1
            else:
                fail_count += 1
                
            for article in articles:
                url = article["article_url"]
                if url not in _seen_urls:
                    _seen_urls.add(url)
                    results.append(article)
                    
    logger.info(
        f"[RSS_POLLER] Cycle: {len(FEEDS)} feeds | "
        f"{success_count} success, {timeout_count} timeouts, {fail_count} errors | "
        f"New articles: {len(results)}"
    )
    return results

async def start_poller(queue: asyncio.Queue) -> None:
    """Runs poll_once in an infinite loop, placing new articles on the queue."""
    logger.info("[RSS_POLLER] Starting polling loop.")
    while True:
        try:
            new_articles = await poll_once()
            for article in new_articles:
                # Blocks naturally when queue is full
                await queue.put(article)
        except Exception as e:
            logger.error(f"[RSS_POLLER] Unexpected exception in poll loop: {e}")
            
        await asyncio.sleep(getattr(config, "RSS_POLL_INTERVAL_SECONDS", 10))
