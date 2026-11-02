# 疑似の上流のイメージ（負荷試験用。試験が終わったらサービスもイメージも消す）。標準ライブラリだけなので、静的にビルドして distroless に置く。
# ビルドの文脈は ops/sse-fake-upstream/（cloudbuild.yaml が指す）。
FROM golang:1.27.2 AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /sse-fake-upstream .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /sse-fake-upstream /sse-fake-upstream
EXPOSE 8080
# 待ち受けは 8080 の h1 と h2c。流れ方・上限は Cloud Run の --args で渡す（-default-scenario=… -max-duration=120s）
ENTRYPOINT ["/sse-fake-upstream", "-listen=:8080"]
