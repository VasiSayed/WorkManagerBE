import re

VAR_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}|{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}")


def extract_variables(*texts, extra=None):
    names = []
    for text in texts:
        for match in VAR_PATTERN.findall(text or ""):
            name = match[0] or match[1]
            if name and name not in names:
                names.append(name)
    for name in extra or []:
        if name and name not in names:
            names.append(str(name))
    return names


def render_template_text(text, values=None):
    values = values or {}

    def repl(match):
        key = match.group(1) or match.group(2)
        value = values.get(key)
        return "" if value is None else str(value)

    return VAR_PATTERN.sub(repl, text or "")
