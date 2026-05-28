import os
import re
import time
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


st.set_page_config(page_title="Shopify GMC Optimizer", page_icon="🛍️", layout="wide")

DEFAULT_API_VERSION = "2026-01"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"

DEFAULT_EXTERNAL_DOMAINS = [
    "cjdropshipping.com", "cjpacket.com", "alicdn.com", "aliexpress",
    "ae01.alicdn.com", "cf.cjdropshipping.com", "imgaz.staticbg.com",
    "banggood.com", "dhresource.com", "shein.com", "temu.com",
]

SHOPIFY_CDN_MARKERS = ["cdn.shopify.com", "shopifycdn.net"]
REQUEST_CONNECT_TIMEOUT = 20
REQUEST_READ_TIMEOUT = 150
DEFAULT_MAX_RETRIES = 6


@dataclass
class ProductData:
    id: str
    title: str
    handle: str
    description_html: str
    vendor: Optional[str]
    product_type: Optional[str]
    tags: List[str]
    options: List[dict]
    variants: List[dict]
    images: List[dict]


@dataclass
class ImageReplacement:
    original_url: str
    new_url: str
    alt: str
    status: str


def get_secret_or_env(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


def normalize_store_domain(store_domain: str) -> str:
    return (store_domain or "").strip().replace("https://", "").replace("http://", "").strip("/")


def normalize_api_version(api_version: str) -> str:
    api_version = (api_version or DEFAULT_API_VERSION).strip()
    return api_version if re.match(r"^\d{4}-\d{2}$", api_version) else DEFAULT_API_VERSION


def e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def extract_handle_from_product_url(product_url: str) -> str:
    product_url = (product_url or "").strip()
    if not product_url:
        raise ValueError("상품 URL을 입력해주세요.")
    parsed = urllib.parse.urlparse(product_url)
    parts = parsed.path.strip("/").split("/")
    if "products" not in parts:
        raise ValueError("URL 안에 /products/ 경로가 없습니다.")
    idx = parts.index("products")
    if idx + 1 >= len(parts):
        raise ValueError("상품 handle을 URL에서 찾을 수 없습니다.")
    return urllib.parse.unquote(parts[idx + 1])


def run_gql(store_domain: str, token: str, api_version: str, query: str, variables: Optional[dict] = None, max_retries: int = DEFAULT_MAX_RETRIES) -> dict:
    endpoint = f"https://{normalize_store_domain(store_domain)}/admin/api/{normalize_api_version(api_version)}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables or {}}
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(endpoint, headers=headers, json=payload, timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT))
            if res.status_code == 401:
                raise RuntimeError("401 Unauthorized: Shopify Admin API token을 확인해주세요.")
            if res.status_code == 403:
                raise RuntimeError("403 Forbidden: read/write products/files 권한을 확인해주세요.")
            if res.status_code == 404:
                raise RuntimeError(f"404 Not Found: store_domain/API version 확인 필요: {endpoint}")
            if res.status_code == 429:
                last_error = RuntimeError("429 Too Many Requests")
                time.sleep(min(8 * attempt, 40))
                continue
            res.raise_for_status()
            data = res.json()
            if "errors" in data:
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(data['errors'], ensure_ascii=False)}")
            return data["data"]
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as err:
            last_error = err
            time.sleep(min(5 * attempt, 30))
        except requests.exceptions.HTTPError as err:
            raise RuntimeError(f"Shopify API HTTP error: {err}. Response: {getattr(res, 'text', '')}")

    raise RuntimeError(f"Shopify API connection failed after {max_retries} retries. Last error: {last_error}")


def get_product_by_handle(store_domain: str, token: str, api_version: str, handle: str) -> ProductData:
    query = """
    query GetProduct($query: String!) {
      products(first: 1, query: $query) {
        edges {
          node {
            id
            title
            handle
            descriptionHtml
            vendor
            productType
            tags
            options { id name values }
            variants(first: 100) {
              edges {
                node {
                  id title sku availableForSale price compareAtPrice
                  selectedOptions { name value }
                }
              }
            }
            images(first: 100) {
              edges { node { id url altText width height } }
            }
          }
        }
      }
    }
    """
    data = run_gql(store_domain, token, api_version, query, {"query": f"handle:{handle}"})
    edges = data.get("products", {}).get("edges", [])
    if not edges:
        raise RuntimeError(f"상품을 찾지 못했습니다. handle: {handle}")
    node = edges[0]["node"]
    return ProductData(
        id=node["id"],
        title=node.get("title", ""),
        handle=node.get("handle", ""),
        description_html=node.get("descriptionHtml") or "",
        vendor=node.get("vendor"),
        product_type=node.get("productType"),
        tags=node.get("tags") or [],
        options=node.get("options") or [],
        variants=[edge["node"] for edge in node.get("variants", {}).get("edges", [])],
        images=[edge["node"] for edge in node.get("images", {}).get("edges", [])],
    )


