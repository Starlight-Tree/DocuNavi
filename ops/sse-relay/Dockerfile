# SSE 中継のイメージ（ops/sse-relay/README.md）。標準ライブラリだけなので、静的にビルドして distroless に置く。
# ビルドの文脈は ops/sse-relay/（cloudbuild.yaml が指す）。
FROM golang:1.26.10 AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /sse-relay .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /sse-relay /sse-relay
EXPOSE 8080
# 上流の URL は Cloud Run の --args で渡す（-upstream=https://…）。待ち受けは 8080 の h1 と h2c
ENTRYPOINT ["/sse-relay", "-listen=:8080"]
