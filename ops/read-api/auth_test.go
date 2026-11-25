package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestVerifyAcceptsEs256AndRs256(t *testing.T) {
	k := newTestKeys(t)
	v, _ := newTestVerifier(t, k)
	for _, tc := range []struct{ alg, kid string }{{algES256, "k1"}, {algRS256, "k1-rsa"}} {
		sub, err := v.verify(context.Background(), k.mint(t, tc.alg, tc.kid, goodClaims(nil)))
		if err != nil || sub != "user_abcdefgh1234" {
			t.Fatalf("%s: sub=%q err=%v", tc.alg, sub, err)
		}
	}
}

func TestVerifyRejects(t *testing.T) {
	k := newTestKeys(t)
	v, _ := newTestVerifier(t, k)
	good := func(o map[string]any) string { return k.mint(t, algES256, "k1", goodClaims(o)) }
	exp := func(d time.Duration) float64 { return float64(testNow.Add(d).Unix()) }
	signed := good(nil)
	tampered := signed[:len(signed)-4] + "AAAA"
	cases := []struct {
		name   string
		token  string
		reason string
	}{
		{"malformed", "not.a.jwt.at.all", "malformed"},
		{"two parts", "aaa.bbb", "malformed"},
		{"alg none", k.mint(t, "none", "k1", goodClaims(nil)), "algorithm"},
		{"alg HS256 (public key as secret)", k.mint(t, "HS256", "k1", goodClaims(nil)), "algorithm"},
		{"no kid", k.mint(t, algES256, "", goodClaims(nil)), "kid"},
		{"unknown kid", k.mint(t, algES256, "other", goodClaims(nil)), "unknown_key"},
		{"alg does not match the key", k.mint(t, algRS256, "k1", goodClaims(nil)), "unknown_key"},
		{"bad signature", tampered, "InvalidSignatureError"},
		{"expired", good(map[string]any{"exp": exp(-11 * time.Second)}), "ExpiredSignatureError"},
		{"missing exp", good(map[string]any{"exp": nil}), "MissingRequiredClaimError"},
		{"missing iss", good(map[string]any{"iss": nil}), "MissingRequiredClaimError"},
		{"missing aud", good(map[string]any{"aud": nil}), "MissingRequiredClaimError"},
		{"missing sub", good(map[string]any{"sub": nil}), "MissingRequiredClaimError"},
		{"wrong issuer", good(map[string]any{"iss": "https://evil.example"}), "InvalidIssuerError"},
		{"wrong audience", good(map[string]any{"aud": "other"}), "InvalidAudienceError"},
		{"not yet valid (nbf)", good(map[string]any{"nbf": exp(30 * time.Second)}), "ImmatureSignatureError"},
		{"issued in the future", good(map[string]any{"iat": exp(30 * time.Second)}), "ImmatureSignatureError"},
		{"exp is a string", good(map[string]any{"exp": "9999999999"}), "InvalidClaim:exp"},
		{"sub with a colon", good(map[string]any{"sub": "user:abcdefgh1234"}), "sub"},
		{"sub too short", good(map[string]any{"sub": "short"}), "sub"},
		{"sub is not a string", good(map[string]any{"sub": 12345678}), "sub"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := v.verify(context.Background(), tc.token)
			var invalid invalidToken
			if !errors.As(err, &invalid) || invalid.reason != tc.reason {
				t.Fatalf("err=%v, want invalidToken{%s}", err, tc.reason)
			}
		})
	}
}

func TestVerifyAllowsLeewayAndAudienceList(t *testing.T) {
	k := newTestKeys(t)
	v, _ := newTestVerifier(t, k)
	// 時計のずれは 10 秒まで許す（exp が 9 秒前でも通る。PyJWT と同じ）
	token := k.mint(t, algES256, "k1", goodClaims(map[string]any{"exp": float64(testNow.Add(-9 * time.Second).Unix())}))
	if _, err := v.verify(context.Background(), token); err != nil {
		t.Fatalf("leeway: %v", err)
	}
	token = k.mint(t, algES256, "k1", goodClaims(map[string]any{"aud": []any{"other", "docunavi"}}))
	if _, err := v.verify(context.Background(), token); err != nil {
		t.Fatalf("aud list: %v", err)
	}
}

func TestParseKeysKeepsOnlyUsableKeys(t *testing.T) {
	k := newTestKeys(t)
	keys, err := parseKeys(k.jwks(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 2 || keys["k1"].alg != algES256 || keys["k1-rsa"].alg != algRS256 {
		t.Fatalf("keys=%v", keys)
	}
	// 曲線上にない点は捨てる
	bad := []byte(`{"keys":[{"kty":"EC","crv":"P-256","kid":"x","x":"AQ","y":"AQ"}]}`)
	if keys, _ := parseKeys(bad); len(keys) != 0 {
		t.Fatalf("a point off the curve was accepted: %v", keys)
	}
}

// ---- JWKS の取り直しの規則 ----

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func TestJWKSCacheRefreshRules(t *testing.T) {
	k := newTestKeys(t)
	body := k.jwks(t)
	c := &clock{t: testNow}
	fetches := 0
	var failure error
	cache := newJWKSCache("u", func(context.Context, string) ([]byte, error) {
		fetches++
		return body, failure
	}, c.now, quietLog(t))
	ctx := context.Background()

	if _, found, err := cache.get(ctx, "k1"); !found || err != nil || fetches != 1 {
		t.Fatalf("first get: found=%v err=%v fetches=%d", found, err, fetches)
	}
	cache.get(ctx, "k1") // 期限内の既知の kid は、取り直さない
	if fetches != 1 {
		t.Fatalf("a known kid refetched: %d", fetches)
	}
	cache.get(ctx, "unknown") // 未知の kid でも、最小間隔（60 秒）の内は取り直さない
	if fetches != 1 {
		t.Fatalf("unknown kid amplified the fetch: %d", fetches)
	}
	c.t = c.t.Add(61 * time.Second)
	cache.get(ctx, "unknown")
	if fetches != 2 {
		t.Fatalf("unknown kid after the interval: %d", fetches)
	}
	c.t = c.t.Add(601 * time.Second) // 期限切れ。取り直しに失敗しても、前の鍵で検証を続ける
	failure = errors.New("down")
	if _, found, err := cache.get(ctx, "k1"); !found || err != nil || fetches != 3 {
		t.Fatalf("expired + fetch failure: found=%v err=%v fetches=%d", found, err, fetches)
	}
}

func TestJWKSCacheWithoutKeysIsUnavailable(t *testing.T) {
	cache := newJWKSCache("u", func(context.Context, string) ([]byte, error) { return nil, errors.New("down") }, nowFunc(), quietLog(t))
	if _, _, err := cache.get(context.Background(), "k1"); !errors.Is(err, errAuthUnavailable) {
		t.Fatalf("err=%v", err)
	}
}
