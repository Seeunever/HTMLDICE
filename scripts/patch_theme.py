from pathlib import Path

p = Path(__file__).resolve().parent.parent / "index.html"
text = p.read_text(encoding="utf-8")
start = text.index("<style>")
end = text.index("</style>") + len("</style>")
head = """  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=Noto+Serif+SC:wght@400;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="cthulhu.css">"""
text = text[:start] + head + text[end:]
p.write_text(text, encoding="utf-8")
print("ok")