def update_product_description(store_domain: str, token: str, api_version: str, product_id: str, new_html: str) -> dict:
    mutation = """
    mutation ProductUpdate($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product { id title handle }
        userErrors { field message }
      }
    }
    """
    data = run_gql(store_domain, token, api_version, mutation, {"product": {"id": product_id, "descriptionHtml": new_html}})
    result = data["productUpdate"]
    if result.get("userErrors"):
        raise RuntimeError(f"productUpdate userErrors: {result['userErrors']}")
    return result["product"]


def absolutize_url(src: str, product_url: Optional[str] = None) -> str:
    if not src:
        return src
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if product_url:
        parsed = urllib.parse.urlparse(product_url)
        return urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}", src)
    return src


def is_shopify_cdn_url(url: str) -> bool:
    return bool(url) and any(marker in url.lower() for marker in SHOPIFY_CDN_MARKERS)


def is_external_image_url(url: str, store_domain: str, external_domains: List[str]) -> bool:
    if not url:
        return False
    lower = url.lower().strip()
    if lower.startswith(("data:", "blob:", "#", "javascript:")):
        return False
    if is_shopify_cdn_url(url):
        return False
    if any(domain.lower() in lower for domain in external_domains if domain.strip()):
        return True
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host:
        return False
    if normalize_store_domain(store_domain).lower() in host:
        return False
    if "sockslover.net" in host and "cdn.shopify.com" not in host:
        return True
    return True


def file_create_from_url(store_domain: str, token: str, api_version: str, source_url: str, alt_text: str) -> str:
    mutation = """
    mutation FileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          id fileStatus alt createdAt
          ... on MediaImage { image { url } }
        }
        userErrors { field message }
      }
    }
    """
    variables = {"files": [{"originalSource": source_url, "contentType": "IMAGE", "alt": alt_text or "SOCKSLOVER product image"}]}
    data = run_gql(store_domain, token, api_version, mutation, variables)
    result = data["fileCreate"]
    if result.get("userErrors"):
        raise RuntimeError(f"fileCreate userErrors: {result['userErrors']}")
    files = result.get("files") or []
    if not files:
        raise RuntimeError("fileCreate succeeded but no file returned.")
    return files[0]["id"]


def get_media_image_url(store_domain: str, token: str, api_version: str, file_id: str) -> str:
    query = """
    query GetFile($id: ID!) {
      node(id: $id) {
        ... on MediaImage {
          id fileStatus image { url }
        }
      }
    }
    """
    last_status = None
    for _ in range(30):
        data = run_gql(store_domain, token, api_version, query, {"id": file_id})
        node = data.get("node")
        if node:
            last_status = node.get("fileStatus")
            image = node.get("image")
            if last_status == "READY" and image and image.get("url"):
                return image["url"]
            if last_status == "FAILED":
                raise RuntimeError(f"Shopify file processing failed. file_id={file_id}")
        time.sleep(5)
    raise RuntimeError(f"Shopify file not ready. file_id={file_id}, last_status={last_status}")


def replace_external_images(store_domain: str, token: str, api_version: str, description_html: str, product_url: str, external_domains: List[str], dry_run: bool, progress_area=None) -> Tuple[str, List[ImageReplacement], List[str]]:
    soup = BeautifulSoup(description_html or "", "html.parser")
    replacements, skipped, cache = [], [], {}
    image_tags = soup.find_all("img")
    if not image_tags:
        return str(soup), replacements, ["Body HTML 안에서 <img> 태그를 찾지 못했습니다."]

    for index, img in enumerate(image_tags, start=1):
        src = absolutize_url(img.get("src", ""), product_url)
        if not src:
            skipped.append(f"[{index}/{len(image_tags)}] src 없음")
            continue
        if not is_external_image_url(src, store_domain, external_domains):
            skipped.append(f"[{index}/{len(image_tags)}] Shopify CDN 또는 내부 이미지 스킵: {src}")
            continue

        alt_text = img.get("alt") or "SOCKSLOVER product image"
        if progress_area:
            progress_area.info(f"[{index}/{len(image_tags)}] 이미지 처리 중: {src}")

        try:
            if dry_run:
                new_url, status = f"SHOPIFY_CDN_URL_PREVIEW_{index}", "dry_run"
            else:
                if src not in cache:
                    file_id = file_create_from_url(store_domain, token, api_version, src, alt_text)
                    cache[src] = get_media_image_url(store_domain, token, api_version, file_id)
                new_url, status = cache[src], "uploaded"
            img["src"] = new_url
            img["alt"] = alt_text
            img["loading"] = "lazy"
            img["style"] = "max-width:100%;height:auto;margin:14px 0;border-radius:12px;"
            replacements.append(ImageReplacement(src, new_url, alt_text, status))
        except Exception as err:
            skipped.append(f"[{index}/{len(image_tags)}] 업로드 실패: {src} / {err}")

    return str(soup), replacements, skipped


