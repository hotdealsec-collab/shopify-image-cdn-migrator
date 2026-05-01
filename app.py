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
# Shopify GMC Optimizer
# ---------------------------------------------------------
# What this app does:
# 1. Accepts a Shopify product URL
# 2. Finds the Shopify product by handle
# 3. Extracts images from descriptionHtml
# 4. Uploads external images to Shopify Files
# 5. Replaces external image URLs with Shopify CDN URLs
# 6. Rebuilds product descriptionHtml into a GMC-friendly UX layout
# 7. Updates the Shopify product description
# =========================================================


# -----------------------------
# Basic page config
# -----------------------------
st.set_page_config(
    page_title="Shopify GMC Optimizer",
    page_icon="🛍️",
    layout="wide",
)


# -----------------------------
# Constants
# -----------------------------
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

SHOPIFY_CDN_MARKERS = [
    "cdn.shopify.com",
    "shopifycdn.net",
]

REQUEST_CONNECT_TIMEOUT = 20
REQUEST_READ_TIMEOUT = 150
DEFAULT_MAX_RETRIES = 6


# -----------------------------
# Utility dataclasses
# -----------------------------
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


# -----------------------------
# Secret / config helpers
# -----------------------------
def get_secret_or_env(key: str, default: str = "") -> str:
    """Read value from Streamlit secrets first, then env."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


def normalize_store_domain(store_domain: str) -> str:
    store_domain = (store_domain or "").strip()
    store_domain = store_domain.replace("https://", "").replace("http://", "")
    store_domain = store_domain.strip("/")
    return store_domain


def normalize_api_version(api_version: str) -> str:
    api_version = (api_version or DEFAULT_API_VERSION).strip()
    if not re.match(r"^\d{4}-\d{2}$", api_version):
        return DEFAULT_API_VERSION
    return api_version


def extract_handle_from_product_url(product_url: str) -> str:
    """
    Extract product handle from:
    https://store.com/products/handle
    https://store.com/products/handle?variant=...
    """
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

    handle = parts[idx + 1]
    handle = urllib.parse.unquote(handle)
    return handle


def safe_html_text(value: str) -> str:
    return html.escape(value or "", quote=True)


# -----------------------------
# Robust Shopify GraphQL client
# -----------------------------
def run_gql(
    store_domain: str,
    token: str,
    api_version: str,
    query: str,
    variables: Optional[dict] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """
    Robust GraphQL request with retry.
    Handles Streamlit Cloud / Shopify intermittent connection timeouts.
    """
    store_domain = normalize_store_domain(store_domain)
    api_version = normalize_api_version(api_version)

    endpoint = f"https://{store_domain}/admin/api/{api_version}/graphql.json"

    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }

    payload = {
        "query": query,
        "variables": variables or {},
    }

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
                raise RuntimeError(f"404 Not Found: Shopify endpoint를 찾을 수 없습니다. store_domain/API version 확인: {endpoint}")

            if res.status_code == 429:
                wait_sec = min(8 * attempt, 40)
                time.sleep(wait_sec)
                last_error = RuntimeError("429 Too Many Requests")
                continue

            res.raise_for_status()
            data = res.json()

            if "errors" in data:
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(data['errors'], ensure_ascii=False)}")

            if "data" not in data:
                raise RuntimeError(f"Invalid Shopify response: {data}")

            return data["data"]

        except requests.exceptions.ConnectTimeout as e:
            last_error = e
            time.sleep(min(5 * attempt, 30))

        except requests.exceptions.ReadTimeout as e:
            last_error = e
            time.sleep(min(5 * attempt, 30))

        except requests.exceptions.ConnectionError as e:
            last_error = e
            time.sleep(min(5 * attempt, 30))

        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = res.text
            except Exception:
                pass
            raise RuntimeError(f"Shopify API HTTP error: {e}. Response: {body}")

    raise RuntimeError(
        f"Shopify API connection failed after {max_retries} retries. Last error: {last_error}"
    )


# -----------------------------
# Shopify product functions
# -----------------------------
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

    data = run_gql(
        store_domain=store_domain,
        token=token,
        api_version=api_version,
        query=query,
        variables={"query": f"handle:{handle}"},
    )

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


def update_product_description(
    store_domain: str,
    token: str,
    api_version: str,
    product_id: str,
    new_description_html: str,
) -> dict:
    mutation = """
    mutation ProductUpdate($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product {
          id
          title
          handle
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    variables = {
        "product": {
            "id": product_id,
            "descriptionHtml": new_description_html,
        }
    }

    data = run_gql(
        store_domain=store_domain,
        token=token,
        api_version=api_version,
        query=mutation,
        variables=variables,
    )

    result = data["productUpdate"]
    errors = result.get("userErrors", [])
    if errors:
        raise RuntimeError(f"productUpdate userErrors: {errors}")

    return result["product"]


# -----------------------------
# Shopify Files functions
# -----------------------------
def file_create_from_url(
    store_domain: str,
    token: str,
    api_version: str,
    source_url: str,
    alt_text: str,
) -> str:
    """
    Creates a Shopify file from external URL.
    Returns file ID.
    """
    mutation = """
    mutation FileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          id
          fileStatus
          alt
          createdAt
          ... on MediaImage {
            image {
              url
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    variables = {
        "files": [
            {
                "originalSource": source_url,
                "contentType": "IMAGE",
                "alt": alt_text or "SOCKSLOVER product image",
            }
        ]
    }

    data = run_gql(
        store_domain=store_domain,
        token=token,
        api_version=api_version,
        query=mutation,
        variables=variables,
    )

    result = data["fileCreate"]
    errors = result.get("userErrors", [])
    if errors:
        raise RuntimeError(f"fileCreate userErrors: {errors}")

    files = result.get("files") or []
    if not files:
        raise RuntimeError("fileCreate succeeded but no file returned.")

    return files[0]["id"]


def get_media_image_url(
    store_domain: str,
    token: str,
    api_version: str,
    file_id: str,
    max_checks: int = 30,
    sleep_sec: int = 5,
) -> str:
    """
    Polls MediaImage until fileStatus READY and image.url exists.
    """
    query = """
    query GetFile($id: ID!) {
      node(id: $id) {
        ... on MediaImage {
          id
          fileStatus
          image {
            url
          }
        }
      }
    }
    """

    last_status = None

    for _ in range(max_checks):
        data = run_gql(
            store_domain=store_domain,
            token=token,
            api_version=api_version,
            query=query,
            variables={"id": file_id},
        )

        node = data.get("node")
        if not node:
            time.sleep(sleep_sec)
            continue

        last_status = node.get("fileStatus")
        image = node.get("image")

        if last_status == "READY" and image and image.get("url"):
            return image["url"]

        if last_status in ["FAILED"]:
            raise RuntimeError(f"Shopify file processing failed. file_id={file_id}")

        time.sleep(sleep_sec)

    raise RuntimeError(f"Shopify file not ready. file_id={file_id}, last_status={last_status}")


# -----------------------------
# HTML image logic
# -----------------------------
def absolutize_url(src: str, product_url: Optional[str] = None) -> str:
    if not src:
        return src

    src = src.strip()

    if src.startswith("//"):
        return "https:" + src

    if src.startswith("http://") or src.startswith("https://"):
        return src

    # Relative image URL. Resolve against product URL.
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
    return (
        lower.startswith("data:")
        or lower.startswith("blob:")
        or lower.startswith("#")
        or lower.startswith("javascript:")
    )


def is_external_image_url(url: str, store_domain: str, external_domains: List[str]) -> bool:
    """
    Determines whether image should be migrated to Shopify CDN.
    Rule:
    - Shopify CDN: no
    - data/blob: no
    - URL containing configured external domains: yes
    - non-Shopify external host: yes
    - store's own host: no
    """
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

    # Public storefront domain may differ from myshopify domain.
    # sockslover.net hosted images are still not Shopify CDN, but usually should be migrated if inside body HTML.
    if "sockslover.net" in host and "cdn.shopify.com" not in host:
        return True

    # Any image from non-Shopify external host should be migrated.
    return True


def extract_image_tags(description_html: str) -> List[dict]:
    soup = BeautifulSoup(description_html or "", "html.parser")
    images = []
    for img in soup.find_all("img"):
        images.append(
            {
                "src": img.get("src", ""),
                "alt": img.get("alt", ""),
            }
        )
    return images


def replace_external_images(
    store_domain: str,
    token: str,
    api_version: str,
    description_html: str,
    product_url: str,
    external_domains: List[str],
    dry_run: bool,
    progress_area=None,
) -> Tuple[str, List[ImageReplacement], List[str]]:
    """
    Replaces external image URLs in HTML with Shopify CDN URLs.
    Returns:
    - updated HTML
    - replacement list
    - skipped messages
    """
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
        original_src = img.get("src", "")
        src = absolutize_url(original_src, product_url=product_url)

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
                    file_id = file_create_from_url(
                        store_domain=store_domain,
                        token=token,
                        api_version=api_version,
                        source_url=src,
                        alt_text=alt_text,
                    )
                    new_url = get_media_image_url(
                        store_domain=store_domain,
                        token=token,
                        api_version=api_version,
                        file_id=file_id,
                        max_checks=30,
                        sleep_sec=5,
                    )
                    cache[src] = new_url

                status = "uploaded"

            img["src"] = new_url
            img["alt"] = alt_text
            img["loading"] = "lazy"
            img["style"] = "max-width:100%;height:auto;margin:12px 0;"

            replacements.append(
                ImageReplacement(
                    original_url=src,
                    new_url=new_url,
                    alt=alt_text,
                    status=status,
                )
            )
            migrated_count += 1

        except Exception as e:
            message = f"[{index}/{total}] 업로드 실패: {src} / {e}"
            skipped.append(message)
            # Keep original image URL if migration fails.
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
        img["style"] = "max-width:100%;height:auto;margin:12px 0;"
        image_html_list.append(str(img))

    return "\n".join(image_html_list)


# -----------------------------
# GMC-friendly UX HTML builder
# -----------------------------
def build_gmc_ux_html(
    product_title: str,
    product_type: str,
    image_html: str,
    custom_intro: str,
    material: str,
    size_text: str,
    colors: str,
    scenes: str,
    shipping_text: str,
    return_text: str,
) -> str:
    title = safe_html_text(product_title)
    product_type_safe = safe_html_text(product_type or "ファッション雑貨")
    material_safe = safe_html_text(material or "商品ページをご確認ください")
    size_safe = safe_html_text(size_text or "商品ページをご確認ください")
    colors_safe = safe_html_text(colors or "商品ページをご確認ください")
    scenes_safe = safe_html_text(scenes or "デイリーのお出かけ、通勤、旅行、サブバッグとしておすすめです。")
    shipping_safe = safe_html_text(shipping_text or "全商品送料無料。通常、発送後7〜14営業日前後でお届けします。")
    return_safe = safe_html_text(return_text or "商品に不良があった場合は、返金または再送にて対応いたします。")

    intro = custom_intro.strip() if custom_intro else ""
    if not intro:
        intro = (
            f"{title}は、毎日の外出に取り入れやすいシンプルなデザインのアイテムです。"
            "使いやすさとコーディネートへの合わせやすさを重視し、日常使いにおすすめです。"
        )
    intro_safe = safe_html_text(intro)

    html_output = f"""
<div class="sl-product-detail">

  <section class="sl-summary">
    <h2>{title}</h2>
    <p>{intro_safe}</p>
  </section>

  <section class="sl-points">
    <h3>この商品のポイント</h3>
    <ul>
      <li>日常使いしやすいシンプルなデザイン</li>
      <li>コーディネートに合わせやすい実用的なアイテム</li>
      <li>お出かけ・通勤・旅行など幅広いシーンで使いやすい</li>
      <li>日本のお客様向けに送料無料でお届け</li>
    </ul>
  </section>

  <section class="sl-scene">
    <h3>おすすめの使用シーン</h3>
    <p>{scenes_safe}</p>
  </section>

  <section class="sl-info">
    <h3>商品情報</h3>
    <table>
      <tbody>
        <tr><th>商品名</th><td>{title}</td></tr>
        <tr><th>カテゴリー</th><td>{product_type_safe}</td></tr>
        <tr><th>素材</th><td>{material_safe}</td></tr>
        <tr><th>サイズ</th><td>{size_safe}</td></tr>
        <tr><th>カラー</th><td>{colors_safe}</td></tr>
        <tr><th>内容</th><td>商品1点</td></tr>
      </tbody>
    </table>
  </section>

  <section class="sl-images">
    <h3>商品イメージ</h3>
    {image_html}
  </section>

  <section class="sl-shipping">
    <h3>配送について</h3>
    <p>{shipping_safe}</p>
  </section>

  <section class="sl-return">
    <h3>返品・返金について</h3>
    <p>{return_safe}</p>
  </section>

  <section class="sl-trust">
    <h3>安心してご購入いただくために</h3>
    <p>
      SOCKSLOVERは日本のお客様向けに運営しているオンラインストアです。
      配送期間・返品条件・お問い合わせ先を明記し、安心してお買い物いただけるよう努めています。
    </p>
  </section>

</div>
""".strip()

    return html_output


# -----------------------------
# UI
# -----------------------------
def render_header():
    st.title("🛍️ Shopify GMC Optimizer")
    st.caption(
        "상품 URL만 입력하면 Body HTML의 외부 이미지를 Shopify CDN으로 교체하고, "
        "GMC 심사와 전환에 유리한 상품 설명 구조로 재작성합니다."
    )


def render_sidebar():
    st.sidebar.header("Shopify Settings")

    default_store = get_secret_or_env("SHOPIFY_STORE_DOMAIN", "sockslover-net.myshopify.com")
    default_token = get_secret_or_env("SHOPIFY_ADMIN_API_TOKEN", "")
    default_api_version = get_secret_or_env("SHOPIFY_API_VERSION", DEFAULT_API_VERSION)

    store_domain = st.sidebar.text_input(
        "Store domain",
        value=default_store,
        help="예: sockslover-net.myshopify.com",
    )

    token = st.sidebar.text_input(
        "Admin API access token",
        value=default_token,
        type="password",
        help="Streamlit Cloud에서는 secrets.toml에 저장하는 것을 권장합니다.",
    )

    api_version = st.sidebar.text_input(
        "Admin API version",
        value=normalize_api_version(default_api_version),
        help="권장: 2026-01",
    )

    st.sidebar.divider()
    st.sidebar.header("Image Detection")

    external_domains_text = st.sidebar.text_area(
        "외부 이미지 도메인 키워드",
        value="\n".join(DEFAULT_EXTERNAL_DOMAINS),
        height=180,
        help="한 줄에 하나씩 입력하세요. 이 도메인이 포함된 이미지는 Shopify Files로 업로드합니다.",
    )

    external_domains = [
        line.strip()
        for line in external_domains_text.splitlines()
        if line.strip()
    ]

    return normalize_store_domain(store_domain), token.strip(), normalize_api_version(api_version), external_domains


def render_product_options(product: ProductData):
    st.subheader("UX 상품 설명 설정")

    col1, col2 = st.columns(2)

    with col1:
        material = st.text_input("素材", value="PU")
        size_text = st.text_input("サイズ", value="約27.5×19.5×9.5cm")
        colors = st.text_input("カラー", value="Black / Brown / Coffee / Khaki")

    with col2:
        product_type = st.text_input(
            "カテゴリー",
            value=product.product_type or "レディースバッグ",
        )
        scenes = st.text_area(
            "おすすめ使用シーン",
            value="スマホ、財布、キーケース、ミニポーチなどを持ち歩く日常使いにおすすめです。両手を空けたいお出かけや、旅行中のサブバッグとしても活躍します。",
            height=110,
        )

    custom_intro = st.text_area(
        "冒頭説明文",
        value=(
            f"{product.title}は、クロスボディバッグとしてもバックパックとしても使える、"
            "毎日の外出に便利な2WAYタイプのレディースバッグです。"
            "コンパクトながら必要な小物を入れやすく、通勤・旅行・ちょっとしたお出かけにも使いやすいデザインです。"
        ),
        height=130,
    )

    shipping_text = st.text_area(
        "配送説明",
        value="SOCKSLOVERでは全商品送料無料でお届けしています。ご注文後、通常2〜4営業日以内に発送準備を行い、発送後7〜14営業日前後でお届けします。",
        height=90,
    )

    return_text = st.text_area(
        "返品・返金説明",
        value="商品に不良があった場合は、返金または再送にて対応いたします。詳細は返品・返金ポリシーをご確認ください。",
        height=90,
    )

    return {
        "material": material,
        "size_text": size_text,
        "colors": colors,
        "product_type": product_type,
        "scenes": scenes,
        "custom_intro": custom_intro,
        "shipping_text": shipping_text,
        "return_text": return_text,
    }


def preview_replacements(replacements: List[ImageReplacement], skipped: List[str]):
    st.subheader("이미지 처리 결과")

    if replacements:
        st.success(f"교체 대상 이미지: {len(replacements)}개")
        rows = [
            {
                "status": r.status,
                "alt": r.alt,
                "original_url": r.original_url,
                "new_url": r.new_url,
            }
            for r in replacements
        ]
        st.dataframe(rows, use_container_width=True)
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
        st.markdown(
            """
- Shopify Admin API 권한: `read_products`, `write_products`, `read_files`, `write_files`
- Streamlit Cloud 사용 시 토큰은 `secrets.toml`에 저장 권장
- 처음에는 반드시 `Dry Run`으로 테스트
- 실제 반영 전 상품 설명 HTML 백업 권장
            """
        )

    product_url = st.text_input(
        "Shopify 상품 URL",
        placeholder="https://sockslover.net/products/dual-move_...",
    )

    col_a, col_b, col_c = st.columns([1, 1, 2])

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
                product = get_product_by_handle(
                    store_domain=store_domain,
                    token=token,
                    api_version=api_version,
                    handle=handle,
                )
            st.session_state.product = product
            st.session_state.product_url = product_url
            st.success(f"상품을 찾았습니다: {product.title}")

        except Exception as e:
            st.error(f"상품 조회 실패: {e}")
            return

    product: Optional[ProductData] = st.session_state.product

    if not product:
        st.info("먼저 상품 URL을 입력하고 상품을 불러와주세요.")
        return

    st.divider()

    left, right = st.columns([1, 1])

    with left:
        st.subheader("상품 정보")
        st.write(f"**Title:** {product.title}")
        st.write(f"**Handle:** `{product.handle}`")
        st.write(f"**Product Type:** {product.product_type or '-'}")
        st.write(f"**Vendor:** {product.vendor or '-'}")

        images = extract_image_tags(product.description_html)
        st.write(f"**Body HTML 이미지 수:** {len(images)}")

        with st.expander("현재 Body HTML 보기"):
            st.code(product.description_html[:12000], language="html")

    with right:
        if images:
            st.subheader("현재 이미지 목록")
            image_rows = []
            for img in images:
                src_abs = absolutize_url(img["src"], product_url=st.session_state.product_url)
                image_rows.append(
                    {
                        "external?": is_external_image_url(src_abs, store_domain, external_domains),
                        "alt": img["alt"],
                        "src": src_abs,
                    }
                )
            st.dataframe(image_rows, use_container_width=True)
        else:
            st.warning("현재 Body HTML에 이미지가 없습니다.")

    st.divider()

    ux_options = render_product_options(product)

    st.divider()

    st.subheader("실행")

    confirm = st.checkbox(
        "현재 상품의 Body HTML을 새 UX 구조로 교체하는 것을 이해했습니다.",
        value=False,
    )

    run_button = st.button(
        "Dry Run 실행" if dry_run else "실제 업데이트 실행",
        type="primary",
        disabled=not confirm,
    )

    if run_button:
        if not store_domain or not token:
            st.error("Store domain과 Admin API token을 입력해주세요.")
            return

        progress_area = st.empty()

        try:
            with st.spinner("이미지 업로드 및 HTML 재구성 중..."):
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

                image_html = images_only_html(
                    replaced_html,
                    fallback_alt=product.title,
                )

                new_body_html = build_gmc_ux_html(
                    product_title=product.title,
                    product_type=ux_options["product_type"],
                    image_html=image_html,
                    custom_intro=ux_options["custom_intro"],
                    material=ux_options["material"],
                    size_text=ux_options["size_text"],
                    colors=ux_options["colors"],
                    scenes=ux_options["scenes"],
                    shipping_text=ux_options["shipping_text"],
                    return_text=ux_options["return_text"],
                )

            preview_replacements(replacements, skipped)

            st.subheader("생성된 Body HTML Preview")
            st.code(new_body_html, language="html")

            if dry_run:
                st.info("Dry Run이므로 Shopify에는 반영하지 않았습니다. 문제가 없으면 Dry Run을 끄고 다시 실행하세요.")
            else:
                with st.spinner("Shopify 상품 설명을 업데이트하는 중..."):
                    updated = update_product_description(
                        store_domain=store_domain,
                        token=token,
                        api_version=api_version,
                        product_id=product.id,
                        new_description_html=new_body_html,
                    )

                st.success("Shopify 상품 설명 업데이트 완료")
                st.json(updated)

                # Refresh session product data after update
                refreshed = get_product_by_handle(
                    store_domain=store_domain,
                    token=token,
                    api_version=api_version,
                    handle=product.handle,
                )
                st.session_state.product = refreshed

        except Exception as e:
            st.error(f"실행 실패: {e}")
            st.warning(
                "네트워크 타임아웃이면 다시 실행해보세요. "
                "계속 실패하면 API version을 2026-01로 고정하고, Streamlit Cloud secrets의 토큰을 확인해주세요."
            )

    st.divider()
    st.caption("Shopify GMC Optimizer / Built for SOCKSLOVER & Ion Labs")


if __name__ == "__main__":
    main()
