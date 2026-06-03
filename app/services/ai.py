"""
AI advisor: OpenAI, Anthropic, and local rule-based fallback.
"""
import json
import os
import sqlite3
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from app.services.portfolio import (
    portfolio_summary_data,
    portfolio_performance_data,
    portfolio_allocation_data,
    expected_dividends_data,
)
from app.services.loans import loans_health_data


def build_advisor_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    return {
        "summary": portfolio_summary_data(conn),
        "performance": portfolio_performance_data(conn),
        "allocation": portfolio_allocation_data(conn),
        "expected_dividends": expected_dividends_data(conn),
        "loans_health": loans_health_data(conn),
    }


def call_openai(question: str, snapshot: Dict[str, Any]) -> Optional[str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    system_prompt = (
        "你是家族辦公室投資顧問，請根據提供的投資快照，提出具體、可執行、風險分級的資產配置與再平衡建議。"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"投資快照JSON:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n問題:\n{question}",
            },
        ],
        "temperature": 0.2,
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        print(f"[AI] OpenAI HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}", flush=True)
        return None
    except Exception as e:
        print(f"[AI] OpenAI error: {type(e).__name__}: {e}", flush=True)
        return None


def call_anthropic(question: str, snapshot: Dict[str, Any]) -> Optional[str]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
    system_prompt = "你是家族辦公室投資顧問，請用條列與風險分級回答。"

    payload = {
        "model": model,
        "max_tokens": 900,
        "temperature": 0.2,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": f"投資快照JSON:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n問題:\n{question}",
            }
        ],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "\n".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
    except urllib.error.HTTPError as e:
        print(f"[AI] Anthropic HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}", flush=True)
        return None
    except Exception as e:
        print(f"[AI] Anthropic error: {type(e).__name__}: {e}", flush=True)
        return None


def local_rule_based_advice(question: str, snapshot: Dict[str, Any]) -> str:
    summary = snapshot["summary"]
    loans = snapshot["loans_health"]
    allocation = snapshot["allocation"]

    top_asset_types = sorted(allocation["asset_type"], key=lambda x: x["value"], reverse=True)
    concentration = top_asset_types[0]["name"] if top_asset_types else "未知"

    lines = [
        f"問題：{question}",
        f"目前淨資產約 {summary['net_assets']:.2f}，總資產 {summary['total_assets']:.2f}，總負債 {summary['total_liabilities']:.2f}。",
        f"目前最大資產類別集中於「{concentration}」。",
    ]

    mr = loans.get("maintenance_rate")
    if mr is not None:
        if mr < 167:
            lines.append("建議優先降低槓桿：減少高波動持倉或提前償還部分借款，將維持率拉回 200% 以上。")
        else:
            lines.append("槓桿風險目前可控，可採分批再平衡，不建議一次性大幅調整。")

    lines.append("再平衡建議：將單一產業或資產類別權重控制在可承受範圍，並保留現金緩衝以覆蓋至少 6-12 個月利息。")
    lines.append("配息規劃建議：以預估股息優先覆蓋借款利息，剩餘再投入低相關性資產。")
    return "\n".join(lines)
