# Hermes Agent Integration Guide

English | [한국어](hermes-agent.ko.md)

Guide to connecting Nous Research's [Hermes Agent](https://hermes-agent.nousresearch.com/) to `local-llm-starter`'s long-context model (Strata Qwen3.8-Flash-Next 128K).

---

## 1. Overview

- **Hermes Agent**: Autonomous agent runtime supporting tool calling, persistent memory, autonomous skill creation, and a full TUI.
- **Recommended Profile**: `profiles/strata-qwen3.8-flash-next-128k.env` (124K~128K context window).
  - Hermes Agent recommends at least 64K context for multi-turn tool calling and long session memory.

---

## 2. Quick Setup

### Linux (Direct on Host)
```bash
# Defaults: http://localhost:8000/v1, qwen3.8-flash-next-iq3_s, context 124K
./scripts/setup-hermes.sh
```

### Windows (Remote Connection)
```powershell
# Defaults: http://10.6.6.56:8000/v1
.\scripts\setup-hermes.ps1
```

---

## 3. Manual Configuration

If Hermes CLI is already installed:

```bash
hermes config set model.provider custom
hermes config set model.base_url http://localhost:8000/v1
hermes config set model.default qwen3.8-flash-next-iq3_s
hermes config set model.context_length 124000
hermes config set telemetry.shared_metrics.enabled false
hermes config set telemetry.shared_metrics.send false
```

---

## 4. Smoke Test

```bash
hermes chat -q "Say OK only."
```

---

## 5. Usage

- **Interactive TUI**: Run `hermes` in your terminal.
- **Single Query**: Run `hermes chat -q "query"`
- **Resume Session**: Run `hermes -c` or `hermes --resume latest`

