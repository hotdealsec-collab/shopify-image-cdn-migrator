import os
import re
import time
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
import streamlit as st
from bs4 import BeautifulSoup


# =========================================================
# Shopify GMC Optimizer - SOCKSLOVER Style
# =========================================================

st.set_page_config(
    page_title="Shopify GMC Optimizer",
    page_icon="🛍️",
    layout="wide",
)

DEFAULT_API_VERSION = "2026-01"

DEFAULT_EXTERNAL_DOMAINS = [
    "cjdropshipping.com",
    "cjpacket.com",
    "alicdn.com",
    "aliexpress",
    "ae01.alicdn.com",
    "cf.cjdropshipping.com",
    "imgaz.staticbg.com",
    "banggood.com",
    "dhresource.com",
    "shein.com",
    "temu.com",
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
    vendor: Optional[str] = None
    product_type: Optional[str] = None


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
    store_domain = (store_domain or "").strip()
    store_domain = store_domain.replace("https://", "").replace("http://", "")
    return store_domain.strip("/")


def normalize_api_version(api_version: str) -> str:
    api_version = (api_version or DEFAULT_API_VERSION).strip()
    if not re.match(r"^\d{4}-\d{2}$", api_version):
        return DEFAULT_API_VERSION
    return api_version


def extract_handle_from_product_url(product_url: str) -> str:
    product_url = (product_url or "").strip()
    if not product_url:
        raise ValueError("상품 URL을 입력해주세요.")

    parsed = urllib.parse.urlparse(product_url)
    path = parsed.path.strip("/")
    parts = path.split("/")

    if "products" not in parts:
        raise ValueError("URL 안에 /products/ 경로가 없습니다.")

    idx = parts.index("products")
    if idx + 1 >= len(parts):
        raise ValueError("상품 handle을 URL에서 찾을 수 없습니다.")

    return urllib.parse.unquote(parts[idx + 1])


def e(value: str) -> str:
    return html.escape(value or "", quote=True)


def split_lines(value: str) -> List[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def ul_from_lines(value: str, fallback: List[str]) -> str:
    lines = split_lines(value) or fallback
    return "\n".join(f"<li>{e(line)}</li>" for line in lines)


def run_gql(
    store_domain: str,
    token: str,
    api_version: str,
    query: str,
    variables: Optional[dict] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    store_domain = normalize_store_domain(store_domain)
    api_version = normalize_api_version(api_version)
    endpoint = f"https://{store_domain}/admin/api/{api_version}/graphql.json"

    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
            )

            if res.status_code == 401:
                raise RuntimeError("401 Unauthorized: Shopify Admin API token이 올바르지 않거나 권한이 부족합니다.")
            if res.status_code == 403:
                raise RuntimeError("403 Forbidden: 앱 권한(read/write products/files)을 확인해주세요.")
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
            if "data" not in data:
                raise RuntimeError(f"Invalid Shopify response: {data}")

            return data["data"]

        except requests.exceptions.ConnectTimeout as err:
            last_error = err
            time.sleep(min(5 * attempt, 30))
        except requests.exceptions.ReadTimeout as err:
            last_error = err
            time.sleep(min(5 * attempt, 30))
        except requests.exceptions.ConnectionError as err:
            last_error = err
            time.sleep(min(5 * attempt, 30))
        except requests.exceptions.HTTPError as err:
            body = ""
            try:
                body = res.text
            except Exception:
                pass
            raise RuntimeError(f"Shopify API HTTP error: {err}. Response: {body}")

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
    )


def update_product_description(store_domain: str, token: str, api_version: str, product_id: str, new_description_html: str) -> dict:
    mutation = """
    mutation ProductUpdate($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product { id title handle }
        userErrors { field message }
      }
    }
    """
    variables = {"product": {"id": product_id, "descriptionHtml": new_description_html}}
    data = run_gql(store_domain, token, api_version, mutation, variables)
    result = data["productUpdate"]
    errors = result.get("userErrors", [])
    if errors:
        raise RuntimeError(f"productUpdate userErrors: {errors}")
    return result["product"]


def file_create_from_url(store_domain: str, token: str, api_version: str, source_url: str, alt_text: str) -> str:
    mutation = """
    mutation FileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          id
          fileStatus
          alt
          createdAt
          ... on MediaImage { image { url } }
        }
        userErrors { field message }
      }
    }
    """
    variables = {"files": [{"originalSource": source_url, "contentType": "IMAGE", "alt": alt_text or "SOCKSLOVER product image"}]}
    data = run_gql(store_domain, token, api_version, mutation, variables)
    result = data["fileCreate"]
    errors = result.get("userErrors", [])
    if errors:
        raise RuntimeError(f"fileCreate userErrors: {errors}")
    files = result.get("files") or []
    if not files:
        raise RuntimeError("fileCreate succeeded but no file returned.")
    return files[0]["id"]


def get_media_image_url(store_domain: str, token: str, api_version: str, file_id: str, max_checks: int = 30, sleep_sec: int = 5) -> str:
    query = """
    query GetFile($id: ID!) {
      node(id: $id) {
        ... on MediaImage {
          id
          fileStatus
          image { url }
        }
      }
    }
    """
    last_status = None
    for _ in range(max_checks):
        data = run_gql(store_domain, token, api_version, query, {"id": file_id})
        node = data.get("node")
        if not node:
            time.sleep(sleep_sec)
            continue
        last_status = node.get("fileStatus")
        image = node.get("image")
        if last_status == "READY" and image and image.get("url"):
            return image["url"]
        if last_status == "FAILED":
            raise RuntimeError(f"Shopify file processing failed. file_id={file_id}")
        time.sleep(sleep_sec)
    raise RuntimeError(f"Shopify file not ready. file_id={file_id}, last_status={last_status}")


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
        base = f"{parsed.scheme}://{parsed.netloc}"
        return urllib.parse.urljoin(base, src)
    return src


def is_shopify_cdn_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return any(marker in lower for marker in SHOPIFY_CDN_MARKERS)


def is_data_or_placeholder_image(url: str) -> bool:
    if not url:
        return True
    lower = url.lower().strip()
    return lower.startswith(("data:", "blob:", "#", "javascript:"))


def is_external_image_url(url: str, store_domain: str, external_domains: List[str]) -> bool:
    if not url or is_data_or_placeholder_image(url):
        return False
    if is_shopify_cdn_url(url):
        return False

    lower = url.lower()
    store_domain = normalize_store_domain(store_domain).lower()

    if any(domain.strip().lower() in lower for domain in external_domains if domain.strip()):
        return True

    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False

    if not host:
        return False
    if store_domain and store_domain in host:
        return False
    if "sockslover.net" in host and "cdn.shopify.com" not in host:
        return True
    return True


def extract_image_tags(description_html: str) -> List[dict]:
    soup = BeautifulSoup(description_html or "", "html.parser")
    return [{"src": img.get("src", ""), "alt": img.get("alt", "")} for img in soup.find_all("img")]


def replace_external_images(store_domain: str, token: str, api_version: str, description_html: str, product_url: str, external_domains: List[str], dry_run: bool, progress_area=None) -> Tuple[str, List[ImageReplacement], List[str]]:
    soup = BeautifulSoup(description_html or "", "html.parser")
    replacements: List[ImageReplacement] = []
    skipped: List[str] = []
    cache: Dict[str, str] = {}
    image_tags = soup.find_all("img")

    if not image_tags:
        return str(soup), replacements, ["Body HTML 안에서 <img> 태그를 찾지 못했습니다."]

    total = len(image_tags)
    migrated_count = 0

    for index, img in enumerate(image_tags, start=1):
        src = absolutize_url(img.get("src", ""), product_url=product_url)
        if not src:
            skipped.append(f"[{index}/{total}] src가 없는 이미지 스킵")
            continue
        if not is_external_image_url(src, store_domain, external_domains):
            skipped.append(f"[{index}/{total}] Shopify CDN 또는 내부 이미지로 판단되어 스킵: {src}")
            continue

        alt_text = img.get("alt") or "SOCKSLOVER product image"
        if progress_area:
            progress_area.info(f"[{index}/{total}] 이미지 처리 중: {src}")

        try:
            if dry_run:
                new_url = f"SHOPIFY_CDN_URL_PREVIEW_{index}"
                status = "dry_run"
            else:
                if src in cache:
                    new_url = cache[src]
                else:
                    file_id = file_create_from_url(store_domain, token, api_version, src, alt_text)
                    new_url = get_media_image_url(store_domain, token, api_version, file_id, max_checks=30, sleep_sec=5)
                    cache[src] = new_url
                status = "uploaded"

            img["src"] = new_url
            img["alt"] = alt_text
            img["loading"] = "lazy"
            img["style"] = "max-width:100%;height:auto;margin:14px 0;border-radius:12px;"
            replacements.append(ImageReplacement(src, new_url, alt_text, status))
            migrated_count += 1
        except Exception as err:
            skipped.append(f"[{index}/{total}] 업로드 실패: {src} / {err}")
            continue

    if progress_area:
        progress_area.success(f"이미지 처리 완료: 교체 {migrated_count}개 / 전체 {total}개")
    return str(soup), replacements, skipped


def images_only_html(description_html: str, fallback_alt: str) -> str:
    soup = BeautifulSoup(description_html or "", "html.parser")
    imgs = soup.find_all("img")
    if not imgs:
        return ""
    image_html_list = []
    for i, img in enumerate(imgs, start=1):
        if not img.get("alt"):
            img["alt"] = f"{fallback_alt} 商品画像 {i}"
        img["loading"] = "lazy"
        img["style"] = "max-width:100%;height:auto;margin:14px 0;border-radius:12px;"
        image_html_list.append(str(img))
    return "\n".join(image_html_list)


def build_sockslover_style_html(
    product_title: str,
    product_type: str,
    image_html: str,
    lead_text: str,
    why_points: str,
    storage_text: str,
    scene_points: str,
    style_text: str,
    color_text: str,
    material: str,
    size_text: str,
    weight_text: str,
    closure_text: str,
    pocket_text: str,
    shipping_text: str,
    return_text: str,
    faq_1_q: str,
    faq_1_a: str,
    faq_2_q: str,
    faq_2_a: str,
    faq_3_q: str,
    faq_3_a: str,
) -> str:
    title = e(product_title)
    product_type_safe = e(product_type or "ファッション雑貨")
    fallback_why = [
        "毎日使いやすいシンプルなデザイン",
        "必要なものをすっきり持ち歩ける実用的なサイズ感",
        "カジュアルにもきれいめにも合わせやすい",
        "通勤・お出かけ・旅行のサブバッグとして使いやすい",
    ]
    fallback_scene = ["近所へのお出かけ", "通勤・通学", "休日のショッピング", "旅行中のサブバッグ"]

    lead = lead_text.strip() or f"{product_title}は、毎日の外出に取り入れやすいシンプルなデザインのアイテムです。必要なものを持ち歩きやすく、普段使いから旅行まで幅広いシーンで活躍します。"
    storage = storage_text.strip() or "スマホ、財布、キーケース、ミニポーチなど、外出時に必要な小物をすっきり収納しやすいサイズ感です。"
    style = style_text.strip() or "主張しすぎないデザインなので、デニムやワンピース、ジャケットスタイルなど幅広いコーディネートに合わせやすいです。"
    colors = color_text.strip() or "ベーシックカラーを中心に、日常コーデに取り入れやすいカラー展開です。"
    shipping = shipping_text.strip() or "SOCKSLOVERでは全商品送料無料でお届けしています。通常、発送後7〜14営業日前後でお届けします。"
    returns = return_text.strip() or "商品に不良があった場合は、返金または再送にて対応いたします。詳細は返品・返金ポリシーをご確認ください。"
    faq1q = faq_1_q.strip() or "普段使いしやすいサイズですか？"
    faq1a = faq_1_a.strip() or "はい。スマホや財布、小物を持ち歩く日常使いにおすすめのサイズ感です。"
    faq2q = faq_2_q.strip() or "日本まで配送されますか？"
    faq2a = faq_2_a.strip() or "はい。SOCKSLOVERは日本のお客様向けに運営しており、全商品送料無料でお届けしています。"
    faq3q = faq_3_q.strip() or "返品や返金はできますか？"
    faq3a = faq_3_a.strip() or "商品に不良があった場合は、返品・返金ポリシーに基づき対応いたします。"
    image_block = image_html or "<p>商品画像は商品ギャラリーをご確認ください。</p>"
    scene_items = "".join(f"<li style='background:#f8f6f3; border-radius:12px; padding:12px 14px;'>{e(line)}</li>" for line in (split_lines(scene_points) or fallback_scene))

    return f"""
<div class="sl-detail" style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#333;line-height:1.8;">
  <section class="sl-hero" style="padding:26px 22px;background:#faf7f2;border-radius:18px;margin:24px 0;">
    <p style="font-size:13px;letter-spacing:.08em;color:#8a7562;margin:0 0 8px;">SOCKSLOVER SELECT</p>
    <h2 style="font-size:24px;line-height:1.45;margin:0 0 12px;color:#2f2925;">{title}</h2>
    <p style="font-size:15px;margin:0;">{e(lead)}</p>
  </section>

  <section class="sl-why" style="margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">選ばれる理由</h3>
    <ul style="padding-left:1.2em;margin:0;">{ul_from_lines(why_points, fallback_why)}</ul>
  </section>

  <section class="sl-storage" style="padding:22px;border:1px solid #eadfd5;border-radius:16px;margin:30px 0;background:#fff;">
    <h3 style="font-size:20px;margin:0 0 10px;">必要なものがすっきり入る</h3>
    <p style="margin:0;">{e(storage)}</p>
  </section>

  <section class="sl-scene" style="margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">こんなシーンにおすすめ</h3>
    <ul style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;list-style:none;padding:0;margin:0;">{scene_items}</ul>
  </section>

  <section class="sl-style" style="padding:22px;background:#f7f2eb;border-radius:16px;margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 10px;">どんなコーデにも合わせやすい</h3>
    <p style="margin:0;">{e(style)}</p>
  </section>

  <section class="sl-colors" style="margin:30px 0;">
    <h3 style="font-size:20px;margin:0 0 10px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">カラーバリエーション</h3>
    <p style="margin:0;">{e(colors)}</p>
  </section>

  <section class="sl-images" style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">商品イメージ</h3>
    <div style="text-align:center;">{image_block}</div>
  </section>

  <section class="sl-spec" style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">製品情報</h3>
    <table style="width:100%;border-collapse:collapse;font-size:14px;"><tbody>
      <tr><th style="width:34%;text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">商品名</th><td style="padding:12px;border:1px solid #eadfd5;">{title}</td></tr>
      <tr><th style="text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">カテゴリー</th><td style="padding:12px;border:1px solid #eadfd5;">{product_type_safe}</td></tr>
      <tr><th style="text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">素材</th><td style="padding:12px;border:1px solid #eadfd5;">{e(material or '商品ページをご確認ください')}</td></tr>
      <tr><th style="text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">サイズ</th><td style="padding:12px;border:1px solid #eadfd5;">{e(size_text or '商品ページをご確認ください')}</td></tr>
      <tr><th style="text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">重さ</th><td style="padding:12px;border:1px solid #eadfd5;">{e(weight_text or '商品ページをご確認ください')}</td></tr>
      <tr><th style="text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">開閉</th><td style="padding:12px;border:1px solid #eadfd5;">{e(closure_text or '商品ページをご確認ください')}</td></tr>
      <tr><th style="text-align:left;background:#f8f6f3;padding:12px;border:1px solid #eadfd5;">ポケット</th><td style="padding:12px;border:1px solid #eadfd5;">{e(pocket_text or '商品ページをご確認ください')}</td></tr>
    </tbody></table>
  </section>

  <section class="sl-trust" style="padding:22px;border-radius:16px;background:#fbfaf8;border:1px solid #eadfd5;margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 12px;">安心してご購入いただくために</h3>
    <p style="margin:0 0 10px;">SOCKSLOVERは日本のお客様向けに運営しているオンラインストアです。</p>
    <p style="margin:0;">配送期間・返品条件・お問い合わせ先を明記し、安心してお買い物いただけるよう努めています。</p>
  </section>

  <section class="sl-shipping" style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 10px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">配送・返品について</h3>
    <p style="margin:0 0 8px;"><strong>配送：</strong>{e(shipping)}</p>
    <p style="margin:0;"><strong>返品・返金：</strong>{e(returns)}</p>
  </section>

  <section class="sl-faq" style="margin:34px 0;">
    <h3 style="font-size:20px;margin:0 0 14px;border-bottom:1px solid #e8ded4;padding-bottom:8px;">よくあるご質問</h3>
    <div style="border-bottom:1px solid #eee;padding:12px 0;"><p style="font-weight:600;margin:0 0 6px;">Q. {e(faq1q)}</p><p style="margin:0;">A. {e(faq1a)}</p></div>
    <div style="border-bottom:1px solid #eee;padding:12px 0;"><p style="font-weight:600;margin:0 0 6px;">Q. {e(faq2q)}</p><p style="margin:0;">A. {e(faq2a)}</p></div>
    <div style="border-bottom:1px solid #eee;padding:12px 0;"><p style="font-weight:600;margin:0 0 6px;">Q. {e(faq3q)}</p><p style="margin:0;">A. {e(faq3a)}</p></div>
  </section>
</div>
""".strip()


def render_header():
    st.title("🛍️ Shopify GMC Optimizer")
    st.caption("상품 URL만 입력하면 외부 이미지를 Shopify CDN으로 교체하고, SOCKSLOVER 상세페이지 스타일의 GMC 대응 Body HTML을 생성합니다.")


def render_sidebar():
    st.sidebar.header("Shopify Settings")
    default_store = get_secret_or_env("SHOPIFY_STORE_DOMAIN", "sockslover-net.myshopify.com")
    default_token = get_secret_or_env("SHOPIFY_ADMIN_API_TOKEN", "")
    default_api_version = get_secret_or_env("SHOPIFY_API_VERSION", DEFAULT_API_VERSION)

    store_domain = st.sidebar.text_input("Store domain", value=default_store, help="예: sockslover-net.myshopify.com")
    token = st.sidebar.text_input("Admin API access token", value=default_token, type="password", help="Streamlit Cloud에서는 secrets.toml에 저장하는 것을 권장합니다.")
    api_version = st.sidebar.text_input("Admin API version", value=normalize_api_version(default_api_version), help="권장: 2026-01")

    st.sidebar.divider()
    st.sidebar.header("Image Detection")
    external_domains_text = st.sidebar.text_area("외부 이미지 도메인 키워드", value="\n".join(DEFAULT_EXTERNAL_DOMAINS), height=180)
    external_domains = [line.strip() for line in external_domains_text.splitlines() if line.strip()]
    return normalize_store_domain(store_domain), token.strip(), normalize_api_version(api_version), external_domains


def render_product_options(product: ProductData) -> dict:
    st.subheader("상세페이지 내용 설정")
    st.caption("참고 상품페이지처럼 Why / Storage / Scene / Style / Color / Spec / FAQ 구조로 생성됩니다.")

    lead_text = st.text_area(
        "Hero 요약문",
        value=f"{product.title}は、毎日の外出に取り入れやすいシンプルなデザインのアイテムです。必要なものをすっきり持ち歩きやすく、普段使いから旅行まで幅広いシーンで活躍します。",
        height=120,
    )

    col1, col2 = st.columns(2)
    with col1:
        why_points = st.text_area("選ばれる理由 / 한 줄에 하나", value="毎日使いやすいシンプルなデザイン\n必要なものをすっきり持ち歩ける実用的なサイズ感\nカジュアルにもきれいめにも合わせやすい\n通勤・お出かけ・旅行のサブバッグとして使いやすい", height=150)
        storage_text = st.text_area("Storage 설명", value="スマホ、財布、キーケース、ミニポーチなど、外出時に必要な小物をすっきり収納しやすいサイズ感です。", height=100)
        scene_points = st.text_area("こんなシーンにおすすめ / 한 줄에 하나", value="近所へのお出かけ\n通勤・通学\n休日のショッピング\n旅行中のサブバッグ", height=130)
        style_text = st.text_area("Style 설명", value="主張しすぎないデザインなので、デニムやワンピース、ジャケットスタイルなど幅広いコーディネートに合わせやすいです。", height=100)

    with col2:
        color_text = st.text_area("Color 설명", value="Black / Brown / Coffee / Khaki など、日常コーデに取り入れやすいカラー展開です。", height=90)
        product_type = st.text_input("カテゴリー", value=product.product_type or "レディースバッグ")
        material = st.text_input("素材", value="PU")
        size_text = st.text_input("サイズ", value="約27.5×19.5×9.5cm")
        weight_text = st.text_input("重さ", value="商品ページをご確認ください")
        closure_text = st.text_input("開閉", value="ファスナー")
        pocket_text = st.text_input("ポケット", value="外ポケットあり")

    st.markdown("### 配送・返品")
    shipping_text = st.text_area("配送説明", value="SOCKSLOVERでは全商品送料無料でお届けしています。ご注文後、通常2〜4営業日以内に発送準備を行い、発送後7〜14営業日前後でお届けします。", height=90)
    return_text = st.text_area("返品・返金説明", value="商品に不良があった場合は、返金または再送にて対応いたします。詳細は返品・返金ポリシーをご確認ください。", height=90)

    st.markdown("### FAQ")
    f1, f2, f3 = st.columns(3)
    with f1:
        faq_1_q = st.text_input("FAQ 1 질문", value="普段使いしやすいサイズですか？")
        faq_1_a = st.text_area("FAQ 1 답변", value="はい。スマホや財布、小物を持ち歩く日常使いにおすすめのサイズ感です。", height=100)
    with f2:
        faq_2_q = st.text_input("FAQ 2 질문", value="日本まで配送されますか？")
        faq_2_a = st.text_area("FAQ 2 답변", value="はい。SOCKSLOVERは日本のお客様向けに運営しており、全商品送料無料でお届けしています。", height=100)
    with f3:
        faq_3_q = st.text_input("FAQ 3 질문", value="返品や返金はできますか？")
        faq_3_a = st.text_area("FAQ 3 답변", value="商品に不良があった場合は、返品・返金ポリシーに基づき対応いたします。", height=100)

    return locals()


def preview_replacements(replacements: List[ImageReplacement], skipped: List[str]):
    st.subheader("이미지 처리 결과")
    if replacements:
        st.success(f"교체 대상 이미지: {len(replacements)}개")
        st.dataframe([r.__dict__ for r in replacements], use_container_width=True)
    else:
        st.info("교체된 이미지가 없습니다.")
    if skipped:
        with st.expander(f"스킵/실패 로그 {len(skipped)}개"):
            for item in skipped:
                st.write("- " + item)


def main():
    render_header()
    store_domain, token, api_version, external_domains = render_sidebar()

    with st.expander("사용 전 체크", expanded=False):
        st.markdown("""
- Shopify Admin API 권한: `read_products`, `write_products`, `read_files`, `write_files`
- Streamlit Cloud 사용 시 토큰은 `secrets.toml`에 저장 권장
- 처음에는 반드시 `Dry Run`으로 테스트
- 실제 반영 전 상품 설명 HTML 백업 권장
- 이 버전은 SOCKSLOVER 참고 상품페이지 스타일에 맞춘 섹션 구조를 생성합니다.
        """)

    product_url = st.text_input("Shopify 상품 URL", placeholder="https://sockslover.net/products/dual-move_...")
    col_a, col_b, _ = st.columns([1, 1, 2])
    with col_a:
        dry_run = st.toggle("Dry Run", value=True, help="ON이면 Shopify에 실제 업데이트하지 않습니다.")
    with col_b:
        fetch_button = st.button("상품 불러오기", type="primary")

    if "product" not in st.session_state:
        st.session_state.product = None
    if "product_url" not in st.session_state:
        st.session_state.product_url = ""

    if fetch_button:
        if not store_domain or not token:
            st.error("Store domain과 Admin API token을 입력해주세요.")
            return
        try:
            handle = extract_handle_from_product_url(product_url)
            with st.spinner("Shopify에서 상품을 불러오는 중..."):
                product = get_product_by_handle(store_domain, token, api_version, handle)
            st.session_state.product = product
            st.session_state.product_url = product_url
            st.success(f"상품을 찾았습니다: {product.title}")
        except Exception as err:
            st.error(f"상품 조회 실패: {err}")
            return

    product: Optional[ProductData] = st.session_state.product
    if not product:
        st.info("먼저 상품 URL을 입력하고 상품을 불러와주세요.")
        return

    st.divider()
    left, right = st.columns([1, 1])
    images = extract_image_tags(product.description_html)
    with left:
        st.subheader("상품 정보")
        st.write(f"**Title:** {product.title}")
        st.write(f"**Handle:** `{product.handle}`")
        st.write(f"**Product Type:** {product.product_type or '-'}")
        st.write(f"**Vendor:** {product.vendor or '-'}")
        st.write(f"**Body HTML 이미지 수:** {len(images)}")
        with st.expander("현재 Body HTML 보기"):
            st.code(product.description_html[:12000], language="html")
    with right:
        if images:
            st.subheader("현재 이미지 목록")
            image_rows = []
            for img in images:
                src_abs = absolutize_url(img["src"], product_url=st.session_state.product_url)
                image_rows.append({"external?": is_external_image_url(src_abs, store_domain, external_domains), "alt": img["alt"], "src": src_abs})
            st.dataframe(image_rows, use_container_width=True)
        else:
            st.warning("현재 Body HTML에 이미지가 없습니다.")

    st.divider()
    ux_options = render_product_options(product)
    st.divider()
    st.subheader("실행")

    confirm = st.checkbox("현재 상품의 Body HTML을 새 SOCKSLOVER 스타일 상세페이지 구조로 교체하는 것을 이해했습니다.", value=False)
    run_button = st.button("Dry Run 실행" if dry_run else "실제 업데이트 실행", type="primary", disabled=not confirm)

    if run_button:
        if not store_domain or not token:
            st.error("Store domain과 Admin API token을 입력해주세요.")
            return
        progress_area = st.empty()
        try:
            with st.spinner("이미지 업로드 및 HTML 재구성 중..."):
                replaced_html, replacements, skipped = replace_external_images(store_domain, token, api_version, product.description_html, st.session_state.product_url, external_domains, dry_run, progress_area)
                image_html = images_only_html(replaced_html, fallback_alt=product.title)
                new_body_html = build_sockslover_style_html(
                    product_title=product.title,
                    product_type=ux_options["product_type"],
                    image_html=image_html,
                    lead_text=ux_options["lead_text"],
                    why_points=ux_options["why_points"],
                    storage_text=ux_options["storage_text"],
                    scene_points=ux_options["scene_points"],
                    style_text=ux_options["style_text"],
                    color_text=ux_options["color_text"],
                    material=ux_options["material"],
                    size_text=ux_options["size_text"],
                    weight_text=ux_options["weight_text"],
                    closure_text=ux_options["closure_text"],
                    pocket_text=ux_options["pocket_text"],
                    shipping_text=ux_options["shipping_text"],
                    return_text=ux_options["return_text"],
                    faq_1_q=ux_options["faq_1_q"],
                    faq_1_a=ux_options["faq_1_a"],
                    faq_2_q=ux_options["faq_2_q"],
                    faq_2_a=ux_options["faq_2_a"],
                    faq_3_q=ux_options["faq_3_q"],
                    faq_3_a=ux_options["faq_3_a"],
                )
            preview_replacements(replacements, skipped)
            st.subheader("생성된 Body HTML Preview")
            st.code(new_body_html, language="html")
            st.download_button("생성된 HTML 다운로드", data=new_body_html, file_name=f"{product.handle}_body_html.html", mime="text/html")

            if dry_run:
                st.info("Dry Run이므로 Shopify에는 반영하지 않았습니다. 문제가 없으면 Dry Run을 끄고 다시 실행하세요.")
            else:
                with st.spinner("Shopify 상품 설명을 업데이트하는 중..."):
                    updated = update_product_description(store_domain, token, api_version, product.id, new_body_html)
                st.success("Shopify 상품 설명 업데이트 완료")
                st.json(updated)
                st.session_state.product = get_product_by_handle(store_domain, token, api_version, product.handle)
        except Exception as err:
            st.error(f"실행 실패: {err}")
            st.warning("네트워크 타임아웃이면 다시 실행해보세요. 계속 실패하면 API version을 2026-01로 고정하고, Streamlit Cloud secrets의 토큰과 권한을 확인해주세요.")

    st.divider()
    st.caption("Shopify GMC Optimizer / Built for SOCKSLOVER & Ion Labs")


if __name__ == "__main__":
    main()
