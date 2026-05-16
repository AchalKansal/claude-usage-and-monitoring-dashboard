"""
Token optimization intelligence — analyzes prompts and gives actionable
suggestions for reducing token usage without sacrificing quality.
"""
import re
from typing import Optional

import anthropic

client = anthropic.Anthropic()


# Rule-based quick checks (no API call needed)
def rule_based_suggestions(prompt: str, input_tokens: int) -> list[dict]:
    suggestions = []

    # Long pasted code blocks
    code_blocks = re.findall(r"```[\s\S]{500,}?```", prompt)
    if code_blocks:
        total_code_chars = sum(len(b) for b in code_blocks)
        suggestions.append({
            "type": "code_block",
            "suggestion": (
                f"You pasted {len(code_blocks)} code block(s) totalling ~{total_code_chars} chars. "
                "Instead of pasting full files, share only the relevant function/class. "
                "Estimated savings: 30-70% of code block tokens."
            ),
            "estimated_savings": int(total_code_chars / 4 * 0.5),
        })

    # Very long prompts
    if input_tokens > 5000:
        suggestions.append({
            "type": "long_prompt",
            "suggestion": (
                f"This prompt used {input_tokens:,} input tokens. "
                "Consider breaking the task into smaller, focused requests. "
                "Each turn should have a single clear goal."
            ),
            "estimated_savings": int(input_tokens * 0.3),
        })

    # Repeated boilerplate/context
    if len(prompt) > 2000:
        repeated = _find_repeated_phrases(prompt)
        if repeated:
            suggestions.append({
                "type": "repeated_content",
                "suggestion": (
                    f"Detected repeated phrases: {', '.join(repeated[:3])}. "
                    "Use CLAUDE.md to store persistent context instead of repeating it each turn."
                ),
                "estimated_savings": int(len(" ".join(repeated)) / 4),
            })

    # File paths listed in full many times
    path_matches = re.findall(r"/[\w/\-\.]{20,}", prompt)
    unique_paths = set(path_matches)
    if len(path_matches) > 5 and len(unique_paths) < len(path_matches) / 2:
        suggestions.append({
            "type": "repeated_paths",
            "suggestion": (
                "File paths appear many times. Define short aliases or use relative paths "
                "to reduce token usage."
            ),
            "estimated_savings": int(len(path_matches) * 5),
        })

    # Overly polite/verbose phrasing
    filler_patterns = [
        r"could you please", r"i would like you to", r"can you please",
        r"i want you to", r"please help me", r"i need you to"
    ]
    filler_count = sum(len(re.findall(p, prompt.lower())) for p in filler_patterns)
    if filler_count > 3:
        suggestions.append({
            "type": "verbose_phrasing",
            "suggestion": (
                "Use direct imperative phrasing ('Fix X', 'Add Y') instead of polite fillers. "
                "Saves 5-15 tokens per request and often improves response quality."
            ),
            "estimated_savings": filler_count * 8,
        })

    return suggestions


def _find_repeated_phrases(text: str, min_len: int = 30, min_count: int = 2) -> list[str]:
    words = text.split()
    repeated = []
    seen = {}
    for i in range(len(words) - 4):
        phrase = " ".join(words[i:i+5])
        if len(phrase) >= min_len:
            seen[phrase] = seen.get(phrase, 0) + 1
            if seen[phrase] == min_count:
                repeated.append(phrase)
    return repeated[:5]


def get_ai_optimization(prompt: str, input_tokens: int, model: str = "claude-haiku-4-5-20251001") -> Optional[str]:
    """
    Use Claude Haiku (cheapest) to give specific token-reduction advice for this prompt.
    Only called for prompts > 2000 tokens to avoid burning tokens on optimizing short prompts.
    """
    if input_tokens < 2000:
        return None

    truncated = prompt[:3000] + ("..." if len(prompt) > 3000 else "")

    system = (
        "You are a prompt efficiency expert. Analyze the user's prompt and give 2-3 "
        "specific, actionable suggestions to reduce token usage. Be concrete. "
        "Format as bullet points. Keep your response under 150 words."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=300,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"This prompt used {input_tokens:,} tokens. "
                    f"How can I reduce the token count?\n\nPROMPT:\n{truncated}"
                )
            }]
        )
        return response.content[0].text if response.content else None
    except Exception as e:
        return f"(AI analysis unavailable: {e})"


def analyze_prompt(prompt: str, input_tokens: int, use_ai: bool = False) -> list[dict]:
    """
    Return a list of optimization suggestions for a prompt.
    Each suggestion: {type, suggestion, estimated_savings}
    """
    if not prompt or not prompt.strip():
        return []

    results = rule_based_suggestions(prompt, input_tokens)

    if use_ai and input_tokens >= 2000:
        ai_text = get_ai_optimization(prompt, input_tokens)
        if ai_text:
            results.append({
                "type": "ai_analysis",
                "suggestion": ai_text,
                "estimated_savings": int(input_tokens * 0.25),
            })

    return results
