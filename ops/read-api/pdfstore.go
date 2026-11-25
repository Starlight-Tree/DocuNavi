package main

// 資料の PDF を探す（app/pdf_originals.py の移植）。`documents` の行（source・content_hash）→ PDF のバイト列。
//   - source は、置き場の中の安全な相対パス（`a.pdf` か `dir/a.pdf`。部品は 3 個まで）だけ。絶対パス・空の部品・`.` / `..`・バックスラッシュ・
//     NUL・制御文字を含むものは「見つからない」
//   - 置き場の直下を資料群として走査し、`<資料群>/<source>` に完全に一致するものを候補にする。中身の sha256 が content_hash と一致するものだけを PDF とする
//     （名前が同じでも中身が違えば返さない）。返すバイト列は、ハッシュを確かめた、そのバイト列
//   - PDF_STORE=r2 は R2（キー `<資料群>/<source>`）。読めなかった（設定の不足・通信・権限）ときは unavailable（見つからない・食い違いとは区別する）。
//     ローカルへは切り替えない（食い違った版を見せないため）。それ以外は local（PDF_DIR。シンボリックリンクはたどらない）
//   - `/chunks` の original_pdf（返せるかだけ）は、取り込み側が DB に記録した original_verified_at を先に見る。記録が無いときだけ、その場で確かめる
//     （r2 のときは結果をプロセス内に控える。app/pdf_originals.py の original_available と同じ時間）

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"
)

const (
	maxOriginalBytes        = 20 * 1024 * 1024
	maxHeroBytes            = 5 * 1024 * 1024
	maxSourceSegments       = 3
	maxSourceChars          = 255
	checkCacheOKTTL         = 600 * time.Second
	checkCacheNegativeTTL   = 60 * time.Second
	checkCacheMaxEntries    = 1024
	defaultPDFDir           = "storage/pdf"
	originalMissing         = "missing"
	originalMismatch        = "mismatch"
	originalUnavailableCode = "unavailable"
)

// original は、PDF の探索結果。ok のときだけ data がある。
type original struct {
	ok     bool
	reason string // missing / mismatch / unavailable
	data   []byte
}

func missingOriginal() original { return original{reason: originalMissing} }