def images_only_html(description_html: str, fallback_alt: str) -> str:
    soup = BeautifulSoup(description_html or "", "html.parser")
    out = []
    for i, img in enumerate(soup.find_all("img"), start=1):
        img["alt"] = img.get("alt") or f"{fallback_alt} 商品画像 {i}"
        img["loading"] = "lazy"
        img["style"] = "max-width:100%;height:auto;margin:14px 0;border-radius:12px;"
        out.append(str(img))
    return "\n".join(out)


def clean_text(text: str, max_len: int = 4000) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:max_len]


def detect_product_category(product: ProductData, facts: Optional[Dict[str, Any]] = None) -> str:
    """
    상품명 / product_type / tags / handle / 기존 설명을 기준으로
    상세페이지 섹션 구조를 분기하기 위한 상품군을 보수적으로 판정합니다.
    """
    parts = [
        product.title or "",
        product.product_type or "",
        " ".join(product.tags or []),
        product.handle or "",
    ]

    if facts:
        parts.append(json.dumps(facts.get("options", {}), ensure_ascii=False))
        parts.append(str(facts.get("description_facts", {}).get("visible_text", ""))[:1200])

    text = " ".join(parts).lower()

    if re.search(r"sock|socks|ソックス|靴下|くつ下|くつした|レッグ|タイツ|ストッキング", text):
        return "ソックス"
    if re.search(r"hat|cap|ハット|帽子|キャップ|バケットハット|サンバイザー|uv|日よけ|日除け", text):
        return "帽子"
    if re.search(r"pouch|ポーチ|化粧ポーチ|メイクポーチ|ケース|小物入れ", text):
        return "ポーチ"
    if re.search(r"bag|バッグ|カバン|鞄|ショルダー|クロスボディ|リュック|バックパック|トート|ボストン|巾着|サッチェル", text):
        return "バッグ"
    if re.search(r"hair|ヘア|アクセサリー|ピン|クリップ|シュシュ|カチューシャ|ヘアゴム|ヘアピン", text):
        return "アクセサリー"

    return "その他"


def category_defaults(category: str) -> Dict[str, Any]:
    """
    OpenAIが失敗した場合や古いJSON形式が返った場合に使うカテゴリ別の安全な初期値。
    ここで「必要なものがすっきり入る」を全商品共通にしない。
    """
    defaults = {
        "バッグ": {
            "main_section_title": "必要なものを持ち歩きやすい",
            "main_section_body": "スマホや財布など、外出時に持ち歩きたい小物をまとめやすいアイテムです。",
            "scene_points": ["近所へのお出かけ", "通勤・通学", "休日のショッピング", "旅行中のサブバッグ"],
            "why_points": ["日常使いしやすいデザイン", "コーディネートに合わせやすい", "お出かけに使いやすいサイズ感", "日本のお客様向けに送料無料でお届け"],
            "style_section_title": "どんなコーデにも合わせやすい",
            "color_section_title": "カラーバリエーション",
        },
        "ポーチ": {
            "main_section_title": "小物をすっきり整理",
            "main_section_body": "メイク用品や小物類をまとめたい時に使いやすいポーチです。",
            "scene_points": ["バッグの中の整理", "旅行用ポーチ", "メイク用品の収納", "デスク周りの小物整理"],
            "why_points": ["小物を整理しやすい", "持ち歩きやすいサイズ感", "日常使いしやすいデザイン", "日本のお客様向けに送料無料でお届け"],
            "style_section_title": "バッグの中にもなじみやすい",
            "color_section_title": "カラーバリエーション",
        },
        "ソックス": {
            "main_section_title": "足元に取り入れやすいデザイン",
            "main_section_body": "毎日のコーディネートに合わせやすく、足元の印象をさりげなく変えたい時におすすめです。",
            "scene_points": ["普段のお出かけ", "カジュアルコーデ", "スニーカー合わせ", "季節の足元コーデ"],
            "why_points": ["足元のアクセントに使いやすい", "デイリーコーデに合わせやすい", "色違いで選びやすい", "日本のお客様向けに送料無料でお届け"],
            "style_section_title": "足元コーデに合わせやすい",
            "color_section_title": "カラー・デザイン",
        },
        "帽子": {
            "main_section_title": "日差しが気になる日のお出かけに",
            "main_section_body": "外出時の日差し対策や、コーディネートのアクセントとして取り入れやすい帽子です。",
            "scene_points": ["近所へのお出かけ", "旅行や散歩", "屋外イベント", "カジュアルコーデ"],
            "why_points": ["日差しが気になる日に使いやすい", "外出時に取り入れやすい", "コーディネートに合わせやすい", "日本のお客様向けに送料無料でお届け"],
            "style_section_title": "外出コーデに合わせやすい",
            "color_section_title": "カラーバリエーション",
        },
        "アクセサリー": {
            "main_section_title": "さりげなく印象を変えるアクセント",
            "main_section_body": "いつものスタイルに取り入れやすく、手軽に雰囲気を変えたい時におすすめです。",
            "scene_points": ["毎日のヘアアレンジ", "お出かけ前の身支度", "カジュアルスタイル", "ギフトにも"],
            "why_points": ["手軽に使いやすい", "コーディネートのアクセントになる", "日常使いしやすい", "日本のお客様向けに送料無料でお届け"],
            "style_section_title": "いつものスタイルに合わせやすい",
            "color_section_title": "デザイン・カラー",
        },
        "その他": {
            "main_section_title": "毎日に取り入れやすいアイテム",
            "main_section_body": "日常のコーディネートやお出かけに取り入れやすい、使いやすさを意識したアイテムです。",
            "scene_points": ["普段のお出かけ", "休日のコーディネート", "旅行や外出", "ギフトにも"],
            "why_points": ["日常使いしやすい", "シンプルで合わせやすい", "幅広いシーンで使いやすい", "日本のお客様向けに送料無料でお届け"],
            "style_section_title": "コーディネートに合わせやすい",
            "color_section_title": "バリエーション",
        },
    }
    return defaults.get(category, defaults["その他"])


