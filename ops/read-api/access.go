package main

// 資料グループのアクセス制御と利用資格（app/access.py の移植）。
//   - 見えるグループ = 利用者のロール（user_roles）が持つグループ（role_collection_access）の和集合。ロールが 1 つも無い利用者
//     （AUTH_MODE=off の匿名を含む）は default ロール。プロセス内に短い TTL（既定 60 秒）の控えを持つ
//   - ACCESS_MODE: off（制限なし。DB も読まない）/ shadow（制限せず、enforce なら何件が絞り込まれるかをログに出す）/
//     enforce（制限する。権限を引けなければ 503。制限なしには倒れない）
//   - MEMBERSHIP_MODE: 利用資格（app_members）。ACCESS_MODE とは独立。資格は資料の権限より先に確かめる
//   - 見えないものは「無い」のと同じに見せる。文はすべて固定文で、見えないグループの名前を載せない
//
// Python との違い: 見えるグループと一緒に「いま存在する全グループを含むか」を引く問い合わせ（app/access.py の _KNOWN_SQL）は、
// 会話の伏せ方（mask_hidden_turns）だけに使う。この読み取り API には会話が無いので、引かない（DB への往復が 1 本減る）。

import (
	"context"
	"errors"
	"net/http"
	"sort"
	"sync"
	"time"
)

const (
	membershipRequiredCode = "APP_MEMBERSHIP_REQUIRED"
	cacheMaxEntries        = 1024

	detailMembershipRequired    = "このアカウントには利用許可がありません。"
	detailMembershipUnavailable = "利用資格を確認できませんでした。しばらく待ってから再試行してください。"
	detailNoCollections         = "利用できる資料がありません。"
	detailAccessUnavailable     = "資料の利用権限を確認できませんでした。しばらく待ってから再試行してください。"
)

// scope は、検索・一覧が届くグループの範囲。all なら全グループ。空の names は何も見えない。
type scope struct {
	all    bool
	names  []string // 並びを固定（sorted）
	set    map[string]struct{}
	shadow map[string]struct{} // shadow のときだけ「enforce なら見える範囲」。制限には使わず、ログのためだけ
}

func scopeAll() scope { return scope{all: true} }

func scopeOnly(names []string) scope {
	sorted := append([]string{}, names...) // 空でも nil にしない（nil は「全グループ」になる）
	sort.Strings(sorted)
	set := make(map[string]struct{}, len(sorted))
	for _, n := range sorted {
		set[n] = struct{}{}
	}
	return scope{names: sorted, set: set}
}

func (s scope) isEmpty() bool { return !s.all && len(s.names) == 0 }

// allows は、このグループの資料を扱ってよいか。
func (s scope) allows(collection string) bool {
	if s.all {
		return true
	}
	_, ok := s.set[collection]
	return ok
}

// sqlNames は SQL の allowed に渡す値（nil は全グループ）。
func (s scope) sqlNames() []string {
	if s.all {
		return nil
	}
	return s.names
}

type access struct {
	mode  string
	scope scope
}

// requireAny は、見えるグループが 0 件なら 403（「資料なし」の空の結果にはしない）。
func (a access) requireAny() *httpError {
	if a.scope.isEmpty() {
		return newHTTPError(http.StatusForbidden, detailNoCollections)
	}
	return nil
}

type visibleEntry struct {
	expires time.Time
	names   []string
}

type memberEntry struct {
	expires time.Time
	member  bool
}

// accessChecker は 1 リクエストぶんのアクセス権（資格と範囲）を決める。控えはプロセス内だけ（Cloud Run のインスタンスごとに別）。
type accessChecker struct {
	accessMode     string
	membershipMode string
	ttl            time.Duration
	db             store // nil = DB の接続が無い
	now            func() time.Time
	logf           func(string, ...any)

	mu      sync.Mutex
	visible map[string]visibleEntry
	members map[string]memberEntry
}

func newAccessChecker(cfg config, db store, now func() time.Time, logf func(string, ...any)) *accessChecker {
	return &accessChecker{
		accessMode: cfg.AccessMode, membershipMode: cfg.MembershipMode, ttl: cfg.CacheTTL, db: db, now: now, logf: logf,
		visible: map[string]visibleEntry{}, members: map[string]memberEntry{},
	}
}

func anonKey(userID *string) string {
	if userID == nil {
		return "\x00anonymous" // 利用者 ID は [A-Za-z0-9_-] だけなので、重ならない
	}
	return *userID
}

// makeRoom は、上限に達していたら期限切れを捨て、それでも満杯なら 1 つ捨てる。mu を持って呼ぶ。
func makeRoom[V any](cache map[string]V, expired func(V) bool) {
	if len(cache) < cacheMaxEntries {
		return
	}
	for k, v := range cache {
		if expired(v) {
			delete(cache, k)
		}
	}
	if len(cache) >= cacheMaxEntries {
		for k := range cache { // 最も古いものとは限らないが、上限を守る（Python は入れた順）
			delete(cache, k)
			break
		}
	}
}

func (a *accessChecker) visibleGroups(ctx context.Context, userID *string) ([]string, error) {
	if a.db == nil {
		return nil, errors.New("DB の接続が開いていません")
	}
	key := anonKey(userID)
	now := a.now()
	a.mu.Lock()
	if hit, ok := a.visible[key]; ok && hit.expires.After(now) {
		a.mu.Unlock()
		return hit.names, nil
	}
	a.mu.Unlock()
	names, err := a.db.visibleCollections(ctx, userID)
	if err != nil {
		return nil, err
	}
	if a.ttl > 0 { // 引けなかった結果は控えない
		a.mu.Lock()
		makeRoom(a.visible, func(e visibleEntry) bool { return !e.expires.After(now) })
		a.visible[key] = visibleEntry{expires: now.Add(a.ttl), names: names}
		a.mu.Unlock()
	}
	return names, nil
}

