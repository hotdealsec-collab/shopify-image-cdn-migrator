import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple
from urllib.parse import urlparse, unquote

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ----------------------------
# Config
# ----------------------------

DEFAULT_API_VERSION = "2026-04"

EXTERNAL_IMAGE_KEYWORDS = [
    "cjdropshipping",
    "cjpacket",
    "alicdn",
    "aliexpress",
    "alicdn.com",
    "ae01.alicdn.com",
    "imgaz",
    "cloudfront",
]

SHOPIFY_CDN_KEYWORDS = [
    "cdn.shopify.com",
    "shopifycdn.net",
]


@dataclass
class ShopifyProduct:
    id: str
    title: str
    handle: str
    description_html: str


# ----------------------------
# Helpers
# ----------------------------

def get_secret(name: str, default: str = "") -> str:
    # Streamlit Cloud: st.secrets
    # Local: environment variables
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)


def normalize_store_domain(domain: str) -> str:
    domain = domain.strip()
    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.strip("/")
    return domain


def graphql_url(store_domain: str, api_version: str) -> str:
    return f"https://{store_domain}/admin/api/{api_version}/graphql.json"


def run_gql(store_domain: str, token: str, api_version: str, query: str, variables: dict | None = None) -> dict:
    endpoint = graphql_url(store_domain, api_version)
    res = requests.post(
        endpoint,
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=45,
    )
    try:
        payload = res.json()
    except Exception:
        raise RuntimeError(f"Shopify response is not JSON. HTTP {res.status_code}: {res.text[:500]}")

    if res.status_code >= 400:
        raise RuntimeError(f"Shopify HTTP error {res.status_code}: {payload}")

    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")

    return payload["data"]


def extract_handle_from_product_url(product_url: str) -> str:
    parsed = urlparse(product_url.strip())
    path = parsed.path
    match = re.search(r"/products/([^/?#]+)", path)
    if not match:
        raise ValueError("상품 URL에서 /products/{handle} 형식을 찾지 못했습니다.")
    return unquote(match.group(1))


def get_product_by_handle(store_domain: str, token: str, api_version: str, handle: str) -> ShopifyProduct:
    query = """
    query GetProductByHandle($q: String!) {
      products(first: 1, query: $q) {
        edges {
          node {
            id
            title
            handle
            descriptionHtml
          }
        }
      }
    }
    """
    data = run_gql(store_domain, token, api_version, query, {"q": f"handle:{handle}"})
    edges = data["products"]["edges"]

    # Fallback: sometimes pasted URL handle encoding/normalization differs.
    if not edges:
        fallback_query = """
        query SearchProduct($q: String!) {
          products(first: 10, query: $q) {
            edges {
              node {
                id
                title
                handle
                descriptionHtml
              }
            }
          }
        }
        """
        data = run_gql(store_domain, token, api_version, fallback_query, {"q": handle[:80]})
        edges = data["products"]["edges"]

    if not edges:
        raise RuntimeError(f"상품을 찾지 못했습니다. handle={handle}")

    node = edges[0]["node"]
    return ShopifyProduct(
        id=node["id"],
        title=node["title"],
        handle=node["handle"],
        description_html=node.get("descriptionHtml") or "",
    )


def is_shopify_cdn(url: str) -> bool:
    lower = url.lower()
    return any(k in lower for k in SHOPIFY_CDN_KEYWORDS)


def is_likely_external_image(url: str, strict_mode: bool = False) -> bool:
    if not url:
        return False

    if url.startswith("//"):
        url = "https:" + url

    lower = url.lower()

    if is_shopify_cdn(lower):
        return False

    # strict mode: every non-Shopify-CDN image is treated as external.
    if strict_mode:
        return lower.startswith("http")

    return any(k in lower for k in EXTERNAL_IMAGE_KEYWORDS)


def slugify_filename(text: str, limit: int = 60) -> str:
    text = text.lower()
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^a-z0-9가-힣ぁ-んァ-ン一-龥_-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:limit] or "sockslover-product-image"


