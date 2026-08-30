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

    # 1. Convert block math $$...$$
    def replace_block_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', inner)
        inner = re.sub(r'\\(?:text|mathrm|mathbf)\{([^}]+)\}', r'\1', inner)
        inner = re.sub(r'\\(?:left|right)', '', inner)
        return f"\n```\n{inner}\n```\n"

    text = re.sub(r'\$\$(.+?)\$\$', replace_block_math, text, flags=re.DOTALL)
    text = re.sub(r'\\\[(.+?)\\\]', replace_block_math, text, flags=re.DOTALL)

    # 2. Convert inline math \( ... \)
    text = re.sub(r'\\\((.+?)\\\)', r'$\1$', text)

    # 3. Convert inline math $...$ (ignoring currency like $50 or $100.00)
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
    return text

# Test cases
tests = [
    "The parameter is $d$ in the formula.",
    "Cost is $50 or $100.50 per month, not $x$.",
    "We know $\\alpha \\le \\beta$ and $O(n \\log n)$ complexity.",
    "Formula: $$\\frac{a + b}{c} = \\mu$$ for all $i$.",
    "The value of $d$ is 4.",
    "In d-dimensional space $d=3$, the distance is $O(\\sqrt{d})$."
]

for t in tests:
    print("INPUT: ", t)
    print("OUTPUT:", clean_discord_latex(t))
    print("-" * 40)