// sourceParts は、source を、置き場の中の安全な相対パスの部品に分ける。使えない名前なら nil。
func sourceParts(source string) []string {
	if n := utf8.RuneCountInString(source); n == 0 || n > maxSourceChars { // Python は文字数
		return nil
	}
	parts := strings.Split(source, "/")
	if len(parts) > maxSourceSegments {
		return nil
	}
	for _, part := range parts {
		if part == "" || part == "." || part == ".." || strings.Contains(part, `\`) {
			return nil
		}
		for _, r := range part {
			if r < 32 || r == 127 {
				return nil
			}
		}
	}
	return parts
}

// readRegularFile は、通常のファイルだけを、リンクをたどらずに読む。読めない・大きすぎる・通常のファイルでないなら nil。
func readRegularFile(path string) []byte {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil { // 無い・権限がない・リンク（O_NOFOLLOW）
		return nil
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > maxOriginalBytes {
		return nil
	}
	data, err := io.ReadAll(io.LimitReader(f, maxOriginalBytes+1))
	if err != nil || len(data) > maxOriginalBytes {
		return nil
	}
	return data
}

// fileIn は、directory の下の parts（相対パス）の通常のファイル。途中のディレクトリがリンクなら読まない。
func fileIn(directory string, parts []string) []byte {
	current := directory
	for _, part := range parts[:len(parts)-1] {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return nil
		}
	}
	return readRegularFile(filepath.Join(current, parts[len(parts)-1]))
}

// collectionDirs は、置き場 root の直下の資料群のディレクトリ（名前順）。リンクの行き先が root の直下でないなら除く。
func collectionDirs(root string) []string {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	var out []string
	for _, name := range names {
		resolved, err := filepath.EvalSymlinks(filepath.Join(root, name))
		if err != nil {
			continue
		}
		info, err := os.Stat(resolved)
		if err != nil || !info.IsDir() || filepath.Dir(resolved) != root {
			continue
		}
		out = append(out, resolved)
	}
	return out
}

func wantedHash(contentHash *string) string {
	if contentHash == nil {
		return ""
	}
	return strings.ToLower(strings.TrimSpace(*contentHash))
}

func hashOf(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

type cacheEntry struct {
	expires time.Time
	ok      bool
}

type cacheKey struct{ source, hash string }

// pdfStore は PDF の置き場（local か r2）。テストでは r2 の URL と時計を差し替える。
type pdfStore struct {
	useR2 bool
	dir   string
	r2    *r2Client
	logf  func(string, ...any)
	now   func() time.Time

	mu    sync.Mutex
	cache map[cacheKey]cacheEntry
}

func newPDFStore(cfg config, logf func(string, ...any)) *pdfStore {
	// R2 の窓口は、PDF_STORE に関わらず作る（ヒーロー画像は、PDF_STORE=local でも R2 から読む）。設定は使うときに読む
	return &pdfStore{
		useR2: cfg.PDFStore == pdfStoreR2, dir: cfg.PDFDir, r2: newR2Client(r2Settings{get: cfg.get}), logf: logf, now: time.Now,
		cache: map[cacheKey]cacheEntry{},
	}
}

func (s *pdfStore) findInR2(ctx context.Context, source string, contentHash *string) original {
	wanted := wantedHash(contentHash)
	collections, err := s.r2.collections(ctx)
	if err != nil {
		s.logf("R2 から PDF を読めませんでした (%T)", err)
		return original{reason: originalUnavailableCode}
	}
	seen := false
	for _, collection := range collections {
		data, found, err := s.r2.get(ctx, collection+"/"+source, maxOriginalBytes)
		if err != nil {
			s.logf("R2 から PDF を読めませんでした (%T)", err)
			return original{reason: originalUnavailableCode}
		}
		if !found {
			continue
		}
		seen = true
		if wanted != "" && hashOf(data) == wanted {
			return original{ok: true, data: data}
		}
	}
	if seen {
		s.logf("PDF の内容が、DB に保存された資料と一致しません")
		return original{reason: originalMismatch}
	}
	return missingOriginal()
}

// find は、source と content_hash に合う PDF を探す。見つからなければ missing。名前の合うファイルはあるが、どれも内容が違う
// （または、DB にハッシュが無く確かめられない）なら mismatch。複数が一致したときは中身が同一なので、名前順で先のものを返す。
func (s *pdfStore) find(ctx context.Context, source string, contentHash *string) original {
	parts := sourceParts(source)
	if parts == nil {
		return missingOriginal()
	}
	if s.useR2 {
		return s.findInR2(ctx, source, contentHash)
	}
	root, err := filepath.EvalSymlinks(s.dir)
	if err != nil {
		return missingOriginal()
	}
	if abs, err := filepath.Abs(root); err == nil {
		root = abs
	}
	wanted := wantedHash(contentHash)
	seen := false
	for _, directory := range collectionDirs(root) {
		data := fileIn(directory, parts)
		if data == nil {
			continue
		}
		seen = true
		if wanted != "" && hashOf(data) == wanted {
			return original{ok: true, data: data}
		}
	}
	if seen {
		s.logf("PDF の内容が、DB に保存された資料と一致しません")
		return original{reason: originalMismatch}
	}
	return missingOriginal()
}

// available は、find(...).ok と同じ答え（`/chunks` の original_pdf だけが使う。PDF のバイト列は要らない）。
// r2 のときだけ、結果をプロセス内に控える。返せた（ok）は 600 秒、見つからない・食い違いは 60 秒。**読めなかった（unavailable）は控えない**。
// 限界: R2 の PDF が、DB のハッシュを変えずに差し替わったとき、最大この秒数だけ古い答え（「返せる」）が出る。/file は毎回確かめる。
func (s *pdfStore) available(ctx context.Context, source string, contentHash *string) bool {
	if !s.useR2 {
		return s.find(ctx, source, contentHash).ok
	}
	key := cacheKey{source, wantedHash(contentHash)}
	now := s.now()
	s.mu.Lock()
	if hit, ok := s.cache[key]; ok && hit.expires.After(now) {
		s.mu.Unlock()
		return hit.ok
	}
	s.mu.Unlock()
	found := s.find(ctx, source, contentHash)
	if found.reason != originalUnavailableCode {
		ttl := checkCacheNegativeTTL
		if found.ok {
			ttl = checkCacheOKTTL
		}
		s.mu.Lock()
		if len(s.cache) >= checkCacheMaxEntries {
			for k, v := range s.cache {
				if !v.expires.After(now) {
					delete(s.cache, k)
				}
			}
			if len(s.cache) >= checkCacheMaxEntries {
				for k := range s.cache {
					delete(s.cache, k)
					break
				}
			}
		}
		s.cache[key] = cacheEntry{expires: now.Add(ttl), ok: found.ok}
		s.mu.Unlock()
	}
	return found.ok
}

// hero は、R2 のヒーロー画像（PDF_STORE に関わらず R2。app/api/documents.py と同じ）。無い・大きすぎるなら (nil, false, nil)。
func (s *pdfStore) hero(ctx context.Context, key string) ([]byte, bool, error) {
	return s.r2.get(ctx, key, maxHeroBytes)
}
