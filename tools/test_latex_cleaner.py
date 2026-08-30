import re

def clean_discord_latex(text: str) -> str:
    if not text:
        return text

    symbol_map = {
        r'\\alpha': 'α', r'\\beta': 'β', r'\\gamma': 'γ', r'\\delta': 'δ',
        r'\\epsilon': 'ε', r'\\zeta': 'ζ', r'\\eta': 'η', r'\\theta': 'θ',
        r'\\lambda': 'λ', r'\\mu': 'μ', r'\\pi': 'π', r'\\rho': 'ρ',
        r'\\sigma': 'σ', r'\\tau': 'τ', r'\\phi': 'φ', r'\\omega': 'ω',
        r'\\Delta': 'Δ', r'\\Theta': 'Θ', r'\\Lambda': 'Λ', r'\\Sigma': 'Σ',
        r'\\Omega': 'Ω',
        r'\\cdot': '·', r'\\times': '×', r'\\div': '÷',
        r'\\leq?': '≤', r'\\geq?': '≥', r'\\neq': '≠', r'\\approx': '≈',
        r'\\pm': '±', r'\\to': '→', r'\\rightarrow': '→', r'\\leftarrow': '←',
        r'\\infty': '∞', r'\\partial': '∂', r'\\nabla': '∇',
        r'\\in': '∈', r'\\notin': '∉', r'\\subset': '⊂', r'\\subseteq': '⊆'
    }

    def replace_block_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', inner)
        inner = re.sub(r'\\(?:text|mathrm|mathbf)\{([^}]+)\}', r'\1', inner)
        inner = re.sub(r'\\(?:left|right)', '', inner)
        return f"\n```\n{inner}\n```\n"

    # Only match $$...$$ block math
    text = re.sub(r'\$\$(.+?)\$\$', replace_block_math, text, flags=re.DOTALL)
    text = re.sub(r'\\\((.+?)\\\)', r'$\1$', text)

    def replace_inline_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', inner)
        inner = re.sub(r'\\(?:text|mathrm|mathbf)\{([^}]+)\}', r'\1', inner)
        inner = re.sub(r'\\(?:left|right)', '', inner)
        inner = inner.replace('\\', '')
        
        if len(inner) == 1 and inner.isalpha():
            return f"*{inner}*"
        return inner

    text = re.sub(r'(?<![\w\$])\$(?!\d)([^$\n]+?)\$(?![\w\$])', replace_inline_math, text)

    # Clean HTML tags into Discord markdown (no raw <br> or <b>)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<(b|strong)>', '**', text, flags=re.IGNORECASE)
    text = re.sub(r'</(b|strong)>', '**', text, flags=re.IGNORECASE)
    text = re.sub(r'<(i|em)>', '*', text, flags=re.IGNORECASE)
    text = re.sub(r'</(i|em)>', '*', text, flags=re.IGNORECASE)
    text = re.sub(r'<code>', '`', text, flags=re.IGNORECASE)
    text = re.sub(r'</code>', '`', text, flags=re.IGNORECASE)

    return text

sample = """
🟢 **Zero is online and ready.**

### 🛠️ What Was Just Deployed
1. **HTML Tag Sanitizer:** Fixed the bare `<br>` tags in \[bridge.py\](file:///workspace/tools/bridge.py).
2. **Semantic Titling:** Generates \[CHOICES: Step 1 | Step 2\].
"""

out = clean_discord_latex(sample)
print("CLEANED OUTPUT:")
print(out)
