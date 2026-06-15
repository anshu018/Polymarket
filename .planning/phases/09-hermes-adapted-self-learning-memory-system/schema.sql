-- Schema setup for agent_memories table
-- To be run in the Supabase SQL Editor

CREATE TABLE IF NOT EXISTS agent_memories (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  agent_name TEXT NOT NULL UNIQUE,
  memory_content TEXT NOT NULL DEFAULT '',
  market_profile TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  last_updated TIMESTAMPTZ DEFAULT NOW(),
  total_updates INTEGER NOT NULL DEFAULT 0
);

-- Seed rows on first deploy
INSERT INTO agent_memories (agent_name, memory_content, market_profile) VALUES
  ('news_analyst', 
   'FDA approval headlines: historically 40% false positive rate. Default conservative confidence.' || chr(10) || '§' || chr(10) || 'AP News feed: reliable for political/economic markets. Less reliable for science/health.' || chr(10) || '§' || chr(10) || 'Metaculus and Kalshi signals: higher precision than general news. Weight accordingly.',
   'Signal categories with best historical accuracy: election outcomes, economic indicators.' || chr(10) || '§' || chr(10) || 'Signal categories with worst historical accuracy: FDA/regulatory, scientific announcements.'),
  ('contract_parser',
   'Resolution criteria with "substantially", "majority of", "significant": high ambiguity. Flag for skip.' || chr(10) || '§' || chr(10) || 'Polymarket binary markets resolve YES/NO strictly — "close" outcomes default to NO historically.' || chr(10) || '§' || chr(10) || 'Markets with multiple conditions joined by AND: all conditions must resolve favorably.',
   'Most ambiguous categories: health/science, geopolitical.' || chr(10) || '§' || chr(10) || 'Least ambiguous categories: sports outcomes, exact numeric thresholds.'),
  ('trade_decision',
   'Price range 0.3–0.5 with <10 days to resolution: historically highest EV range.' || chr(10) || '§' || chr(10) || 'Election markets: model consistently overestimates certainty. Reduce position by 25%.' || chr(10) || '§' || chr(10) || 'Brier score gate exists — prioritize calibration over aggressive sizing in paper trading phase.',
   'Profitable categories (paper): economic indicators, election outcomes.' || chr(10) || '§' || chr(10) || 'Loss-making categories (paper): FDA/health, long-horizon (>30 days) markets.'),
  ('coordinator',
   'News Analyst confidence >0.8 + Trade Decision = BUY: historically reliable combination.' || chr(10) || '§' || chr(10) || 'Single-agent signal without corroboration: reduce final confidence by 0.15.' || chr(10) || '§' || chr(10) || 'Disagreement between News Analyst and Trade Decision: default to SKIP unless both >0.7.',
   'Best performing signal combinations: high news confidence + medium price (0.35–0.55).' || chr(10) || '§' || chr(10) || 'Worst performing: low news confidence + high price (>0.65) — avoid.')
ON CONFLICT (agent_name) DO NOTHING;
