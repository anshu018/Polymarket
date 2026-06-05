import logging
from datetime import datetime, timezone
from typing import Optional, Any

try:
    from py_clob_client.client import ClobClient
except ImportError:
    ClobClient = Any  # Fallback for typing if not installed

import config
from monitoring.telegram_alerts import alert_system_halt

logger = logging.getLogger(__name__)

# One module-level variable holds the initialized ClobClient after successful derivation.
# All other files import this client from here. No other file creates a ClobClient.
_polymarket_client: Optional[ClobClient] = None

async def initialize_polymarket_client() -> ClobClient:
    """
    Derives and holds Polymarket L2 API credentials from the L1 private key at startup.
    It is the single source of truth for the authenticated ClobClient instance.
    This centralized mechanism ensures credentials are not exposed throughout the system,
    matching the architecture requirement to isolate L1 keys and derived L2 credentials.
    """
    global _polymarket_client
    
    try:
        # 1. Read POLYMARKET_PRIVATE_KEY from config
        # 2. Instantiate ClobClient with: host, key, chain_id
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=137
        )
        
        # 3. Call client.create_or_derive_api_creds()
        creds = client.create_or_derive_api_creds()
        
        # 4. Set the derived credentials on the client with client.set_api_creds(creds)
        client.set_api_creds(creds)
        
        # 5. Store the authenticated client in the module-level variable
        _polymarket_client = client
        
        # 6. Log success with: component name, timestamp, and confirmation
        now = datetime.now(timezone.utc).isoformat()
        component_name = "execution.polymarket_auth"
        logger.info(f"[{now}] {component_name}: Polymarket API credentials successfully derived and initialized.")
        
        # 7. Return the authenticated client
        return _polymarket_client

    except Exception as e:
        # 1. Log the error with full exception detail
        logger.error(f"Polymarket credential derivation failed: {e}", exc_info=True)
        
        # 2. Call alert_system_halt() from monitoring/telegram_alerts.py
        await alert_system_halt(
            reason="Polymarket credential derivation failed",
            drawdown_pct=0.0,
            portfolio_value=0.0
        )
        
        # 3. Raise the exception — do not swallow it.
        raise

def get_polymarket_client() -> ClobClient:
    """
    Returns the module-level authenticated Polymarket client.
    Raises RuntimeError if called before initialize_polymarket_client() has completed.
    This prevents any other module from accidentally using an unauthenticated client.
    """
    if _polymarket_client is None:
        raise RuntimeError("Polymarket client accessed before initialize_polymarket_client() has completed.")
    return _polymarket_client
