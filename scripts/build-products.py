"""Build the product homepage and same-origin, checksummed local edition downloads."""
import hashlib
import html
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("edition_builder", ROOT / "scripts/build-editions.py")
editions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(editions)

PRODUCTS = [
    ("lite", "Lite", "輕量起步", "無獨立顯示卡也能開始，使用你自己的雲端模型 API 金鑰。", "4 核心 CPU · 8 GB RAM · 無需 GPU", "雲端 API，模型用量由供應商另外計費",
     "免費", "Core 開源核心", "硬體：一般電腦即可"),
    ("personal", "Personal", "個人本機 AI", "在自己的電腦執行模型，適合私人助理、筆記與日常工作。", "8C/16T · 32 GB RAM · 16 GB VRAM", "Ollama · Qwen2.5 3B",
     "免費", "Core 開源核心", "參考硬體約 NT$4.5–6 萬（自備）"),
    ("workstation", "Workstation", "工作站協作", "以多代理協作處理較大的任務，適合有獨立工作站的使用者。", "16C/32T · 64–128 GB RAM · 32 GB VRAM", "vLLM · Qwen2.5 7B",
     "免費", "3 人以下；超過 3 人共用 Team NT$490／人／月（規劃中）", "參考硬體約 NT$18–25 萬（自備）"),
    ("server", "Server", "自架伺服器", "在自己的 Linux 伺服器部署，集中執行排程與代理任務。", "32C/64T · 256 GB RAM · 96 GB VRAM · Linux", "vLLM · Qwen2.5 7B",
     "NT$490", "／人／月 · Team 自架（規劃中，年繳、最少 5 人）；Enterprise 另行報價", "參考硬體約 NT$45–55 萬（自備）"),
]
GUIDES = [slug for slug, *_ in PRODUCTS] + ["cloud"]
TITLES = {slug: title for slug, title, *_ in PRODUCTS} | {"cloud": "Cloud"}


def build(out: Path) -> None:
    downloads = out / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    cards, manifest, sums = [], [], []
    for slug, title, label, desc, hardware, model, price, plan, cost in PRODUCTS:
        bundle = editions.build(slug, downloads)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        size = bundle.stat().st_size
        manifest.append(dict(id=slug, name=title, version=editions.version(), file=f"downloads/{bundle.name}",
                             size_bytes=size, sha256=digest, license="MIT", status="early-alpha"))
        sums.append(f"{digest}  {bundle.name}")
        cards.append(f'''<article class="card"><span class="eyebrow">{label}</span><h3>AIOS {title}</h3>
<p>{desc}</p><p class="spec">{hardware}<br>{model}</p><div class="price">{price} <small>{plan}</small></div>
<p class="meta">{cost}</p>
<a class="button" href="downloads/{bundle.name}" download>免費下載 {title} <span>↓</span></a>
<p class="meta">v{html.escape(editions.version())} · ZIP · {size / 1024:.0f} KB</p>
<a href="guides/{slug}.html">安裝與使用說明 →</a></article>''')
    guide_dir = out / "guides"
    guide_dir.mkdir(exist_ok=True)
    for slug in GUIDES:
        title = TITLES[slug]
        guide = (ROOT / "editions" / slug / "GUIDE.zh-TW.md").read_text(encoding="utf-8")
        (guide_dir / f"{slug}.html").write_text(
            '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>AIOS {title} 使用說明</title><style>body{{font:16px/1.8 system-ui;max-width:850px;margin:40px auto;padding:0 24px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style>'
            f'<a href="../">← 返回產品中心</a><h1>AIOS {title} 使用說明</h1><pre>{html.escape(guide)}</pre></html>', encoding="utf-8")
    (downloads / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    (out / "products.json").write_text(json.dumps({"products": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    template = (ROOT / "web/product.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(template.replace("{{PRODUCTS}}", "\n".join(cards)).replace("{{VERSION}}", html.escape(editions.version())), encoding="utf-8")


if __name__ == "__main__":
    import sys
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