func (a *accessChecker) isMember(ctx context.Context, userID *string) (bool, error) {
	if userID == nil {
		return false, nil // 匿名は資格なし
	}
	if a.db == nil {
		return false, errors.New("DB の接続が開いていません")
	}
	now := a.now()
	a.mu.Lock()
	if hit, ok := a.members[*userID]; ok && hit.expires.After(now) {
		a.mu.Unlock()
		return hit.member, nil
	}
	a.mu.Unlock()
	member, err := a.db.isMember(ctx, *userID)
	if err != nil {
		return false, err
	}
	if a.ttl > 0 { // 許可も不許可も控える
		a.mu.Lock()
		makeRoom(a.members, func(e memberEntry) bool { return !e.expires.After(now) })
		a.members[*userID] = memberEntry{expires: now.Add(a.ttl), member: member}
		a.mu.Unlock()
	}
	return member, nil
}

// enforceMembership は利用資格を確かめる。資格がなければ 403（code 付き）。
// off は何も読まない。shadow は拒否せず、enforce なら拒否される要求を（経路だけ）ログに出す。引けなければ、enforce は 503、shadow は続ける。
func (a *accessChecker) enforceMembership(ctx context.Context, userID *string, path string) *httpError {
	if a.membershipMode == modeOff {
		return nil
	}
	member, err := a.isMember(ctx, userID)
	if err != nil {
		a.logf("利用資格を引けませんでした (mode=%s, %T)", a.membershipMode, err)
		if a.membershipMode == modeShadow {
			return nil
		}
		return newHTTPError(http.StatusServiceUnavailable, detailMembershipUnavailable)
	}
	if member {
		return nil
	}
	if a.membershipMode == modeShadow {
		a.logf("利用資格(shadow): %s は enforce では拒否される（資格のない利用者）", path)
		return nil
	}
	e := newHTTPError(http.StatusForbidden, detailMembershipRequired)
	e.code = membershipRequiredCode
	return e
}

// request は、認証のあとで、利用資格と資料の範囲を決める。資料を扱う経路は、すべてここを通る。
func (a *accessChecker) request(ctx context.Context, userID *string, path string) (access, *httpError) {
	if e := a.enforceMembership(ctx, userID, path); e != nil {
		return access{}, e
	}
	if a.accessMode == modeOff {
		return access{mode: a.accessMode, scope: scopeAll()}, nil
	}
	names, err := a.visibleGroups(ctx, userID)
	if err != nil {
		a.logf("利用できる資料グループを引けませんでした (mode=%s, %T)", a.accessMode, err)
		if a.accessMode == modeShadow {
			return access{mode: a.accessMode, scope: scopeAll()}, nil // shadow は観測だけ。引けなくても、いまの挙動のまま続ける
		}
		return access{}, newHTTPError(http.StatusServiceUnavailable, detailAccessUnavailable)
	}
	if a.accessMode == modeShadow {
		s := scopeAll()
		s.shadow = scopeOnly(names).set
		return access{mode: a.accessMode, scope: s}, nil
	}
	return access{mode: a.accessMode, scope: scopeOnly(names)}, nil
}

// logShadow は shadow のとき、enforce なら何件が絞り込まれるかをログに出す（件数だけ。グループ名は出さない）。
func (a *accessChecker) logShadow(s scope, path string, collections []string) {
	if s.shadow == nil {
		return
	}
	hidden := 0
	for _, c := range collections {
		if _, ok := s.shadow[c]; !ok {
			hidden++
		}
	}
	a.logf("アクセス制御(shadow): %s 全 %d 件のうち %d 件が enforce では絞り込まれる", path, len(collections), hidden)
}

// verifyAccessConfig は起動時の検査。shadow / enforce で使えない状態（表・default ロールが無い）なら、起動を失敗させる。
func verifyAccessConfig(ctx context.Context, cfg config, db store, logf func(string, ...any)) error {
	if cfg.AccessMode != modeOff {
		if db == nil {
			return errors.New("ACCESS_MODE には DB の接続が必要です")
		}
		roles, groups, assignments, err := db.probeAccess(ctx)
		if err != nil {
			return errors.New("ロールの表を読めませんでした (" + errorType(err) + ")。マイグレーションが適用済みか確認してください。")
		}
		if roles == 0 {
			return errors.New("roles に default ロールがありません。マイグレーションの初期データを確認してください。")
		}
		if groups == 0 {
			logf("default ロールに資料グループが割り当てられていません。ロールの無い利用者は、何も見えません")
		}
		logf("アクセス制御: %s（user_roles の割り当て %d 件）", cfg.AccessMode, assignments)
	} else {
		logf("アクセス制御: 無効（ACCESS_MODE=off。利用者ごとの資料の制限はしない）")
	}
	if cfg.MembershipMode != modeOff {
		if db == nil {
			return errors.New("MEMBERSHIP_MODE には DB の接続が必要です")
		}
		members, err := db.memberCount(ctx)
		if err != nil {
			return errors.New("app_members を読めませんでした (" + errorType(err) + ")。マイグレーションが適用済みか確認してください。")
		}
		logf("利用資格: %s（app_members %d 件）", cfg.MembershipMode, members)
		if cfg.MembershipMode == modeEnforce && members == 0 {
			logf("利用資格: app_members が 0 件です。enforce では、全員が拒否されます")
		}
	}
	return nil
}