def create_shopify_file_from_url(
    store_domain: str,
    token: str,
    api_version: str,
    url: str,
    alt_text: str,
    filename_prefix: str,
    index: int,
) -> str:
    mutation = """
    mutation CreateFile($files: [FileCreateInput!]!) {
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

    # Preserve extension if possible
    parsed_path = urlparse(url).path
    ext_match = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:$|\?)", parsed_path, re.IGNORECASE)
    ext = ext_match.group(1).lower() if ext_match else "jpg"
    filename = f"{filename_prefix}-{index:02d}.{ext}"

    variables = {
        "files": [
            {
                "originalSource": url,
                "contentType": "IMAGE",
                "alt": alt_text,
                "filename": filename,
            }
        ]
    }

    data = run_gql(store_domain, token, api_version, mutation, variables)
    errors = data["fileCreate"]["userErrors"]
    if errors:
        raise RuntimeError(f"fileCreate error for {url}: {errors}")

    files = data["fileCreate"]["files"]
    if not files:
        raise RuntimeError(f"fileCreate returned no file for {url}")

    return files[0]["id"]


def get_media_image_url(
    store_domain: str,
    token: str,
    api_version: str,
    file_id: str,
    max_retry: int = 20,
    sleep_sec: float = 2.5,
) -> str:
    query = """
    query GetMediaImage($id: ID!) {
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

    for _ in range(max_retry):
        data = run_gql(store_domain, token, api_version, query, {"id": file_id})
        node = data.get("node")
        if not node:
            raise RuntimeError(f"파일 조회 실패: {file_id}")

        last_status = node.get("fileStatus")
        image = node.get("image") or {}

        if last_status == "READY" and image.get("url"):
            return image["url"]

        time.sleep(sleep_sec)

    raise RuntimeError(f"파일 처리가 완료되지 않았습니다. file_id={file_id}, last_status={last_status}")


def replace_external_images(
    store_domain: str,
    token: str,
    api_version: str,
    product: ShopifyProduct,
    strict_mode: bool,
    dry_run: bool,
) -> Tuple[str, Dict[str, str], List[str]]:
    soup = BeautifulSoup(product.description_html or "", "html.parser")
    replacements: Dict[str, str] = {}
    skipped: List[str] = []
    filename_prefix = slugify_filename(product.handle)

    images = soup.find_all("img")

    for idx, img in enumerate(images, start=1):
        src = img.get("src") or ""
        if src.startswith("//"):
            src = "https:" + src
        src = src.strip()

        if not src:
            continue

        if is_likely_external_image(src, strict_mode=strict_mode):
            alt_text = img.get("alt") or product.title or "SOCKSLOVER product image"

            if dry_run:
                cdn_url = f"https://cdn.shopify.com/s/files/preview/{filename_prefix}-{idx:02d}.jpg"
            else:
                file_id = create_shopify_file_from_url(
                    store_domain=store_domain,
                    token=token,
                    api_version=api_version,
                    url=src,
                    alt_text=alt_text,
                    filename_prefix=filename_prefix,
                    index=idx,
                )
                cdn_url = get_media_image_url(store_domain, token, api_version, file_id)

            img["src"] = cdn_url
            img["alt"] = alt_text
            img["loading"] = "lazy"
            img["style"] = "max-width:100%;height:auto;margin:12px 0;"
            replacements[src] = cdn_url
        else:
            skipped.append(src)
            if img.get("src"):
                img["loading"] = "lazy"
                img["style"] = "max-width:100%;height:auto;margin:12px 0;"
                if not img.get("alt"):
                    img["alt"] = product.title or "SOCKSLOVER product image"

    return str(soup), replacements, skipped


def extract_images_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text or "", "html.parser")
    imgs = soup.find_all("img")

    if not imgs:
        return ""

    return "\n".join(str(img) for img in imgs)