def normalize_ai_copy(copy: Dict[str, Any], product: ProductData, facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    OpenAIレスポンスと旧形式JSONの互換性を吸収します。
    - 旧: storage_text
    - 新: main_section_title / main_section_body
    """
    category = copy.get("detected_category") or facts.get("detected_category") or detect_product_category(product, facts)
    defaults = category_defaults(category)
    regex = facts.get("description_facts", {}).get("regex_specs", {}) or {}
    colors = ", ".join(facts.get("colors_detected") or []) or "商品ページをご確認ください"

    normalized = dict(copy or {})
    normalized["detected_category"] = category
    normalized["lead_text"] = normalized.get("lead_text") or f"{product.title}は、日常のコーディネートに取り入れやすい{category}アイテムです。"
    normalized["why_points"] = normalized.get("why_points") or defaults["why_points"]
    normalized["main_section_title"] = normalized.get("main_section_title") or defaults["main_section_title"]
    normalized["main_section_body"] = normalized.get("main_section_body") or normalized.get("storage_text") or defaults["main_section_body"]
    normalized["scene_points"] = normalized.get("scene_points") or defaults["scene_points"]
    normalized["style_section_title"] = normalized.get("style_section_title") or defaults["style_section_title"]
    normalized["style_text"] = normalized.get("style_text") or "主張しすぎないデザインで、日常のスタイルに取り入れやすいアイテムです。"
    normalized["color_section_title"] = normalized.get("color_section_title") or defaults["color_section_title"]
    normalized["color_text"] = normalized.get("color_text") or ("カラー情報は商品オプションをご確認ください。" if colors == "商品ページをご確認ください" else colors)

    specs = normalized.get("specs") or {}
    normalized["specs"] = {
        "category": specs.get("category") or product.product_type or category,
        "material": specs.get("material") or regex.get("material") or "商品ページをご確認ください",
        "size": specs.get("size") or regex.get("size") or "商品ページをご確認ください",
        "weight": specs.get("weight") or regex.get("weight") or "商品ページをご確認ください",
        "closure": specs.get("closure") or "商品ページをご確認ください",
        "pocket": specs.get("pocket") or "商品ページをご確認ください",
        "colors": specs.get("colors") or colors,
    }

    faq = normalized.get("faq") or []
    if len(faq) < 3:
        faq = faq + [
            {"q": "普段使いしやすいですか？", "a": "はい。日常のお出かけやコーディネートに取り入れやすいアイテムとしておすすめです。"},
            {"q": "日本まで配送されますか？", "a": "はい。SOCKSLOVERは日本のお客様向けに運営しており、全商品送料無料でお届けしています。"},
            {"q": "返品や返金はできますか？", "a": "商品に不良があった場合は、返品・返金ポリシーに基づき対応いたします。"},
        ]
    normalized["faq"] = faq[:3]

    return normalized


def extract_facts_from_description(description_html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(description_html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible_text = clean_text(soup.get_text(" ", strip=True), 3500)

    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [clean_text(c.get_text(" ", strip=True), 200) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)

    lists = []
    for ul in soup.find_all(["ul", "ol"]):
        items = [clean_text(li.get_text(" ", strip=True), 200) for li in ul.find_all("li")]
        if items:
            lists.append(items)

    img_alts = [clean_text(img.get("alt", ""), 200) for img in soup.find_all("img") if img.get("alt")]

    specs = {}
    patterns = {
        "size": r"((?:約)?\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?\s*(?:cm|CM|センチ))",
        "material": r"(PU|合成皮革|フェイクレザー|ポリエステル|ナイロン|コットン|綿|レザー|本革)",
        "weight": r"((?:約)?\d+\s*(?:g|kg|グラム))",
    }
    for key, pat in patterns.items():
        m = re.search(pat, visible_text, re.IGNORECASE)
        if m:
            specs[key] = m.group(1)

    return {"visible_text": visible_text, "tables": tables[:5], "lists": lists[:8], "image_alt_texts": img_alts[:30], "regex_specs": specs}


def extract_product_facts(product: ProductData, description_facts: Dict[str, Any]) -> Dict[str, Any]:
    option_map = {opt.get("name"): opt.get("values", []) for opt in product.options}
    prices = sorted({v.get("price") for v in product.variants if v.get("price")})
    colors = []
    for opt in product.options:
        name = (opt.get("name") or "").lower()
        if name in ["color", "colour", "カラー", "色"]:
            colors = opt.get("values") or []
    if not colors:
        for tag in product.tags:
            if re.search(r"black|brown|khaki|coffee|white|beige|gray|grey|pink|blue|green|red|yellow|purple|orange|ブラック|ブラウン|カーキ|ホワイト|ベージュ|グレー", tag, re.I):
                colors.append(tag)

    facts = {
        "title": product.title,
        "handle": product.handle,
        "vendor": product.vendor,
        "product_type": product.product_type,
        "tags": product.tags,
        "options": option_map,
        "variant_titles": [v.get("title") for v in product.variants if v.get("title")][:80],
        "prices": prices,
        "skus": [v.get("sku") for v in product.variants if v.get("sku")][:20],
        "colors_detected": list(dict.fromkeys(colors)),
        "image_count": len(product.images),
        "product_images": [{"url": img.get("url"), "altText": img.get("altText"), "width": img.get("width"), "height": img.get("height")} for img in product.images[:20]],
        "description_facts": description_facts,
    }
    facts["detected_category"] = detect_product_category(product, facts)
    return facts


def call_openai_json(api_key: str, model: str, product_facts: Dict[str, Any], language: str = "Japanese") -> Dict[str, Any]:
    if OpenAI is None:
        raise RuntimeError("openai package가 설치되어 있지 않습니다. requirements.txt에 openai를 추가해주세요.")
    if not api_key:
        raise RuntimeError("OpenAI API key가 없습니다.")

    client = OpenAI(api_key=api_key)
    system = """
You are a Japanese ecommerce copywriter for SOCKSLOVER.
Write product page copy ONLY from confirmed facts in the provided JSON.
Do not invent unconfirmed facts such as exact capacity, waterproofness, durability, weight, material, dimensions, delivery dates, UV cut rate, medical effects, official certifications, or brand origin.
If a fact is unknown, write neutral copy or use "商品ページをご確認ください" for spec fields only.

Very important:
- Generate category-aware copy and section titles.
- Do NOT use bag/pouch storage titles such as "必要なものがすっきり入る" for socks, hats, hair accessories, or unrelated items.
- For socks, use a foot/style/wearing-comfort related section title.
- For hats, use a sun/outdoor/styling related section title.
- For pouches, storage/organization is acceptable.
- For bags, carry/storage is acceptable.
- Avoid "商品ページをご確認ください" in marketing body copy. Use it only in spec fields when truly unknown.
- Output valid JSON only.
"""
    user = {
        "task": "Generate GMC-safe Japanese Shopify product page copy based only on confirmed product facts.",
        "required_json_schema": {
            "detected_category": "バッグ | ポーチ | ソックス | 帽子 | アクセサリー | その他",
            "lead_text": "string",
            "why_points": ["4 short bullet strings"],
            "main_section_title": "category-aware section title. Do not use storage title for socks/hats.",
            "main_section_body": "string",
            "scene_points": ["4 short scene strings"],
            "style_section_title": "string",
            "style_text": "string",
            "color_section_title": "string",
            "color_text": "string",
            "specs": {"category": "string", "material": "string", "size": "string", "weight": "string", "closure": "string", "pocket": "string", "colors": "string"},
            "faq": [{"q": "string", "a": "string"}, {"q": "string", "a": "string"}, {"q": "string", "a": "string"}],
        },
        "product_facts": product_facts,
        "language": language,
    }
    response = client.chat.completions.create(
        model=model,
        temperature=0.4,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system.strip()}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
    )
    return json.loads(response.choices[0].message.content)


def fallback_copy(product: ProductData, facts: Dict[str, Any]) -> Dict[str, Any]:
    category = facts.get("detected_category") or detect_product_category(product, facts)
    defaults = category_defaults(category)
    regex = facts.get("description_facts", {}).get("regex_specs", {}) or {}
    colors = ", ".join(facts.get("colors_detected") or []) or "商品ページをご確認ください"
    product_type = product.product_type or category

    return normalize_ai_copy({
        "detected_category": category,
        "lead_text": f"{product.title}は、日常のコーディネートに取り入れやすい{category}アイテムです。確認できる商品情報をもとに、使いやすさを重視してご紹介します。",
        "why_points": defaults["why_points"],
        "main_section_title": defaults["main_section_title"],
        "main_section_body": defaults["main_section_body"],
        "scene_points": defaults["scene_points"],
        "style_section_title": defaults["style_section_title"],
        "style_text": "主張しすぎないデザインで、日常のスタイルに取り入れやすいアイテムです。",
        "color_section_title": defaults["color_section_title"],
        "color_text": colors if colors != "商品ページをご確認ください" else "カラー情報は商品オプションをご確認ください。",
        "specs": {
            "category": product_type,
            "material": regex.get("material", "商品ページをご確認ください"),
            "size": regex.get("size", "商品ページをご確認ください"),
            "weight": regex.get("weight", "商品ページをご確認ください"),
            "closure": "商品ページをご確認ください",
            "pocket": "商品ページをご確認ください",
            "colors": colors,
        },
        "faq": [
            {"q": "普段使いしやすいですか？", "a": "はい。日常のお出かけやコーディネートに取り入れやすいアイテムとしておすすめです。"},
            {"q": "日本まで配送されますか？", "a": "はい。SOCKSLOVERは日本のお客様向けに運営しており、全商品送料無料でお届けしています。"},
            {"q": "返品や返金はできますか？", "a": "商品に不良があった場合は、返品・返金ポリシーに基づき対応いたします。"},
        ],
    }, product, facts)


def lines_to_li(items: List[str]) -> str:
    return "\n".join(f"<li>{e(item)}</li>" for item in (items or []) if str(item).strip())


def scene_grid(items: List[str]) -> str:
    return "".join(f"<li style='background:#f8f6f3;border-radius:12px;padding:12px 14px;'>{e(item)}</li>" for item in (items or []) if str(item).strip())


def build_sockslover_style_html(product: ProductData, copy: Dict[str, Any], image_html: str, shipping_text: str, return_text: str, facts: Optional[Dict[str, Any]] = None) -> str:
    copy = normalize_ai_copy(copy or {}, product, facts or {})
    specs = copy.get("specs", {}) or {}
    faq = copy.get("faq", []) or []
    while len(faq) < 3:
        faq.append({"q": "詳しく確認できますか？", "a": "商品ページの情報をご確認ください。"})
    image_block = image_html or "<p>商品画像は商品ギャラリーをご確認ください。</p>"

    return f"""
<div class="sl-detail" style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#333;line-height:1.8;">
  <section style="padding:26px 22px;background:#faf7f2;border-radius:18px;margin:24px 0;">
    <p style="font-size:13px;letter-spacing:.08em;color:#8a7562;margin:0 0 8px;">SOCKSLOVER SELECT</p>
    <h2 style="font-size:24px;line-height:1.45;margin:0 0 12px;color:#2f2925;">{e(product.title)}</h2>
    <p style="font-size:15px;margin:0;">{e(copy.get('lead_text'))}</p>
  </section>
  <section style="margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">選ばれる理由</h3>
    <ul style="padding-left:1.2em;margin:0;">{lines_to_li(copy.get("why_points", []))}</ul>
  </section>
  <section style="padding:22px;border:1px solid #eadfd5;border-radius:16px;margin:30px 0;background:#fff;">
    <h3 style="font-size:20px;margin:0 0 10px;">{e(copy.get('main_section_title'))}</h3>
    <p style="margin:0;">{e(copy.get('main_section_body'))}</p>
  </section>
  <section style="margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">こんなシーンにおすすめ</h3>
    <ul style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;list-style:none;padding:0;margin:0;">{scene_grid(copy.get("scene_points", []))}</ul>
  </section>
  <section style="padding:22px;background:#f7f2eb;border-radius:16px;margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 10px;">{e(copy.get('style_section_title'))}</h3>
    <p style="margin:0;">{e(copy.get('style_text'))}</p>
  </section>
  <section style="margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 10px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">{e(copy.get('color_section_title'))}</h3>
    <p style="margin:0;">{e(copy.get('color_text'))}</p>
  </section>
  <section style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">商品イメージ</h3>
    <div style="text-align:center;">{image_block}</div>
  </section>
  <section style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">製品情報</h3>
    <table style="width:100%;border-collapse:collapse;font-size:14px;"><tbody>
      {''.join(f'<tr><th style="width:34%;text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">{e(label)}</th><td style="padding:12px;border:1px solid #eadfd5;">{e(value)}</td></tr>' for label, value in [
        ("商品名", product.title), ("カテゴリー", specs.get("category")), ("素材", specs.get("material")), ("サイズ", specs.get("size")), ("重さ", specs.get("weight")), ("開閉", specs.get("closure")), ("ポケット", specs.get("pocket")), ("カラー", specs.get("colors"))
      ])}
    </tbody></table>
  </section>
  <section style="padding:22px;border-radius:16px;background:#fbfaf8;border:1px solid #eadfd5;margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 12px;">安心してご購入いただくために</h3>
    <p style="margin:0 0 10px;">SOCKSLOVERは日本のお客様向けに運営しているオンラインストアです。</p>
    <p style="margin:0;">配送期間・返品条件・お問い合わせ先を明記し、安心してお買い物いただけるよう努めています。</p>
  </section>
  <section style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 10px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">配送・返品について</h3>
    <p style="margin:0 0 8px;"><strong>配送：</strong>{e(shipping_text)}</p>
    <p style="margin:0;"><strong>返品・返金：</strong>{e(return_text)}</p>
  </section>
  <section style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">よくあるご質問</h3>
    {''.join(f'<div style="border-bottom:1px solid #eee;padding:12px 0;"><p style="font-weight:600;margin:0 0 6px;">Q. {e(item.get("q"))}</p><p style="margin:0;">A. {e(item.get("a"))}</p></div>' for item in faq[:3])}
  </section>
</div>
""".strip()


def render_sidebar():
    st.sidebar.header("Shopify Settings")
    store_domain = st.sidebar.text_input("Store domain", value=get_secret_or_env("SHOPIFY_STORE_DOMAIN", "sockslover-net.myshopify.com"))
    token = st.sidebar.text_input("Admin API access token", value=get_secret_or_env("SHOPIFY_ADMIN_API_TOKEN", ""), type="password")
    api_version = st.sidebar.text_input("Admin API version", value=normalize_api_version(get_secret_or_env("SHOPIFY_API_VERSION", DEFAULT_API_VERSION)))

    st.sidebar.divider()
    st.sidebar.header("OpenAI Settings")
    openai_api_key = st.sidebar.text_input("OpenAI API key", value=get_secret_or_env("OPENAI_API_KEY", ""), type="password")
    openai_model = st.sidebar.text_input("OpenAI model", value=get_secret_or_env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL))

    st.sidebar.divider()
    st.sidebar.header("Image Detection")
    external_domains_text = st.sidebar.text_area("외부 이미지 도메인 키워드", value="\n".join(DEFAULT_EXTERNAL_DOMAINS), height=160)
    external_domains = [line.strip() for line in external_domains_text.splitlines() if line.strip()]
    return normalize_store_domain(store_domain), token.strip(), normalize_api_version(api_version), openai_api_key.strip(), openai_model.strip(), external_domains


def main():
    st.title("🛍️ Shopify GMC Optimizer + AI Copy")
    st.caption("상품 URL을 입력하면 Shopify에서 확인 가능한 데이터만 추출하고, AI가 상품군별 일본어 상세페이지 문구를 생성합니다.")

    store_domain, token, api_version, openai_api_key, openai_model, external_domains = render_sidebar()

    with st.expander("사용 전 체크", expanded=False):
        st.markdown("""
- Shopify 권한: `read_products`, `write_products`, `read_files`, `write_files`
- AI 문구 생성에는 `OPENAI_API_KEY`가 필요합니다.
- AI는 Shopify에서 확인 가능한 데이터만 근거로 작성하도록 제한되어 있습니다.
- 처음에는 반드시 `Dry Run`으로 테스트하세요.
        """)

    product_url = st.text_input("Shopify 상품 URL", placeholder="https://sockslover.net/products/...")
    col1, col2, _ = st.columns([1, 1, 2])
    with col1:
        dry_run = st.toggle("Dry Run", value=True)
    with col2:
        fetch_button = st.button("상품 불러오기", type="primary")

    for key, default in {"product": None, "product_url": "", "facts": None, "ai_copy": None}.items():
        if key not in st.session_state:
            st.session_state[key] = default

    if fetch_button:
        if not store_domain or not token:
            st.error("Store domain과 Admin API token을 입력해주세요.")
            return
        try:
            handle = extract_handle_from_product_url(product_url)
            with st.spinner("Shopify에서 상품 데이터를 불러오는 중..."):
                product = get_product_by_handle(store_domain, token, api_version, handle)
                desc_facts = extract_facts_from_description(product.description_html)
                facts = extract_product_facts(product, desc_facts)
            st.session_state.product = product
            st.session_state.product_url = product_url
            st.session_state.facts = facts
            st.session_state.ai_copy = None
            st.success(f"상품을 찾았습니다: {product.title}")
            st.info(f"자동 판정 카테고리: {facts.get('detected_category')}")
        except Exception as err:
            st.error(f"상품 조회 실패: {err}")
            return

    product = st.session_state.product
    facts = st.session_state.facts

    if not product:
        st.info("먼저 상품 URL을 입력하고 상품을 불러와주세요.")
        return

    st.divider()
    left, right = st.columns([1, 1])
    with left:
        st.subheader("확인된 Shopify 데이터")
        st.write(f"**Title:** {product.title}")
        st.write(f"**Detected Category:** {facts.get('detected_category') if facts else '-'}")
        st.write(f"**Product Type:** {product.product_type or '-'}")
        st.write(f"**Vendor:** {product.vendor or '-'}")
        st.write(f"**Tags:** {', '.join(product.tags) if product.tags else '-'}")
        st.write(f"**Options:** {json.dumps({o.get('name'): o.get('values') for o in product.options}, ensure_ascii=False)}")
        st.write(f"**Variants:** {len(product.variants)}")
        st.write(f"**Product Images:** {len(product.images)}")
    with right:
        st.subheader("추출된 사실 데이터")
        st.json(facts)

    st.divider()
    st.subheader("AI 문구 생성")
    gen_col1, gen_col2 = st.columns([1, 2])
    with gen_col1:
        gen_button = st.button("AI로 상세 문구 생성", type="primary")
    with gen_col2:
        use_fallback = st.checkbox("OpenAI 실패 시 안전한 fallback 문구 사용", value=True)

    if gen_button:
        try:
            with st.spinner("AI가 상품별 상세페이지 문구를 작성하는 중..."):
                raw_copy = call_openai_json(openai_api_key, openai_model, facts)
                st.session_state.ai_copy = normalize_ai_copy(raw_copy, product, facts)
            st.success("AI 문구 생성 완료")
        except Exception as err:
            if use_fallback:
                st.warning(f"AI 생성 실패. fallback 문구를 사용합니다: {err}")
                st.session_state.ai_copy = fallback_copy(product, facts)
            else:
                st.error(f"AI 생성 실패: {err}")

    if st.session_state.ai_copy:
        st.subheader("AI 생성 문구 Preview")
        st.json(st.session_state.ai_copy)
        with st.expander("AI 문구 직접 수정"):
            ai_copy_text = st.text_area("JSON 수정", value=json.dumps(st.session_state.ai_copy, ensure_ascii=False, indent=2), height=420)
            if st.button("수정한 JSON 적용"):
                try:
                    st.session_state.ai_copy = normalize_ai_copy(json.loads(ai_copy_text), product, facts)
                    st.success("수정한 JSON을 적용했습니다.")
                except Exception as err:
                    st.error(f"JSON 파싱 실패: {err}")

    st.divider()
    st.subheader("배송/반품 공통 문구")
    shipping_text = st.text_area("配送説明", value="SOCKSLOVERでは全商品送料無料でお届けしています。ご注文後、通常2〜4営業日以内に発送準備を行い、発送後7〜14営業日前後でお届けします。", height=80)
    return_text = st.text_area("返品・返金説明", value="商品に不良があった場合は、返金または再送にて対応いたします。詳細は返品・返金ポリシーをご確認ください。", height=80)

    st.divider()
    confirm = st.checkbox("현재 상품의 Body HTML을 AI 생성 상세페이지 구조로 교체하는 것을 이해했습니다.", value=False)
    run_button = st.button("Dry Run 실행" if dry_run else "실제 업데이트 실행", type="primary", disabled=not confirm or not bool(st.session_state.ai_copy))

    if run_button:
        progress_area = st.empty()
        try:
            with st.spinner("이미지 CDN 교체 및 HTML 생성 중..."):
                replaced_html, replacements, skipped = replace_external_images(
                    store_domain=store_domain,
                    token=token,
                    api_version=api_version,
                    description_html=product.description_html,
                    product_url=st.session_state.product_url,
                    external_domains=external_domains,
                    dry_run=dry_run,
                    progress_area=progress_area,
                )
                image_html = images_only_html(replaced_html, product.title)
                new_html = build_sockslover_style_html(product, st.session_state.ai_copy, image_html, shipping_text, return_text, facts)

            st.subheader("이미지 처리 결과")
            if replacements:
                st.dataframe([r.__dict__ for r in replacements], use_container_width=True)
            if skipped:
                with st.expander(f"스킵/실패 로그 {len(skipped)}개"):
                    for s in skipped:
                        st.write("- " + s)

            st.subheader("생성된 Body HTML")
            st.code(new_html, language="html")
            st.download_button("HTML 다운로드", new_html, file_name=f"{product.handle}_body_html.html", mime="text/html")

            if dry_run:
                st.info("Dry Run이므로 Shopify에는 반영하지 않았습니다.")
            else:
                with st.spinner("Shopify 상품 설명 업데이트 중..."):
                    updated = update_product_description(store_domain, token, api_version, product.id, new_html)
                st.success("Shopify 상품 설명 업데이트 완료")
                st.json(updated)
                refreshed = get_product_by_handle(store_domain, token, api_version, product.handle)
                st.session_state.product = refreshed
                st.session_state.facts = extract_product_facts(refreshed, extract_facts_from_description(refreshed.description_html))
        except Exception as err:
            st.error(f"실행 실패: {err}")

    st.divider()
    st.caption("Shopify GMC Optimizer + AI Copy / Built for SOCKSLOVER & Ion Labs")


if __name__ == "__main__":
    main()
