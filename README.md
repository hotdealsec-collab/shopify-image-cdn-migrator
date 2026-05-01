# SOCKSLOVER GMC Product HTML Fixer

Shopify 상품 URL을 입력하면 다음 작업을 수행하는 Streamlit 앱입니다.

1. 상품 URL에서 Shopify product handle 추출
2. Shopify Admin GraphQL API로 상품 Body HTML 조회
3. Body HTML 안의 외부 이미지 URL(CJDropshipping, AliExpress, alicdn 등)을 탐지
4. Shopify Files API(`fileCreate`)로 이미지를 Shopify Files에 생성
5. Shopify CDN URL로 `<img src="">` 교체
6. GMC 심사와 전환 UX를 고려한 일본어 상품 설명 HTML로 재구성
7. `productUpdate`로 상품 Body HTML 업데이트

## 1. Shopify Custom App 권한

Shopify Admin에서 Custom App을 만들고 아래 권한을 부여하세요.

- `read_products`
- `write_products`
- `read_files`
- `write_files`

## 2. 로컬 실행

```bash
git clone <your-repo-url>
cd <repo>
pip install -r requirements.txt
streamlit run app.py
```

로컬 환경변수를 쓰려면 `.env.example`을 참고하세요.  
단, 현재 코드는 Streamlit secrets 또는 OS 환경변수를 읽습니다.

macOS/Linux 예시:

```bash
export SHOPIFY_STORE_DOMAIN="sockslover-net.myshopify.com"
export SHOPIFY_ADMIN_API_TOKEN="shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export SHOPIFY_API_VERSION="2026-04"
streamlit run app.py
```

## 3. Streamlit Cloud 배포

1. 이 폴더를 GitHub에 업로드
2. Streamlit Cloud에서 새 앱 생성
3. App settings > Secrets에 아래 형식으로 등록

```toml
SHOPIFY_STORE_DOMAIN = "sockslover-net.myshopify.com"
SHOPIFY_ADMIN_API_TOKEN = "shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SHOPIFY_API_VERSION = "2026-04"
```

## 4. 사용 방법

1. 상품 URL 입력
2. 처음에는 `Dry Run` ON 상태로 실행
3. HTML과 이미지 교체 목록 확인
4. 문제가 없으면 `Dry Run` OFF
5. 다시 실행하면 Shopify 상품 Body HTML에 반영

## 5. 주의사항

- Admin API token은 GitHub에 절대 커밋하지 마세요.
- 처음에는 반드시 단일 상품으로 테스트하세요.
- Shopify Files의 `fileCreate`는 비동기 처리이므로, 앱 내부에서 `fileStatus == READY`가 될 때까지 확인합니다.
- 외부 이미지 서버가 Shopify에서 접근 불가능하면 업로드가 실패할 수 있습니다.
