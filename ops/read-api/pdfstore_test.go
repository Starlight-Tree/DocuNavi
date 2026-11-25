package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func sum(s string) *string { h := hashOf([]byte(s)); return &h }

// tree は、PDF の置き場（<資料群>/<ファイル>）を作る。
func tree(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	root, _ = filepath.EvalSymlinks(root)
	for name, body := range files {
		path := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func localStore(dir string) *pdfStore {
	cfg := baseConfig()
	cfg.PDFDir = dir
	return newPDFStore(cfg, func(string, ...any) {})
}

func TestSourcePartsAcceptOnlySafeRelativeNames(t *testing.T) {
	good := []string{"a.pdf", "dir/a.pdf", "a/b/c.pdf", "日本語 資料.pdf", strings.Repeat("あ", 255)}
	for _, s := range good {
		if sourceParts(s) == nil {
			t.Errorf("%q must be usable", s)
		}
	}
	bad := []string{"", "/a.pdf", "a//b.pdf", "a/", "./a.pdf", "../a.pdf", "a/../b.pdf", `a\b.pdf`, "a\x00b.pdf", "a\nb.pdf", "a\x7fb.pdf", "a/b/c/d.pdf", strings.Repeat("あ", 256)}
	for _, s := range bad {
		if sourceParts(s) != nil {
			t.Errorf("%q must be rejected", s)
		}
	}
}

func TestLocalFindChecksNameAndHash(t *testing.T) {
	root := tree(t, map[string]string{"cafe/menu.pdf": "MENU", "guides/menu.pdf": "OTHER", "guides/sub/x.pdf": "X"})
	s := localStore(root)
	ctx := t.Context()
	if got := s.find(ctx, "menu.pdf", sum("MENU")); !got.ok || string(got.data) != "MENU" {
		t.Fatalf("hash match in the first collection: %+v", got)
	}
	if got := s.find(ctx, "menu.pdf", sum("OTHER")); !got.ok || string(got.data) != "OTHER" {
		t.Fatalf("hash match in a later collection: %+v", got)
	}
	if got := s.find(ctx, "menu.pdf", sum("NEITHER")); got.ok || got.reason != originalMismatch {
		t.Fatalf("same name, different content must be a mismatch: %+v", got)
	}
	if got := s.find(ctx, "menu.pdf", nil); got.ok || got.reason != originalMismatch {
		t.Fatalf("no hash in the DB cannot be confirmed: %+v", got)
	}
	if got := s.find(ctx, "none.pdf", sum("MENU")); got.ok || got.reason != originalMissing {
		t.Fatalf("missing: %+v", got)
	}
	if got := s.find(ctx, "sub/x.pdf", sum("X")); !got.ok {
		t.Fatalf("a source may be dir/name (guides/sub/x.pdf): %+v", got)
	}
	if got := s.find(ctx, "../etc/passwd", sum("MENU")); got.ok || got.reason != originalMissing {
		t.Fatalf("path traversal: %+v", got)
	}
	upper := strings.ToUpper(*sum("MENU"))
	padded := " " + upper + "\n"
	if got := s.find(ctx, "menu.pdf", &padded); !got.ok {
		t.Fatalf("the stored hash is compared case-insensitively after trimming: %+v", got)
	}
}

func TestLocalFindDoesNotFollowSymlinks(t *testing.T) {
	root := tree(t, map[string]string{"cafe/real.pdf": "REAL"})
	outside := tree(t, map[string]string{"stolen/secret.pdf": "SECRET", "x.pdf": "X"})
	mustLink := func(target, link string) {
		t.Helper()
		if err := os.Symlink(target, link); err != nil {
			t.Fatal(err)
		}
	}
	mustLink(filepath.Join(root, "cafe", "real.pdf"), filepath.Join(root, "cafe", "alias.pdf"))
	mustLink(filepath.Join(outside, "stolen"), filepath.Join(root, "escape")) // 置き場の外へ出る資料群
	mustLink(filepath.Join(root, "cafe"), filepath.Join(root, "inside"))      // 置き場の中だが、直下ではない行き先を指す別名
	mustLink(filepath.Join(outside, "x.pdf"), filepath.Join(root, "cafe", "leak.pdf"))
	s := localStore(root)
	for name, content := range map[string]string{"alias.pdf": "REAL", "secret.pdf": "SECRET", "leak.pdf": "X"} {
		if got := s.find(t.Context(), name, sum(content)); got.ok {
			t.Errorf("%s was served through a symlink", name)
		}
	}
	if got := s.find(t.Context(), "real.pdf", sum("REAL")); !got.ok {
		t.Fatalf("the regular file must still be served: %+v", got)
	}
}

func TestLocalFindWithoutAStoreIsMissing(t *testing.T) {
	if got := localStore(filepath.Join(t.TempDir(), "none")).find(t.Context(), "a.pdf", sum("A")); got.ok || got.reason != originalMissing {
		t.Fatalf("%+v", got)
	}
}

func r2PDFStore(t *testing.T, f *fakeR2, env map[string]string) *pdfStore {
	t.Helper()
	cfg := baseConfig()
	cfg.PDFStore = pdfStoreR2
	cfg.get = func(k string) string { return env[k] }
	s := newPDFStore(cfg, func(string, ...any) {})
	s.r2.endpoint = f.srv.URL
	s.r2.sleep = func(time.Duration) {}
	return s
}

func TestR2FindUsesTheSameRules(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"cafe/menu.pdf": []byte("MENU"), "guides/menu.pdf": []byte("OTHER"), "hero/menu.pdf": []byte("HERO")})
	s := r2PDFStore(t, f, r2Env("key1", "secret1"))
	ctx := t.Context()
	if got := s.find(ctx, "menu.pdf", sum("OTHER")); !got.ok || string(got.data) != "OTHER" {
		t.Fatalf("%+v", got)
	}
	if got := s.find(ctx, "menu.pdf", sum("HERO")); got.ok || got.reason != originalMismatch {
		t.Fatalf("the hero/ prefix is not a collection: %+v", got)
	}
	if got := s.find(ctx, "none.pdf", sum("x")); got.reason != originalMissing {
		t.Fatalf("%+v", got)
	}
	// 設定が足りない → 読めなかった（見つからない・食い違いとは区別する。ローカルへは切り替えない）
	broken := r2PDFStore(t, f, map[string]string{})
	if got := broken.find(ctx, "menu.pdf", sum("MENU")); got.ok || got.reason != originalUnavailableCode {
		t.Fatalf("%+v", got)
	}
}

