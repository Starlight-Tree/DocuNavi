package main

import (
	"errors"
	"fmt"
	"math"
	"net"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	defaultAudience  = "docunavi"
	defaultCacheTTL  = 60 * time.Second
	defaultPoolMax   = 2
	defaultPort      = "8080"
	defaultRole      = "default"
	maxListItems     = 200 // 暴走した件数を返さないための上限（Python の _MAX_COLLECTIONS / _MAX_ITEMS と同じ）
	modeOff          = "off"
	modeShadow       = "shadow"
	modeEnforce      = "enforce"
	authRequired     = "required"
	maxJWKSBytes     = 1_000_000
	jwksFetchTimeout = 5 * time.Second
)

// config は環境変数から作る。get は os.Getenv を直接呼ばないための差し替え口（テストで使う）。
type config struct {
	Port           string
	DatabaseURL    string
	PoolMaxSize    int
	AuthMode       string // off / required
	AuthBaseURL    string
	JWKSURL        string
	Audience       string
	AccessMode     string // off / shadow / enforce
	MembershipMode string // off / shadow / enforce
	CacheTTL       time.Duration
	CORSOrigins    []string
}

func parseMode(get func(string) string, key string, allowed ...string) (string, error) {
	value := strings.ToLower(strings.TrimSpace(get(key)))
	if value == "" {
		return allowed[0], nil
	}
	for _, a := range allowed {
		if value == a {
			return value, nil
		}
	}
	return "", fmt.Errorf("%s は %s のどれかにしてください。", key, strings.Join(allowed, " / "))
}

func isLocalHost(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// parseAuthBaseURL は Python の auth_base_url と同じ規則（https。http はローカルだけ。パス・問い合わせ・断片は付けない）。
func parseAuthBaseURL(raw string) (string, error) {
	raw = strings.TrimRight(strings.TrimSpace(raw), "/")
	if raw == "" {
		return "", errors.New("AUTH_MODE=required には AUTH_BASE_URL が必要です。")
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.RawQuery != "" || u.Fragment != "" || u.Path != "" {
		return "", errors.New("AUTH_BASE_URL は https://<ホスト> の形にしてください（パスや問い合わせは付けない）。")
	}
	if u.Scheme == "http" && !isLocalHost(u.Hostname()) {
		return "", errors.New("AUTH_BASE_URL は https にしてください（http はローカルだけ）。")
	}
	return raw, nil
}

func parseCacheTTL(raw string) (time.Duration, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return defaultCacheTTL, nil
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || v < 0 || math.IsNaN(v) || math.IsInf(v, 0) {
		return 0, errors.New("ACCESS_CACHE_TTL_SECONDS は 0 以上の数（秒）にしてください。")
	}
	return time.Duration(v * float64(time.Second)), nil
}

func parseCORSOrigins(raw string) ([]string, error) {
	var out []string
	for _, o := range strings.Split(raw, ",") {
		o = strings.TrimSpace(o)
		if o == "" {
			continue
		}
		if o == "*" {
			return nil, errors.New("CORS_ORIGINS に * は使えません。許可する画面のオリジンを完全一致で並べてください。")
		}
		out = append(out, o)
	}
	return out, nil
}

func loadConfig(get func(string) string) (config, error) {
	var c config
	c.Port = strings.TrimSpace(get("PORT"))
	if c.Port == "" {
		c.Port = defaultPort
	}
	c.DatabaseURL = strings.TrimSpace(get("DATABASE_URL"))
	c.PoolMaxSize = defaultPoolMax
	if raw := strings.TrimSpace(get("DATABASE_POOL_MAX_SIZE")); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 {
			return c, errors.New("DATABASE_POOL_MAX_SIZE は 1 以上の整数です")
		}
		c.PoolMaxSize = n
	}
	var err error
	if c.AuthMode, err = parseMode(get, "AUTH_MODE", modeOff, authRequired); err != nil {
		return c, err
	}
	if c.AccessMode, err = parseMode(get, "ACCESS_MODE", modeOff, modeShadow, modeEnforce); err != nil {
		return c, err
	}
	if c.MembershipMode, err = parseMode(get, "MEMBERSHIP_MODE", modeOff, modeShadow, modeEnforce); err != nil {
		return c, err
	}
	if c.CacheTTL, err = parseCacheTTL(get("ACCESS_CACHE_TTL_SECONDS")); err != nil {
		return c, err
	}
	if c.CORSOrigins, err = parseCORSOrigins(get("CORS_ORIGINS")); err != nil {
		return c, err
	}
	c.Audience = strings.TrimSpace(get("AUTH_AUDIENCE"))
	if c.Audience == "" {
		c.Audience = defaultAudience
	}
	if c.AuthMode == authRequired {
		if c.AuthBaseURL, err = parseAuthBaseURL(get("AUTH_BASE_URL")); err != nil {
			return c, err
		}
		c.JWKSURL = strings.TrimSpace(get("AUTH_JWKS_URL"))
		if c.JWKSURL == "" {
			c.JWKSURL = c.AuthBaseURL + "/api/auth/jwks"
		}
	}
	if c.MembershipMode != modeOff && c.AuthMode == modeOff {
		return c, fmt.Errorf("MEMBERSHIP_MODE=%s には AUTH_MODE=required が必要です（匿名では利用者を区別できません）。", c.MembershipMode)
	}
	if (c.AccessMode != modeOff || c.MembershipMode != modeOff) && c.DatabaseURL == "" {
		return c, errors.New("ACCESS_MODE / MEMBERSHIP_MODE が off 以外のときは DATABASE_URL が必要です。")
	}
	return c, nil
}
