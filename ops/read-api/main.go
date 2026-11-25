// docunavi-read: DocuNavi の読み取り API（Go）。資料グループ・対象・資料種別の一覧を、画面が直接呼ぶ。
// 決定と範囲は docs/decisions/0017-go-read-api-for-speed.md。チャット・会話の発言・書き込みは Python の API に残す。
package main

import (
	"context"
	"errors"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	logger := log.New(os.Stderr, "read: ", log.LstdFlags|log.Lmicroseconds)
	logf := logger.Printf
	if err := run(os.Getenv, logf); err != nil {
		logf("起動に失敗しました: %v", err)
		os.Exit(1)
	}
}

// build は設定から、サーバーと後始末を作る。DB と JWKS の確認（起動時の検査）もここで行う。
func build(ctx context.Context, cfg config, logf func(string, ...any)) (*server, func(), error) {
	var db store
	cleanup := func() {}
	if cfg.DatabaseURL != "" {
		pg, err := openStore(ctx, cfg.DatabaseURL, cfg.PoolMaxSize)
		if err != nil {
			return nil, cleanup, err
		}
		db = pg
		cleanup = pg.close
		// 接続を 1 本温める。失敗しても起動は続ける（資料の API は、その間 503 の固定文）。権限の検査が要るときは、下で失敗にする
		warmCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		if err := pg.ping(warmCtx); err != nil {
			logf("DB の接続を確認できませんでした (%T)", err)
		}
		cancel()
	}
	if err := verifyAccessConfig(ctx, cfg, db, logf); err != nil {
		cleanup()
		return nil, func() {}, err
	}
	s := &server{cfg: cfg, db: db, logf: logf, checker: newAccessChecker(cfg, db, time.Now, logf), pdfs: newPDFStore(cfg, logf)}
	if cfg.AuthMode == authRequired {
		cache := newJWKSCache(cfg.JWKSURL, fetchJWKS, time.Now, logf)
		count, err := cache.refresh(ctx) // 起動時に取っておく（最初の要求が鍵の取得を待たない）
		if err != nil {
			cleanup()
			return nil, func() {}, errors.New("JWKS を取得できませんでした (" + errorType(err) + ")。AUTH_BASE_URL を確認してください。")
		}
		if count == 0 {
			cleanup()
			return nil, func() {}, errors.New("JWKS に公開鍵（ES256 / RS256）がありません。共有秘密の検証には切り替えません。")
		}
		logf("認証: 必須（AUTH_MODE=required。JWKS の公開鍵 %d 個）", count)
		s.verifier = &tokenVerifier{issuer: cfg.AuthBaseURL, audience: cfg.Audience, keys: cache, now: time.Now}
	} else {
		logf("認証: 無効（AUTH_MODE=off。だれでも読める。公開するときは required にする）")
	}
	return s, cleanup, nil
}

func run(get func(string) string, logf func(string, ...any)) error {
	cfg, err := loadConfig(get)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	s, cleanup, err := build(ctx, cfg, logf)
	if err != nil {
		return err
	}
	defer cleanup()
	srv := &http.Server{
		Addr:              net.JoinHostPort("", cfg.Port),
		Handler:           s,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	logf("待ち受け: %s（DB %s、プール上限 %d）", srv.Addr, map[bool]string{true: "あり", false: "なし"}[cfg.DatabaseURL != ""], cfg.PoolMaxSize)
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return srv.Shutdown(shutdownCtx)
}