func TestAvailableCachesOnlyForR2AndNeverCachesUnavailable(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"cafe/menu.pdf": []byte("MENU")})
	s := r2PDFStore(t, f, r2Env("key1", "secret1"))
	now := time.Date(2026, 11, 25, 12, 0, 0, 0, time.UTC)
	s.now = func() time.Time { return now }
	ctx := t.Context()
	if !s.available(ctx, "menu.pdf", sum("MENU")) {
		t.Fatal("must be available")
	}
	calls := f.requests.Load()
	if !s.available(ctx, "menu.pdf", sum("MENU")) || f.requests.Load() != calls {
		t.Fatalf("the second answer must come from the cache: %d -> %d", calls, f.requests.Load())
	}
	now = now.Add(checkCacheOKTTL + time.Second)
	if !s.available(ctx, "menu.pdf", sum("MENU")) || f.requests.Load() == calls {
		t.Fatal("the entry must expire after 600 seconds")
	}
	// 見つからない: 60 秒だけ控える
	if s.available(ctx, "menu.pdf", sum("OTHER")) {
		t.Fatal("mismatch must not be available")
	}
	calls = f.requests.Load()
	s.available(ctx, "menu.pdf", sum("OTHER"))
	if f.requests.Load() != calls {
		t.Fatal("negative answers are cached for a short time")
	}
	now = now.Add(checkCacheNegativeTTL + time.Second)
	s.available(ctx, "menu.pdf", sum("OTHER"))
	if f.requests.Load() == calls {
		t.Fatal("negative answers expire after 60 seconds")
	}
	// 読めなかった結果は控えない: R2 が戻ったら、すぐ正しい答えにする
	f.fail.Store(1000)
	if s.available(ctx, "other.pdf", sum("x")) {
		t.Fatal("unreadable must be false")
	}
	f.fail.Store(0)
	f.mu.Lock()
	f.objects["cafe/other.pdf"] = []byte("x")
	f.mu.Unlock()
	if !s.available(ctx, "other.pdf", sum("x")) {
		t.Fatal("an unreadable answer must not have been cached")
	}
}

func TestAvailableDoesNotCacheLocally(t *testing.T) {
	root := tree(t, map[string]string{"cafe/a.pdf": "A"})
	s := localStore(root)
	if !s.available(t.Context(), "a.pdf", sum("A")) {
		t.Fatal("available")
	}
	if err := os.Remove(filepath.Join(root, "cafe", "a.pdf")); err != nil {
		t.Fatal(err)
	}
	if s.available(t.Context(), "a.pdf", sum("A")) {
		t.Fatal("the local store is read every time (tests swap the directory)")
	}
}
