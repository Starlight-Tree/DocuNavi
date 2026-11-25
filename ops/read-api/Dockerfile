# 読み取り API（ops/read-api/README.md）のイメージ。pgx 以外は標準ライブラリなので、静的にビルドして distroless に置く。
# ビルドの文脈は ops/read-api/（cloudbuild.yaml が指す）。依存は go.sum で固定し、go mod verify で内容を確かめてからビルドする。
FROM golang:1.27.3 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download && go mod verify
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /read-api .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /read-api /read-api
EXPOSE 8080
# 設定は Cloud Run の環境変数で渡す（README の「環境変数」の表。DATABASE_URL・R2_CREDENTIALS_FILE などの秘密は Secret として）。
# 待ち受けは $PORT（Cloud Run が渡す。既定 8080）。PDF は R2 から読む（PDF_STORE=r2）ので、イメージに PDF は入れない
ENTRYPOINT ["/read-api"]