def build_ux_body_html(
    product_title: str,
    image_html: str,
    material: str,
    size_text: str,
    colors: str,
    category: str,
    shipping_text: str,
) -> str:
    return f"""
<div class="sl-product-detail">
  <section class="sl-summary">
    <h2>2WAYで使える、軽くてシンプルなデイリーバッグ</h2>
    <p>
      {product_title}は、クロスボディバッグとしてもバックパックとしても使える、
      毎日の外出に便利な2WAYタイプのレディースバッグです。
      コンパクトながら必要な小物を入れやすく、通勤・旅行・ちょっとしたお出かけにも使いやすいデザインです。
    </p>
  </section>

  <section class="sl-points">
    <h3>この商品のポイント</h3>
    <ul>
      <li>クロスボディ＆バックパックの2WAY仕様</li>
      <li>日常使いしやすいコンパクトなサイズ感</li>
      <li>軽く持ちやすく、両手を空けたい外出にも便利</li>
      <li>シンプルなデザインでカジュアルにもきれいめにも合わせやすい</li>
      <li>スマホ・財布・キーケース・ミニポーチなどの収納におすすめ</li>
    </ul>
  </section>

  <section class="sl-scene">
    <h3>おすすめの使用シーン</h3>
    <p>
      近所へのお出かけ、通勤、旅行中のサブバッグ、週末のお買い物など、
      荷物をすっきり持ち歩きたい日におすすめです。
    </p>
  </section>

  <section class="sl-info">
    <h3>商品情報</h3>
    <table>
      <tbody>
        <tr><th>商品名</th><td>{product_title}</td></tr>
        <tr><th>カテゴリー</th><td>{category}</td></tr>
        <tr><th>素材</th><td>{material}</td></tr>
        <tr><th>サイズ</th><td>{size_text}</td></tr>
        <tr><th>カラー</th><td>{colors}</td></tr>
        <tr><th>内容</th><td>バッグ×1</td></tr>
      </tbody>
    </table>
  </section>

  <section class="sl-images">
    <h3>商品イメージ</h3>
    {image_html}
  </section>

  <section class="sl-shipping">
    <h3>配送・返品について</h3>
    <p>{shipping_text}</p>
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


def update_product_description(
    store_domain: str,
    token: str,
    api_version: str,
    product_id: str,
    new_html: str,
) -> dict:
    mutation = """
    mutation UpdateProduct($product: ProductUpdateInput!) {
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
            "descriptionHtml": new_html,
        }
    }

    data = run_gql(store_domain, token, api_version, mutation, variables)
    errors = data["productUpdate"]["userErrors"]
    if errors:
        raise RuntimeError(f"productUpdate error: {errors}")

    return data["productUpdate"]["product"]


# ----------------------------
# Streamlit UI
# ----------------------------

st.set_page_config(
    page_title="SOCKSLOVER GMC Product HTML Fixer",
    page_icon="🧦",
    layout="wide",
)

st.title("SOCKSLOVER GMC Product HTML Fixer")
st.caption("상품 URL만 입력하면 외부 이미지를 Shopify Files/CDN으로 옮기고, GMC용 UX 상품 설명 HTML로 재구성합니다.")

with st.sidebar:
    st.header("Shopify 설정")
    store_domain = st.text_input(
        "Shopify myshopify.com 도메인",
        value=get_secret("SHOPIFY_STORE_DOMAIN", "sockslover-net.myshopify.com"),
    )
    admin_token = st.text_input(
        "Admin API access token",
        value=get_secret("SHOPIFY_ADMIN_API_TOKEN", ""),
        type="password",
    )
    api_version = st.text_input(
        "Admin API version",
        value=get_secret("SHOPIFY_API_VERSION", DEFAULT_API_VERSION),
    )

    st.divider()
    dry_run = st.toggle("Dry Run: 실제 반영하지 않고 미리보기만", value=True)
    strict_mode = st.toggle("모든 비-Shopify CDN 이미지를 외부 이미지로 간주", value=False)

    st.warning("토큰은 GitHub에 커밋하지 마세요. Streamlit secrets 또는 로컬 환경변수로만 관리하세요.")

product_url = st.text_input(
    "상품 URL",
    placeholder="https://sockslover.net/products/dual-move_...",
)

