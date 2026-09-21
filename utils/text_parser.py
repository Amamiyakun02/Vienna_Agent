import re
from typing import List, Dict, Optional
from pygments.lexers import guess_lexer
from pygments.util import ClassNotFound

# Keyword spesifik tiap bahasa (hindari kata terlalu umum)
JS_KEYWORDS = r"\b(return|var|let|const|if|else|for|while|console.log|class|import|export|async|await|=>)\b"
SQL_KEYWORDS = r"\b(SELECT|FROM|WHERE|ORDER BY|INSERT|UPDATE|DELETE|JOIN|ON|GROUP BY|LIMIT|CREATE|DROP|ALTER|TRUNCATE)\b"
BASH_KEYWORDS = r"\b(export|#!/bin/bash|echo|fi|then|elif|if|else|while|for|do|done|sudo|tar|backup)\b"
CONFIG_BLOCK = r"\b(server|location|listen|server_name|proxy_pass|http|events|upstream|error_log|worker_processes|client_max_body_size)\b"
JAVA_KEYWORDS = r"\b(public|class|static|void|System\.out\.println|new|extends|implements|package|import|private|protected|final)\b"
PYTHON_KEYWORDS = r"\b(def|class|import|from|print|self|__init__|return|except|BaseModel|pydantic|with|as|Fastapi|tf|pytorch|flask|lambda|async|await|elif|None|True|False)\b"
GO_KEYWORDS = r"\b(func|package|import|defer|go|chan|select|interface|struct|map|range|type|var|const|fmt\.Println)\b"
PHP_KEYWORDS = r"\b(\$this|echo|namespace|use|public|private|protected|class|new|extends|implements|require|include)\b"
RUBY_KEYWORDS = r"\b(def|class|module|end|puts|require|include|extend|attr_accessor|initialize)\b"
CSHARP_KEYWORDS = r"\b(public|private|class|namespace|using|void|static|int|string|bool|new|override|virtual|sealed)\b"
TYPESCRIPT_KEYWORDS = r"\b(import|export|class|interface|extends|implements|public|private|protected|readonly|async|await)\b"
DOCKERFILE_PATTERN = r"^(FROM|RUN|CMD|ENTRYPOINT|WORKDIR|COPY|ADD|EXPOSE|ENV|VOLUME|USER|ARG|LABEL|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\b"
YAML_PATTERN = r"^\s*[\w\-]+:\s+"
JSON_PATTERN = r"^\s*[\{\[]"
TERRAFORM_PATTERN = r"\b(resource|provider|variable|output|module|terraform|locals)\b"

GENERAL_SYMBOLS = r"[{}();=<>]"

IP_PATTERN = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
URL_PATTERN = re.compile(r"https?://[^\s]+")
HASH_PATTERN = re.compile(r"\b[a-fA-F0-9]{32,}\b")  # MD5/SHA-ish

LANGUAGE_MAP = {
    'Text only': 'unknown',
    'GDScript': 'shell',
    'CSS+Lasso': 'nginx',
    'Bash': 'shell',
    'Shell Session': 'shell',
    'JavaScript': 'javascript',
    'SQL': 'sql',
    'Python': 'python',
    'Java': 'java',
    'Go': 'go',
    'PHP': 'php',
    'Ruby': 'ruby',
    'C#': 'csharp',
    'TypeScript': 'typescript',
    'Dockerfile': 'dockerfile',
    'YAML': 'yaml',
    'JSON': 'json',
    'Terraform': 'terraform',
    'Tera Term macro': 'unknown',
}

