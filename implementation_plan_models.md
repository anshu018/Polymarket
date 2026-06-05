# Upgrade LLM Configurations to Free Models/Endpoints

Upgrade the LLM routing logic across all prediction agent wrappers to target free model endpoints on OpenRouter, NVIDIA NIM, and the DeepSeek API, and remove the SiliconFlow integration.

## User Review Required

> [!IMPORTANT]
> The primary provider for Trade Decision is switched from SiliconFlow to NVIDIA NIM.
> The primary provider for News Analyst is switched from OpenRouter `qwen/qwen3-32b` to OpenRouter `google/gemma-4-12b-it:free`.
> The primary provider for Contract Parser is switched from OpenRouter `deepseek/deepseek-chat` to OpenRouter `moonshotai/kimi-k2.6:free`.
> We need the user to obtain and configure two new API keys:
> 1. **NVIDIA API Key** (from [build.nvidia.com](https://build.nvidia.com/))
> 2. **DeepSeek API Key** (from [platform.deepseek.com](https://platform.deepseek.com/))
>
> We will remove the requirement for `SILICONFLOW_API_KEY` from startup checks.

## Open Questions

None. The user has explicitly specified the models, primary routes, and fallback chains.

## Proposed Changes

### Configuration Updates

#### [MODIFY] [config.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/config.py)
- Define new API endpoints:
  - `PROVIDER_NVIDIA = "https://integrate.api.nvidia.com/v1"`
  - `PROVIDER_DEEPSEEK = "https://api.deepseek.com/v1"`
  - `PROVIDER_OPENROUTER = "https://openrouter.ai/api/v1"`
- Add new keys `NVIDIA_API_KEY` and `DEEPSEEK_API_KEY`.
- Remove `SILICONFLOW_API_KEY` from `_required_vars` and check list. Add `NVIDIA_API_KEY` and `DEEPSEEK_API_KEY` to `_required_vars`.
- Update model constants:
  - `MODEL_NEWS_ANALYST = "google/gemma-4-12b-it:free"`
  - `MODEL_NEWS_ANALYST_FALLBACK = "qwen/qwen3-32b"`
  - `MODEL_CONTRACT_PARSER = "moonshotai/kimi-k2.6:free"`
  - `MODEL_CONTRACT_PARSER_FALLBACK = "deepseek-ai/deepseek-v4-flash"`
  - `MODEL_TRADE_DECISION = "qwen/qwen3-235b-a22b"`
  - `MODEL_COORDINATOR = "qwen/qwen3-32b"`

### News Analyst Updates

#### [MODIFY] [news_analyst.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/news_analyst.py)
- Update `classify_signal` to:
  - First attempt primary: `MODEL_NEWS_ANALYST` (`google/gemma-4-12b-it:free`) via OpenRouter with a 6-second timeout.
  - If that fails/times out, fallback to: `MODEL_NEWS_ANALYST_FALLBACK` (`qwen/qwen3-32b`) via NVIDIA NIM (using `NVIDIA_API_KEY`) with a 4-second timeout.
  - Maintain the overall timeout limit of 10 seconds.

### Contract Parser Updates

#### [MODIFY] [contract_parser.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/contract_parser.py)
- Update `_call_deepseek` to support fallback:
  - First attempt primary: `MODEL_CONTRACT_PARSER` (`moonshotai/kimi-k2.6:free`) via OpenRouter. Timeout: 9s.
  - If that fails/times out, fallback 1: `MODEL_CONTRACT_PARSER_FALLBACK` (`deepseek-ai/deepseek-v4-flash`) via DeepSeek API (using `DEEPSEEK_API_KEY`). Timeout: 5s.
  - If that fails/times out, fallback 2: `MODEL_CONTRACT_PARSER_FALLBACK` (`deepseek-ai/deepseek-v4-flash`) via NVIDIA NIM (using `NVIDIA_API_KEY`). Timeout: 4s.
  - Keep overall timeout limit to 18 seconds (`config.LLM_TIMEOUT_SECONDS`).

### Trade Decision Updates

#### [MODIFY] [trade_decision.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/trade_decision.py)
- Replace SiliconFlow primary with NVIDIA NIM:
  - Primary: `MODEL_TRADE_DECISION` (`qwen/qwen3-235b-a22b`) via NVIDIA NIM (using `NVIDIA_API_KEY`). Timeout: 18s.
  - Fallback: `MODEL_TRADE_DECISION` (`qwen/qwen3-235b-a22b`) via OpenRouter (using `OPENROUTER_API_KEY`). Timeout: 15s.

### Coordinator Updates

#### [MODIFY] [coordinator.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/llm/coordinator.py)
- Update `escalate_to_llm_coordinator` to support primary/fallback:
  - Primary: `MODEL_COORDINATOR` (`qwen/qwen3-32b`) via NVIDIA NIM (using `NVIDIA_API_KEY`). Timeout: 18s.
  - Fallback: `MODEL_COORDINATOR` (`qwen/qwen3-32b`) via OpenRouter (using `OPENROUTER_API_KEY`). Timeout: 15s.

### Integration Tests Updates

#### [MODIFY] [test_integration.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/tests/test_integration.py)
- Update `mock_llm_apis` fixtures and routing mocks to handle the new endpoints:
  - Mock OpenRouter for `google/gemma-4-12b-it:free` (News Analyst) and `moonshotai/kimi-k2.6:free` (Contract Parser).
  - Mock DeepSeek API for `deepseek-ai/deepseek-v4-flash` (Contract Parser).
  - Mock NVIDIA NIM for `qwen/qwen3-32b` (News Analyst fallback), `deepseek-ai/deepseek-v4-flash` (Contract Parser fallback), and `qwen/qwen3-235b-a22b` (Trade Decision & Coordinator).
  - Adapt the test `test_6_10_siliconflow_failover` to test primary-to-fallback routing (now NVIDIA NIM primary failover to OpenRouter).

#### [MODIFY] [run_layer1.py](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/tests/run_layer1.py)
- Update the list of required variables checked by Layer 1 tests to check `NVIDIA_API_KEY` and `DEEPSEEK_API_KEY` instead of `SILICONFLOW_API_KEY`.

#### [MODIFY] [.env.example](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/.env.example) and [.env.test](file:///c:/Users/ash74/OneDrive/Desktop/Polymarket/.env.test)
- Replace `SILICONFLOW_API_KEY` with `NVIDIA_API_KEY` and `DEEPSEEK_API_KEY`.

## Verification Plan

### Automated Tests
- Run `pytest` to execute all integration and unit tests:
  ```powershell
  python -c "
  import subprocess, sys
  result = subprocess.run(
      [sys.executable, '-m', 'pytest'],
      capture_output=True,
      text=True,
      timeout=60
  )
  output = result.stdout + result.stderr
  print(output[-4000:])
  "
  ```
- Run Layer 1 verification tests to ensure env variables and database schemas are validated correctly:
  ```powershell
  python tests/run_layer1.py
  ```