with st.expander("UX 템플릿 기본값 수정", expanded=False):
    col_a, col_b = st.columns(2)
    with col_a:
        material = st.text_input("素材", value="PU")
        size_text = st.text_input("サイズ", value="約27.5×19.5×9.5cm")
        colors = st.text_input("カラー", value="Black / Brown / Coffee / Khaki")
    with col_b:
        category = st.text_input("カテゴリー", value="レディースバッグ")
        shipping_text = st.text_area(
            "配送・返品テキスト",
            value=(
                "SOCKSLOVERでは全商品送料無料でお届けしています。"
                "ご注文後、通常2〜4営業日以内に発送準備を行い、発送後7〜14営業日前後でお届けします。"
                "商品に不良があった場合は、返金または再送にて対応いたします。"
            ),
            height=120,
        )

run_button = st.button("商品HTMLを確認・変換する", type="primary", use_container_width=True)

if run_button:
    try:
        if not product_url:
            st.error("상품 URL을 입력해주세요.")
            st.stop()

        if not store_domain or not admin_token:
            st.error("Shopify 도메인과 Admin API token이 필요합니다.")
            st.stop()

        store_domain = normalize_store_domain(store_domain)
        handle = extract_handle_from_product_url(product_url)

        with st.status("Shopify 상품 정보를 가져오는 중...", expanded=True) as status:
            st.write(f"Handle: `{handle}`")
            product = get_product_by_handle(store_domain, admin_token, api_version, handle)
            st.write(f"상품명: **{product.title}**")
            st.write(f"Shopify handle: `{product.handle}`")
            status.update(label="상품 정보를 가져왔습니다.", state="complete")

        with st.status("외부 이미지 확인 및 Shopify Files 변환 중...", expanded=True) as status:
            replaced_html, replacements, skipped = replace_external_images(
                store_domain=store_domain,
                token=admin_token,
                api_version=api_version,
                product=product,
                strict_mode=strict_mode,
                dry_run=dry_run,
            )
            st.write(f"교체 대상 이미지: **{len(replacements)}개**")
            st.write(f"유지한 이미지: **{len(skipped)}개**")
            status.update(label="이미지 처리 완료", state="complete")

        image_html = extract_images_html(replaced_html)
        new_body_html = build_ux_body_html(
            product_title=product.title,
            image_html=image_html,
            material=material,
            size_text=size_text,
            colors=colors,
            category=category,
            shipping_text=shipping_text,
        )

        st.subheader("변환 결과")
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### 이미지 교체 목록")
            if replacements:
                for old, new in replacements.items():
                    st.code(f"{old}\n→\n{new}", language="text")
            else:
                st.info("교체할 외부 이미지가 발견되지 않았습니다.")

        with col2:
            st.markdown("### 유지된 이미지")
            if skipped:
                for url in skipped[:20]:
                    st.code(url, language="text")
                if len(skipped) > 20:
                    st.caption(f"외 {len(skipped) - 20}개")
            else:
                st.info("유지된 이미지가 없습니다.")

        st.markdown("### 새 Body HTML")
        st.code(new_body_html, language="html")

        st.download_button(
            label="새 Body HTML 다운로드",
            data=new_body_html,
            file_name=f"{product.handle}_body_html.html",
            mime="text/html",
            use_container_width=True,
        )

        if dry_run:
            st.info("Dry Run이 ON입니다. Shopify에는 아직 반영하지 않았습니다.")
            st.warning("실제 반영하려면 왼쪽 사이드바에서 Dry Run을 OFF로 바꾼 후 다시 실행하세요.")
        else:
            with st.status("Shopify 상품 Body HTML 업데이트 중...", expanded=True) as status:
                updated = update_product_description(
                    store_domain=store_domain,
                    token=admin_token,
                    api_version=api_version,
                    product_id=product.id,
                    new_html=new_body_html,
                )
                st.write(updated)
                status.update(label="Shopify 업데이트 완료", state="complete")

            st.success("상품 Body HTML 업데이트가 완료되었습니다.")

    except Exception as e:
        st.error("처리 중 오류가 발생했습니다.")
        st.exception(e)
