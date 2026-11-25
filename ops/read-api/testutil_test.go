package main

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"sync"
	"testing"
	"time"
)

var testNow = time.Date(2026, 11, 25, 12, 0, 0, 0, time.UTC)

func nowFunc() func() time.Time { return func() time.Time { return testNow } }

func quietLog(*testing.T) func(string, ...any) { return func(string, ...any) {} }

// ---- 署名つきトークンの組み立て ----

type testKeys struct {
	es  *ecdsa.PrivateKey
	rs  *rsa.PrivateKey
	kid string
}

func newTestKeys(t *testing.T) *testKeys {
	t.Helper()
	es, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	rs, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return &testKeys{es: es, rs: rs, kid: "k1"}
}

func enc(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func (k *testKeys) jwks(t *testing.T) []byte {
	t.Helper()
	point, err := k.es.PublicKey.Bytes()
	if err != nil {
		t.Fatal(err)
	}
	doc := map[string]any{"keys": []map[string]any{
		{"kty": "EC", "crv": "P-256", "kid": k.kid, "x": enc(point[1:33]), "y": enc(point[33:65])},
		{"kty": "RSA", "kid": k.kid + "-rsa", "n": enc(k.rs.N.Bytes()), "e": enc(big.NewInt(int64(k.rs.E)).Bytes())},
		{"kty": "oct", "kid": "sym", "k": "c2VjcmV0"},       // 共有秘密は捨てる
		{"kty": "EC", "crv": "P-384", "kid": "p384"},        // 許可しない曲線は捨てる
		{"kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"}, // kid が無いものは捨てる
	}}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// mint は、alg に応じて署名したトークンを作る。claims の値で上書きできる。
func (k *testKeys) mint(t *testing.T, alg, kid string, claims map[string]any) string {
	t.Helper()
	header := map[string]any{"alg": alg, "typ": "JWT"}
	if kid != "" {
		header["kid"] = kid
	}
	hb, _ := json.Marshal(header)
	cb, _ := json.Marshal(claims)
	input := enc(hb) + "." + enc(cb)
	digest := sha256.Sum256([]byte(input))
	var sig []byte
	switch alg {
	case algES256:
		r, s, err := ecdsa.Sign(rand.Reader, k.es, digest[:])
		if err != nil {
			t.Fatal(err)
		}
		sig = append(r.FillBytes(make([]byte, 32)), s.FillBytes(make([]byte, 32))...)
	case algRS256:
		var err error
		if sig, err = rsa.SignPKCS1v15(rand.Reader, k.rs, crypto.SHA256, digest[:]); err != nil {
			t.Fatal(err)
		}
	default:
		sig = []byte("not-a-real-signature")
	}
	return input + "." + enc(sig)
}

func goodClaims(overrides map[string]any) map[string]any {
	claims := map[string]any{
		"sub": "user_abcdefgh1234",
		"iss": "https://auth.example.test",
		"aud": "docunavi",
		"exp": float64(testNow.Add(15 * time.Minute).Unix()),
		"iat": float64(testNow.Unix()),
	}
	for k, v := range overrides {
		if v == nil {
			delete(claims, k)
		} else {
			claims[k] = v
		}
	}
	return claims
}

func newTestVerifier(t *testing.T, k *testKeys) (*tokenVerifier, *jwksCache) {
	t.Helper()
	body := k.jwks(t)
	cache := newJWKSCache("https://auth.example.test/api/auth/jwks",
		func(context.Context, string) ([]byte, error) { return body, nil }, nowFunc(), quietLog(t))
	if _, err := cache.refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	return &tokenVerifier{issuer: "https://auth.example.test", audience: "docunavi", keys: cache, now: nowFunc()}, cache
}

// ---- 偽の DB ----

type fakeStore struct {
	mu             sync.Mutex
	collectionRows []collectionRow
	targetRows     []classificationRow
	typeRows       []classificationRow
	documentRows   []documentRow
	visible        []string
	members        map[string]bool
	err            error

	lastAllowed   []string
	lastAllowedOK bool // lastAllowed が nil（全グループ）でなく渡されたか
	visibleCalls  int
	memberCalls   int
	roles, groups int64
	assignments   int64
	memberTotal   int64
}

func (f *fakeStore) record(allowed []string) {
	f.lastAllowed = allowed
	f.lastAllowedOK = allowed != nil
}

func (f *fakeStore) collections(_ context.Context, allowed []string, _ int) ([]collectionRow, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record(allowed)
	return f.collectionRows, f.err
}

func (f *fakeStore) classifications(_ context.Context, kind classificationKind, allowed []string, _ int) ([]classificationRow, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record(allowed)
	if kind == kindDocTypes {
		return f.typeRows, f.err
	}
	return f.targetRows, f.err
}

func (f *fakeStore) documents(_ context.Context, allowed []string, _ int) ([]documentRow, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record(allowed)
	return f.documentRows, f.err
}

func (f *fakeStore) visibleCollections(context.Context, *string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.visibleCalls++
	return f.visible, f.err
}

func (f *fakeStore) isMember(_ context.Context, userID string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.memberCalls++
	return f.members[userID], f.err
}

func (f *fakeStore) probeAccess(context.Context) (int64, int64, int64, error) {
	return f.roles, f.groups, f.assignments, f.err
}
func (f *fakeStore) memberCount(context.Context) (int64, error) { return f.memberTotal, f.err }
func (f *fakeStore) ping(context.Context) error                 { return f.err }
func (f *fakeStore) close()                                     {}

var errBoom = errors.New("boom: secret connection string")