def is_false_positive_code(text: str) -> bool:
    """Cegah deteksi kode pada blok pendek dengan IP, URL, atau hash."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) <= 2:
        if IP_PATTERN.search(text) or URL_PATTERN.search(text) or HASH_PATTERN.search(text):
            return True
    return False

def normalize_language_name(pygments_name: str, block: str) -> str:
    """Normalisasi nama bahasa berdasarkan keyword dalam blok."""
    name = LANGUAGE_MAP.get(pygments_name, pygments_name.lower())

    if re.search(JAVA_KEYWORDS, block, re.IGNORECASE):
        return 'java'
    if re.search(CSHARP_KEYWORDS, block, re.IGNORECASE):
        return 'csharp'
    if name == "python" or re.search(PYTHON_KEYWORDS, block, re.IGNORECASE):
        return 'python'
    if re.search(GO_KEYWORDS, block, re.IGNORECASE):
        return 'go'
    if re.search(PHP_KEYWORDS, block, re.IGNORECASE):
        return 'php'
    if re.search(RUBY_KEYWORDS, block, re.IGNORECASE):
        return 'ruby'
    if re.search(TYPESCRIPT_KEYWORDS, block, re.IGNORECASE):
        return 'typescript'
    if re.search(JS_KEYWORDS, block, re.IGNORECASE):
        return 'javascript'
    if re.search(BASH_KEYWORDS, block, re.IGNORECASE):
        return 'shell'
    if re.search(DOCKERFILE_PATTERN, block, re.IGNORECASE | re.MULTILINE):
        return 'dockerfile'
    if re.search(YAML_PATTERN, block, re.MULTILINE):
        return 'yaml'
    if re.search(JSON_PATTERN, block, re.MULTILINE):
        return 'json'
    if re.search(TERRAFORM_PATTERN, block, re.IGNORECASE):
        return 'terraform'
    if re.search(CONFIG_BLOCK, block, re.IGNORECASE):
        return 'nginx'
    if re.search(SQL_KEYWORDS, block, re.IGNORECASE):
        return 'sql'
    if name not in ['unknown', 'text only']:
        return name
    return 'unknown'

def guess_language(block: str) -> str:
    """Tebak bahasa blok kode dengan pygments, fallback heuristik."""
    try:
        lexer = guess_lexer(block)
        raw_name = lexer.name
        return normalize_language_name(raw_name, block)
    except ClassNotFound:
        if re.search(r'\b(public|class|static|void|System\.out\.println)\b', block):
            return 'java'
        if re.search(JS_KEYWORDS, block):
            return 'javascript'
        if re.search(BASH_KEYWORDS, block, re.IGNORECASE):
            return 'shell'
        if re.search(CONFIG_BLOCK, block):
            return 'nginx'
        if re.search(SQL_KEYWORDS, block.upper()):
            return 'sql'
        return 'unknown'
    except Exception:
        return 'unknown'

def is_code_block(block: str) -> bool:
    """Tentukan apakah blok adalah kode, dengan threshold keyword density dan tanda baca."""
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return False

    text = " ".join(lines)
    words = re.findall(r"\w+", text)
    total_words = len(words)

    # Hitung jumlah keyword
    keyword_count = 0
    symbol_count = 0
    for line in lines:
        if re.search(JS_KEYWORDS, line, re.IGNORECASE):
            keyword_count += len(re.findall(JS_KEYWORDS, line, re.IGNORECASE))
        if re.search(SQL_KEYWORDS, line, re.IGNORECASE):
            keyword_count += len(re.findall(SQL_KEYWORDS, line, re.IGNORECASE))
        if re.search(BASH_KEYWORDS, line, re.IGNORECASE):
            keyword_count += len(re.findall(BASH_KEYWORDS, line, re.IGNORECASE))
        if re.search(CONFIG_BLOCK, line, re.IGNORECASE):
            keyword_count += len(re.findall(CONFIG_BLOCK, line, re.IGNORECASE))

        if re.search(GENERAL_SYMBOLS, line):
            symbol_count += len(re.findall(GENERAL_SYMBOLS, line))

    if keyword_count == 0 and symbol_count == 0:
        return False

    # Hitung density keyword
    keyword_density = keyword_count / total_words if total_words > 0 else 0

    # Hitung tanda baca (.,;:!?)
    punctuation_count = len(re.findall(r"[.,;:!?]", text))

    # Jika density keyword rendah dan banyak tanda baca, ini kemungkinan narasi
    if keyword_density < 0.05 and punctuation_count > max(5, total_words / 10):
        return False

    if keyword_density >= 0.05:
        return True

    if symbol_count >= max(1, len(lines) / 3):
        return True

    # Cek indentasi berturut-turut minimal 3 baris
    indent_streak = 0
    max_streak = 0
    for line in lines:
        if re.match(r"^\s{4,}|\t", line):
            indent_streak += 1
            max_streak = max(max_streak, indent_streak)
        else:
            indent_streak = 0
    if max_streak >= 3 and (keyword_count > 0 or symbol_count > 0):
        return True

    # Fallback ke pygments
    try:
        lexer = guess_lexer(block)
        lang = lexer.name.lower()
        if lang in ["cbm basic v2", "text only", "unknown"] and keyword_count == 0 and symbol_count == 0:
            return False
        if lang in ["javascript", "sql", "bash", "shell", "python", "c", "cpp", "java", "go"]:
            return True
    except ClassNotFound:
        pass

    return False

def fix_language_prefix(prefix: str, code: str) -> str:
    """Perbaiki prefix bahasa bila tidak sesuai dengan isi kode."""
    prefix = prefix.lower()

    if prefix == "python" and re.search(JS_KEYWORDS, code, re.IGNORECASE):
        return "javascript"
    if prefix == "python" and re.search(SQL_KEYWORDS, code, re.IGNORECASE):
        return "sql"
    if prefix == "yaml" and re.search(r"\b(body|font-family|background-color|color|margin|padding|border)\b", code, re.IGNORECASE):
        return "css"
    if prefix == "go" and re.search(BASH_KEYWORDS, code, re.IGNORECASE):
        return "shell"
    if prefix == "java" and re.search(r'import\s+"fmt"', code):
        return "go"
    if prefix == "go" and not re.search(GO_KEYWORDS, code, re.IGNORECASE) and re.search(JS_KEYWORDS, code, re.IGNORECASE):
        return "javascript"
    return prefix

def parse_hybrid(text: str) -> List[Dict[str, str]]:
    """Parsing gabungan narasi dan kode, dengan validasi prefix [code] bahasa."""
    blocks = re.split(r"\n\s*\n", text.strip())
    segments = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Cek prefix [code] bahasa:
        prefix_match = re.match(r"^\[code\]\s*(\w+):\s*\n?", block, re.IGNORECASE)
        if prefix_match:
            language_hint = prefix_match.group(1).lower()
            content = block[len(prefix_match.group(0)):].strip()
            # Validasi isi, kalau bukan kode ubah jadi narasi
            if is_code_block(content):
                lang = fix_language_prefix(language_hint, content)
                segments.append({"type": "code", "lang": lang, "content": content})
            else:
                segments.append({"type": "narrative", "content": content})
            continue

        if is_false_positive_code(block):
            segments.append({"type": "narrative", "content": block})
        elif is_code_block(block):
            lang = guess_language(block)
            lang = fix_language_prefix(lang, block)
            segments.append({"type": "code", "lang": lang, "content": block})
        else:
            segments.append({"type": "narrative", "content": block})

    return segments

# parsed = parse_hybrid(sample_text)
# for seg in parsed:
#     print(f"[{seg['type']}] {seg.get('lang', '-')}:")
#     print(seg['content'])
#     print("-----")
# # print(parsed)

def llm_output_parser(text: str) -> List[Dict[str, Optional[str]]]:
    """
    Parse input text containing narrative and code blocks delimited by triple backticks.
    Returns list of dicts with:
      - type: 'narrative' or 'code'
      - lang: language if code block (may be None)
      - content: the block content
    """
    # Regex pattern to split narrative and code blocks
    # Matches triple backticks optionally followed by lang, then capture content until closing triple backticks
    pattern = re.compile(
        r"```(\w+)?\n(.*?)```",
        re.DOTALL
    )

    segments = []
    last_end = 0

    for match in pattern.finditer(text):
        start, end = match.span()
        lang = match.group(1) or None
        code_content = match.group(2).strip('\n')

        # Narrative before this code block
        narrative_text = text[last_end:start].strip()
        if narrative_text:
            segments.append({
                "type": "narrative",
                "lang": None,
                "content": narrative_text
            })

        # Code block
        segments.append({
            "type": "code",
            "lang": lang,
            "content": code_content
        })

        last_end = end

    # Any remaining narrative after last code block
    remaining_text = text[last_end:].strip()
    if remaining_text:
        segments.append({
            "type": "narrative",
            "lang": None,
            "content": remaining_text
        })

    return segments