package main

// JWT の検証（Better Auth が発行した ES256 / RS256 の JWT。app/auth.py と同じ規則）。標準ライブラリだけ。
//   - 署名は JWKS の公開鍵だけで確かめる。alg=none・HS256 などは受け付けない
//   - exp・iss・aud・sub は必須。iss は AUTH_BASE_URL、aud は AUTH_AUDIENCE。時計のずれは 10 秒まで許す
//   - 利用者 ID は検証に通ったトークンの sub だけから取る（英数字と _ - の 8〜64 文字）
//   - トークンの中身・Authorization ヘッダーはログに出さない。残すのは失敗の種類だけ

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	algES256 = "ES256"
	algRS256 = "RS256"

	leeway             = 10 * time.Second
	keysTTL            = 600 * time.Second // 取得した鍵を使い続ける時間。過ぎたら取り直す（失敗したら前の鍵を使い続ける）
	minRefreshInterval = 60 * time.Second  // 未知の kid を理由にした取り直しの最小間隔
	jwksUserAgent      = "docunavi-read/1.0"
)

var userIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{8,64}$`)

// errAuthUnavailable は、鍵を取得できず検証できないこと（503 にする）。
var errAuthUnavailable = errors.New("auth unavailable")

// invalidToken は、トークンを受け付けないこと。理由はログ用で、クライアントへは返さない。
type invalidToken struct{ reason string }

func (e invalidToken) Error() string { return e.reason }

type jwkKey struct {
	alg string
	ec  *ecdsa.PublicKey
	rsa *rsa.PublicKey
}

type fetchFunc func(ctx context.Context, url string) ([]byte, error)

func fetchJWKS(ctx context.Context, url string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, jwksFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	// 既定の User-Agent は Cloudflare に弾かれることがある（app/auth.py と同じ理由）
	req.Header.Set("User-Agent", jwksUserAgent)
	req.Header.Set("Accept", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("jwks status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxJWKSBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxJWKSBytes {
		return nil, errors.New("jwks too large")
	}
	return body, nil
}

func b64(s string) ([]byte, error) { return base64.RawURLEncoding.DecodeString(s) }

// parseKeys は JWKS から、使ってよい鍵（EC P-256 または RSA で、kid があるもの）だけを kid で引ける形にする。
func parseKeys(document []byte) (map[string]jwkKey, error) {
	var doc struct {
		Keys []map[string]any `json:"keys"`
	}
	if err := json.Unmarshal(document, &doc); err != nil {
		return nil, err
	}
	keys := make(map[string]jwkKey)
	for _, item := range doc.Keys {
		kid, _ := item["kid"].(string)
		kty, _ := item["kty"].(string)
		if kid == "" {
			continue
		}
		declared, _ := item["alg"].(string)
		switch kty {
		case "EC":
			if crv, _ := item["crv"].(string); crv != "P-256" || (declared != "" && declared != algES256) {
				continue
			}
			xs, _ := item["x"].(string)
			ys, _ := item["y"].(string)
			x, errX := b64(xs)
			y, errY := b64(ys)
			if errX != nil || errY != nil || len(x) == 0 || len(x) > 32 || len(y) == 0 || len(y) > 32 {
				continue
			}
			point := make([]byte, 65)
			point[0] = 0x04
			copy(point[1+32-len(x):33], x)
			copy(point[33+32-len(y):65], y)
			pub, err := ecdsa.ParseUncompressedPublicKey(elliptic.P256(), point) // 曲線上の点かも確かめる
			if err != nil {
				continue
			}
			keys[kid] = jwkKey{alg: algES256, ec: pub}
		case "RSA":
			if declared != "" && declared != algRS256 {
				continue
			}
			ns, _ := item["n"].(string)
			es, _ := item["e"].(string)
			nb, errN := b64(ns)
			eb, errE := b64(es)
			if errN != nil || errE != nil || len(nb) == 0 || len(eb) == 0 || len(eb) > 4 {
				continue
			}
			e := new(big.Int).SetBytes(eb)
			if !e.IsInt64() || e.Int64() < 3 || e.Int64() > math.MaxInt32 {
				continue
			}
			keys[kid] = jwkKey{alg: algRS256, rsa: &rsa.PublicKey{N: new(big.Int).SetBytes(nb), E: int(e.Int64())}}
		}
	}
	return keys, nil
}

// jwksCache は JWKS の公開鍵の置き場。期限が来たら取り直し、未知の kid は間隔を空けて取り直す。
type jwksCache struct {
	url   string
	fetch fetchFunc
	now   func() time.Time
	logf  func(string, ...any)

	refreshMu sync.Mutex // 取り直しを 1 本にする
	mu        sync.RWMutex
	keys      map[string]jwkKey
	fetchedAt time.Time // 最後に取得に成功した時刻（ゼロ値 = まだ無い）
	attempted time.Time // 最後に取得を試みた時刻
}

func newJWKSCache(url string, fetch fetchFunc, now func() time.Time, logf func(string, ...any)) *jwksCache {
	return &jwksCache{url: url, fetch: fetch, now: now, logf: logf, keys: map[string]jwkKey{}}
}

// refreshLocked は refreshMu を持って呼ぶ。取得できなければ例外（前の鍵はそのまま残す）。
func (c *jwksCache) refreshLocked(ctx context.Context) (int, error) {
	c.mu.Lock()
	c.attempted = c.now()
	c.mu.Unlock()
	body, err := c.fetch(ctx, c.url)
	if err != nil {
		return 0, err
	}
	keys, err := parseKeys(body)
	if err != nil {
		return 0, err
	}
	c.mu.Lock()
	c.keys = keys
	c.fetchedAt = c.now()
	c.mu.Unlock()
	return len(keys), nil
}

// refresh は鍵を取り直して、使える鍵の数を返す。
func (c *jwksCache) refresh(ctx context.Context) (int, error) {
	c.refreshMu.Lock()
	defer c.refreshMu.Unlock()
	return c.refreshLocked(ctx)
}

func (c *jwksCache) lookup(kid string, now time.Time) (key jwkKey, found, expired bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	expired = c.fetchedAt.IsZero() || now.Sub(c.fetchedAt) >= keysTTL
	key, found = c.keys[kid]
	return key, found, expired
}

func (c *jwksCache) get(ctx context.Context, kid string) (jwkKey, bool, error) {
	if key, found, expired := c.lookup(kid, c.now()); !expired && found {
		return key, true, nil
	}
	c.refreshMu.Lock()
	defer c.refreshMu.Unlock()
	now := c.now()
	if key, found, expired := c.lookup(kid, now); !expired && found { // 待っている間に、ほかのリクエストが取り直した
		return key, true, nil
	}
	c.mu.RLock()
	recently := !c.attempted.IsZero() && now.Sub(c.attempted) < minRefreshInterval
	have := len(c.keys) > 0
	c.mu.RUnlock()
	if recently && have {
		key, found, _ := c.lookup(kid, now)
		return key, found, nil
	}
	if _, err := c.refreshLocked(ctx); err != nil {
		// 失敗の種類だけを残す（URL・本文は出さない）。前の鍵があれば、それで検証を続ける
		c.logf("JWKS を取得できませんでした (%T)", err)
		if !have {
			return jwkKey{}, false, errAuthUnavailable
		}
	}
	key, found, _ := c.lookup(kid, c.now())
	return key, found, nil
}

// tokenVerifier は JWT を検証して利用者 ID を返す。
type tokenVerifier struct {
	issuer   string
	audience string
	keys     *jwksCache
	now      func() time.Time
}

func decodePart(part string, into any) error {
	raw, err := b64(part)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, into)
}

// numericClaim は exp・nbf・iat のような数値のクレーム。無ければ found=false、数でなければ ok=false。
func numericClaim(claims map[string]any, name string) (value float64, found, ok bool) {
	raw, present := claims[name]
	if !present {
		return 0, false, true
	}
	switch v := raw.(type) {
	case float64:
		return v, true, true
	default:
		return 0, true, false
	}
}

func audienceMatches(raw any, want string) bool {
	switch v := raw.(type) {
	case string:
		return v == want
	case []any:
		for _, item := range v {
			if s, ok := item.(string); ok && s == want {
				return true
			}
		}
	}
	return false
}

// verify はトークンを検証して、利用者 ID（sub）を返す。受け付けないときは invalidToken、鍵を得られないときは errAuthUnavailable。
func (v *tokenVerifier) verify(ctx context.Context, token string) (string, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", invalidToken{"malformed"}
	}
	var header map[string]any
	if err := decodePart(parts[0], &header); err != nil || header == nil {
		return "", invalidToken{"malformed"}
	}
	alg, _ := header["alg"].(string)
	kid, _ := header["kid"].(string)
	if alg != algES256 && alg != algRS256 {
		return "", invalidToken{"algorithm"}
	}
	if kid == "" {
		return "", invalidToken{"kid"}
	}
	key, found, err := v.keys.get(ctx, kid)
	if err != nil {
		return "", err
	}
	if !found || key.alg != alg {
		return "", invalidToken{"unknown_key"}
	}
	signature, err := b64(parts[2])
	if err != nil {
		return "", invalidToken{"malformed"}
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	switch alg {
	case algES256:
		if len(signature) != 64 || !ecdsa.Verify(key.ec, digest[:], new(big.Int).SetBytes(signature[:32]), new(big.Int).SetBytes(signature[32:])) {
			return "", invalidToken{"InvalidSignatureError"}
		}
	case algRS256:
		if rsa.VerifyPKCS1v15(key.rsa, crypto.SHA256, digest[:], signature) != nil {
			return "", invalidToken{"InvalidSignatureError"}
		}
	}

	var claims map[string]any
	if err := decodePart(parts[1], &claims); err != nil || claims == nil {
		return "", invalidToken{"DecodeError"}
	}
	for _, name := range []string{"exp", "iss", "aud", "sub"} {
		if _, present := claims[name]; !present {
			return "", invalidToken{"MissingRequiredClaimError"}
		}
	}
	now := float64(v.now().UnixNano()) / 1e9
	lw := leeway.Seconds()
	for _, name := range []string{"iat", "nbf", "exp"} {
		value, found, ok := numericClaim(claims, name)
		if !ok {
			return "", invalidToken{"InvalidClaim:" + name}
		}
		if !found {
			continue
		}
		switch {
		case name == "exp" && value <= now-lw:
			return "", invalidToken{"ExpiredSignatureError"}
		case name != "exp" && value > now+lw:
			return "", invalidToken{"ImmatureSignatureError"}
		}
	}
	if iss, _ := claims["iss"].(string); iss != v.issuer {
		return "", invalidToken{"InvalidIssuerError"}
	}
	if !audienceMatches(claims["aud"], v.audience) {
		return "", invalidToken{"InvalidAudienceError"}
	}
	sub, _ := claims["sub"].(string)
	if !userIDPattern.MatchString(sub) {
		return "", invalidToken{"sub"}
	}
	return sub, nil
}
