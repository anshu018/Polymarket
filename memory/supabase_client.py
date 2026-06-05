import asyncio
import logging
from functools import partial
from supabase import create_client, Client
import config

logger = logging.getLogger(__name__)
_client: Client | None = None

async def get_client() -> Client:
    global _client
    if _client is not None:
        return _client
    loop = asyncio.get_event_loop()
    _client = await loop.run_in_executor(
        None,
        partial(
            create_client,
            config.SUPABASE_URL,
            config.SUPABASE_KEY
        )
    )
    logger.info("[SUPABASE] Client ready")
    return _client
