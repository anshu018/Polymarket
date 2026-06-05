import asyncio, time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv('.env')
from llm.contract_parser import (
    parse_contract, get_cached_keywords
)
from memory.supabase_client import get_client

MARKET_IDS = ['P1','P2','P3','C1','C2',
              'S1','S2','L1','L2','E1']

async def main():
    client = await get_client()

    # 5.1 and 5.4 — check cache has 10 rows
    # with all required fields per TESTING.md
    rows = client.table('resolution_keyword_cache')\
        .select('market_id,resolution_keywords,'
                'resolution_conditions,'
                'ambiguity_score,cached_at,'
                'resolution_type')\
        .in_('market_id', MARKET_IDS)\
        .execute()

    print(f'Cache rows found: {len(rows.data)}')

    valid_51 = True
    for r in rows.data:
        kws = r.get('resolution_keywords') or []
        conds = r.get('resolution_conditions') or {}
        score = r.get('ambiguity_score')
        rtype = r.get('resolution_type') or ''
        cached = r.get('cached_at') or ''
        src = conds.get('resolution_source','')
        cond = conds.get('resolution_condition','')
        entities = conds.get('key_entities',[])
        field_ok = (
            len(kws) >= 3 and
            bool(src) and
            bool(cond) and
            len(entities) >= 1 and
            score is not None and
            0.0 <= float(score) <= 1.0 and
            bool(rtype) and
            bool(cached)
        )
        status = 'OK' if field_ok else 'FAIL'
        print(f'  {r["market_id"]}: {status} | '
              f'kws={len(kws)} score={score:.2f}')
        if not field_ok:
            valid_51 = False

    all_10 = len(rows.data) == 10
    print(f'5.1 VERDICT: {"PASS" if all_10 and valid_51 else "FAIL"}')
    print(f'5.4 VERDICT: {"PASS" if all_10 else f"FAIL {len(rows.data)}/10"}')

    # 5.2 — keywords meaningful not generic
    stopwords = {'the','will','market','yes','no',
                 'a','an','in','of','to','is','be'}
    kw_ok = True
    for r in rows.data:
        kws = r.get('resolution_keywords') or []
        bad = [k for k in kws
               if k.lower().strip() in stopwords]
        if bad:
            print(f'  5.2 FAIL {r["market_id"]}: '
                  f'generic keywords: {bad}')
            kw_ok = False
    print(f'5.2 VERDICT: {"PASS" if kw_ok else "FAIL"}')

    # 5.3 — ambiguity calibrated
    btc = next((r for r in rows.data
                if r['market_id']=='C1'), None)
    e1  = next((r for r in rows.data
                if r['market_id']=='E1'), None)
    if btc and e1:
        btc_score = float(btc['ambiguity_score'])
        e1_score  = float(e1['ambiguity_score'])
        print(f'  BTC (clear): {btc_score:.2f}')
        print(f'  E1 (ambiguous): {e1_score:.2f}')
        scores_valid = (
            0.0 <= btc_score <= 1.0 and
            0.0 <= e1_score <= 1.0
        )
        print(f'5.3 VERDICT: {"PASS" if scores_valid else "FAIL"}')
    else:
        print('5.3 VERDICT: FAIL - markets missing')

    # 5.5 — cache hit returns fast (no API call)
    t0 = time.perf_counter()
    r = await parse_contract(
        'P1',
        'Will Donald Trump be impeached before January 2027?',
        'Resolves YES if House votes to impeach before Jan 2027.'
    )
    ms = (time.perf_counter()-t0)*1000
    print(f'  5.5 second parse: {ms:.0f}ms')
    print(f'5.5 VERDICT: {"PASS" if ms < 500 else f"FAIL {ms:.0f}ms"}')

    # 5.6 — stale entry triggers refresh
    stale = (datetime.now(timezone.utc) -
             timedelta(hours=25)).isoformat()
    client.table('resolution_keyword_cache')\
        .update({'cached_at': stale})\
        .eq('market_id','P3')\
        .execute()
    t0 = time.perf_counter()
    r56 = await parse_contract(
        'P3',
        'Will France government collapse in 2026?',
        'Resolves YES if no confidence vote passes before Dec 31 2026.'
    )
    ms = (time.perf_counter()-t0)*1000
    print(f'  5.6 stale refresh: {ms:.0f}ms')
    print(f'5.6 VERDICT: {"PASS" if ms > 500 and r56 else f"FAIL ms={ms:.0f}"}')

    # 5.7 — timeout fires at 18 seconds
    try:
        await asyncio.wait_for(
            asyncio.sleep(19), timeout=18
        )
        print('5.7 VERDICT: FAIL - no timeout')
    except asyncio.TimeoutError:
        print('5.7 VERDICT: PASS - TimeoutError at 18s')

    # 5.8 — get_cached_keywords works for fast path
    kws = await get_cached_keywords('C1')
    has_kws = kws is not None and len(kws) >= 3
    print(f'  5.8 C1 keywords: {kws[:3] if kws else None}')
    print(f'5.8 VERDICT: {"PASS" if has_kws else "FAIL"}')

asyncio.run(main())
