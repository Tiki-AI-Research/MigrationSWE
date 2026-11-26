// Package f2p0002 contains the target-side F2P probes for BATCH-F2P-0002.
//
// Every probe executes a real call/load path of the migrated Go engine located
// at the read-only reference target /input/target (consumed through a filesystem
// replace), and asserts the frozen target postcondition. Source-side probes live
// in source/batches/BATCH-F2P-0002 and exercise the same semantic predicate on
// the Rust source at /input/source, where the Go-specific postcondition does not
// hold.
package f2p0002

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	core "github.com/stremio/stremio-core"
	addontransport "github.com/stremio/stremio-core/addon_transport"
	"github.com/stremio/stremio-core/deep_links"
	"github.com/stremio/stremio-core/models"
	"github.com/stremio/stremio-core/models/common"
	"github.com/stremio/stremio-core/runtime"
	"github.com/stremio/stremio-core/runtime/msg"
	"github.com/stremio/stremio-core/types"
)

const targetRoot = "/input/target"

// ---------------------------------------------------------------------------
// shared harness
// ---------------------------------------------------------------------------

// requireTargetTech asserts the frozen target technology (Go 1.23.0) is the
// technology actually used by the module under test. This is the same predicate
// that the source-side probe applies to /input/source, where it fails.
func requireTargetTech(t *testing.T) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(targetRoot, "go.mod"))
	if err != nil {
		t.Fatalf("target go.mod unreadable: %v", err)
	}
	if !strings.Contains(string(raw), "go 1.23.0") {
		t.Fatalf("target go.mod does not require Go 1.23.0: %s", raw)
	}
	out, err := exec.Command("go", "version").Output()
	if err != nil {
		t.Fatalf("go version: %v", err)
	}
	if !strings.HasPrefix(string(out), "go version go1.23") {
		t.Fatalf("toolchain is not Go 1.23.x: %s", strings.TrimSpace(string(out)))
	}
}

type probeEnv struct {
	storage   map[string]json.RawMessage
	transport runtime.AddonTransport
	fetchFn   func(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError)
}

func newEnv() *probeEnv { return &probeEnv{storage: map[string]json.RawMessage{}} }

func (e *probeEnv) Fetch(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError) {
	if e.fetchFn != nil {
		return e.fetchFn(method, url, headers, body)
	}
	return []byte("{}"), nil
}

func (e *probeEnv) GetStorage(key string) (json.RawMessage, bool, *runtime.EnvError) {
	v, ok := e.storage[key]
	return v, ok, nil
}

func (e *probeEnv) SetStorage(key string, value any) *runtime.EnvError {
	if value == nil {
		delete(e.storage, key)
		return nil
	}
	b, err := json.Marshal(value)
	if err != nil {
		return runtime.EnvErrorSerde(err.Error())
	}
	e.storage[key] = b
	return nil
}

func (e *probeEnv) ExecConcurrent(f func())                     { f() }
func (e *probeEnv) ExecSequential(f func())                     { f() }
func (e *probeEnv) Now() time.Time                              { return time.Unix(0, 0).UTC() }
func (e *probeEnv) LocalNow() time.Time                         { return e.Now() }
func (e *probeEnv) FlushAnalytics()                             {}
func (e *probeEnv) AnalyticsContext(_ any, _ any, _ string) any { return nil }
func (e *probeEnv) Log(string)                                  {}
func (e *probeEnv) MigrateStorageSchema() *runtime.EnvError     { return runtime.MigrateStorageSchema(e) }

func (e *probeEnv) AddonTransport(string) runtime.AddonTransport {
	if e.transport != nil {
		return e.transport
	}
	return staticTransport{}
}

type staticTransport struct{}

func (staticTransport) Resource(types.ResourcePath) (types.ResourceResponse, *runtime.EnvError) {
	return types.ResourceResponse{Raw: []byte("{}")}, nil
}
func (staticTransport) Manifest() (types.Manifest, *runtime.EnvError) { return types.Manifest{}, nil }

var _ runtime.Env = (*probeEnv)(nil)

type fakeTransport struct{ resources map[string][]byte }

func (t fakeTransport) Resource(path types.ResourcePath) (types.ResourceResponse, *runtime.EnvError) {
	if raw, ok := t.resources[path.Resource]; ok {
		return types.ResourceResponse{Raw: raw}, nil
	}
	return types.ResourceResponse{Raw: []byte("{}")}, nil
}
func (t fakeTransport) Manifest() (types.Manifest, *runtime.EnvError) { return types.Manifest{}, nil }

func drain(rx <-chan runtime.RuntimeEvent) []runtime.RuntimeEvent {
	var events []runtime.RuntimeEvent
	for {
		select {
		case ev := <-rx:
			events = append(events, ev)
		default:
			return events
		}
	}
}

func ptrString(s string) *string { return &s }

// assertEnvSurface introspects the target runtime.Env interface method set.
func assertEnvSurface(t *testing.T) {
	t.Helper()
	want := []string{
		"Fetch", "GetStorage", "SetStorage", "ExecConcurrent", "ExecSequential",
		"Now", "LocalNow", "FlushAnalytics", "AnalyticsContext", "AddonTransport",
		"MigrateStorageSchema",
	}
	typ := reflect.TypeOf((*runtime.Env)(nil)).Elem()
	names := map[string]bool{}
	for i := 0; i < typ.NumMethod(); i++ {
		names[typ.Method(i).Name] = true
	}
	for _, w := range want {
		if !names[w] {
			t.Fatalf("runtime.Env is missing operation %q (have %v)", w, names)
		}
	}
}

// assertWasmUntouched verifies no out-of-scope web/wasm/binding file changed.
func assertWasmUntouched(t *testing.T) {
	t.Helper()
	out, err := exec.Command("git", "-C", targetRoot, "status", "--porcelain", "--", "stremio-core-web").Output()
	if err != nil {
		t.Fatalf("git status stremio-core-web: %v", err)
	}
	if strings.TrimSpace(string(out)) != "" {
		t.Fatalf("stremio-core-web has changes: %s", out)
	}
	all, err := exec.Command("git", "-C", targetRoot, "status", "--porcelain").Output()
	if err != nil {
		t.Fatalf("git status: %v", err)
	}
	for _, line := range strings.Split(string(all), "\n") {
		for _, bad := range []string{".wasm", "bridge.js", "worker.js", "_bg.wasm"} {
			if strings.Contains(line, bad) {
				t.Fatalf("out-of-scope wasm/binding artifact changed: %q", line)
			}
		}
	}
}

// assertChangeScope verifies every changed path stays inside the in-scope Go
// modules and never touches stremio-core-web.
func assertChangeScope(t *testing.T) {
	t.Helper()
	allowed := []string{
		"constants", "runtime/", "models/", "types/", "addon_transport/",
		"deep_links/", "analytics/", "internal/", "go.mod",
	}
	out, err := exec.Command("git", "-C", targetRoot, "status", "--porcelain").Output()
	if err != nil {
		t.Fatalf("git status: %v", err)
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		p := strings.TrimSpace(line)
		if len(p) > 3 {
			p = strings.TrimSpace(p[3:])
		}
		if strings.HasPrefix(p, "stremio-core-web/") {
			t.Fatalf("out-of-scope change under stremio-core-web: %q", p)
		}
		if strings.HasSuffix(p, "_test.go") {
			continue
		}
		ok := false
		for _, a := range allowed {
			if strings.HasPrefix(p, a) {
				ok = true
				break
			}
		}
		if !ok {
			t.Fatalf("changed path outside the in-scope modules: %q", p)
		}
	}
}

func migrateToolchain(t *testing.T) {
	t.Helper()
	env := newEnv()
	env.storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`1`)
	env.storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"settings":{"interface_language":"eng","streaming_server_url":"http://127.0.0.1:11470"}}`)
	if err := runtime.MigrateStorageSchema(env); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	var version uint32
	if err := json.Unmarshal(env.storage[core.SCHEMA_VERSION_STORAGE_KEY], &version); err != nil {
		t.Fatalf("version: %v", err)
	}
	if version != 25 {
		t.Fatalf("version = %d, want 25", version)
	}
	var profile struct {
		Settings map[string]any `json:"settings"`
	}
	if err := json.Unmarshal(env.storage[core.PROFILE_STORAGE_KEY], &profile); err != nil {
		t.Fatalf("profile: %v", err)
	}
	if _, ok := profile.Settings["interfaceLanguage"]; !ok {
		t.Fatalf("expected interfaceLanguage rename, got %v", profile.Settings)
	}
	if _, ok := profile.Settings["gpuVideoProcessing"]; !ok {
		t.Fatalf("expected gpuVideoProcessing from v25, got %v", profile.Settings)
	}
	if _, ok := profile.Settings["interface_language"]; ok {
		t.Fatalf("legacy interface_language should have been removed")
	}

	downgrade := newEnv()
	downgrade.storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`26`)
	if err := runtime.MigrateStorageSchema(downgrade); err == nil || err.Code != runtime.EnvErrorCodeStorageSchemaVersionDowngrade {
		t.Fatalf("expected downgrade error code 5, got %v", err)
	}
}

func loadPlayer(t *testing.T) (*models.Model, *runtime.Runtime, <-chan runtime.RuntimeEvent) {
	t.Helper()
	env := newEnv()
	env.transport = fakeTransport{resources: map[string][]byte{
		"meta": []byte(`{"meta":{"id":"tt1","type":"series","name":"Show","videos":[{"id":"tt1:1:1","title":"e1"},{"id":"tt1:1:2","title":"e2"}]}}`),
	}}
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 64, env)
	selected := models.Selected{
		Stream:        types.Stream{Source: types.StreamSource{Kind: types.StreamSourceURL, URL: "http://stream"}},
		StreamRequest: &types.ResourceRequest{Base: "b", Path: types.ResourcePath{Resource: "stream", Type: "series", ID: "tt1:1:1"}},
		MetaRequest:   &types.ResourceRequest{Base: "b", Path: types.ResourcePath{Resource: "meta", Type: "series", ID: "tt1"}},
	}
	rt.Dispatch(runtime.RuntimeAction{Action: msg.LoadAction(msg.ModelPlayer, selected)})
	drain(rx)
	return model, rt, rx
}

// ---------------------------------------------------------------------------
// probes
// ---------------------------------------------------------------------------

// F2P-025 / R034
func TestF2P025LoadableWire(t *testing.T) {
	requireTargetTech(t)
	b, _ := json.Marshal(common.Loading[int]())
	if string(b) != `{"type":"Loading"}` {
		t.Fatalf("Loading wire = %s", b)
	}
	b, _ = json.Marshal(common.Ready(5))
	if string(b) != `{"type":"Ready","content":5}` {
		t.Fatalf("Ready wire = %s", b)
	}
	b, _ = json.Marshal(common.Err[int](wireErr{}))
	if string(b) != `{"type":"Err","content":{"msg":"x"}}` {
		t.Fatalf("Err wire = %s", b)
	}
}

type wireErr struct{}

func (wireErr) Error() string                { return "x" }
func (wireErr) MarshalJSON() ([]byte, error) { return []byte(`{"msg":"x"}`), nil }

// F2P-030 / R002
func TestF2P030NoRustEngineDependency(t *testing.T) {
	requireTargetTech(t)

	build := exec.Command("go", "build", "./...")
	build.Dir = targetRoot
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("CGO_ENABLED=0 go build ./... failed: %v\n%s", err, out)
	}

	list := exec.Command("go", "list", "-deps", "./...")
	list.Dir = targetRoot
	out, err := list.Output()
	if err != nil {
		t.Fatalf("go list -deps: %v", err)
	}
	for _, dep := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		dep = strings.TrimSpace(dep)
		if dep == "" {
			continue
		}
		head := strings.SplitN(dep, "/", 2)[0]
		if strings.Contains(head, ".") && !strings.HasPrefix(dep, "github.com/stremio/stremio-core") {
			t.Fatalf("non-stdlib Rust/foreign dependency in build graph: %q", dep)
		}
	}
}

// F2P-032 / R037
func TestF2P032StableNewStateAndReload(t *testing.T) {
	requireTargetTech(t)
	env := newEnv()
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 16, env)

	action := msg.LoadAction(msg.ModelPlayer, map[string]any{"stream": map[string]any{"title": "same"}})
	rt.Dispatch(runtime.RuntimeAction{Action: action})
	first := drain(rx)
	if len(first) != 1 || first[0].Name != "NewState" {
		t.Fatalf("first load events = %+v", first)
	}
	var fields []string
	if err := json.Unmarshal(first[0].Args, &fields); err != nil {
		t.Fatalf("unmarshal fields: %v", err)
	}
	if len(fields) != 1 || fields[0] != models.FieldPlayer {
		t.Fatalf("first load fields = %v, want [player]", fields)
	}

	rt.Dispatch(runtime.RuntimeAction{Action: action})
	second := drain(rx)
	if len(second) != 1 || second[0].Name != "NewState" {
		t.Fatalf("second identical load events = %+v", second)
	}
}

// F2P-034 / R030
func TestF2P034WatchedBitField(t *testing.T) {
	requireTargetTech(t)
	videos := []string{
		"tt2934286:1:1", "tt2934286:1:2", "tt2934286:1:3",
		"tt2934286:1:4", "tt2934286:1:5", "tt2934286:1:6",
		"tt2934286:1:7", "tt2934286:1:8", "tt2934286:1:9",
	}
	wb, err := types.WatchedBitFieldConstructAndResize("tt2934286:1:5:5:eJyTZwAAAEAAIA==", videos)
	if err != nil {
		t.Fatalf("construct: %v", err)
	}
	if !wb.GetVideo("tt2934286:1:5") {
		t.Fatalf("video 5 must decode as watched")
	}
	if wb.GetVideo("tt2934286:1:6") {
		t.Fatalf("video 6 must not decode as watched")
	}
	wb.SetVideo("tt2934286:1:6", true)
	reparsed, err := types.WatchedBitFieldConstructAndResize(wb.String(), videos)
	if err != nil {
		t.Fatalf("reparse: %v", err)
	}
	if !reparsed.GetVideo("tt2934286:1:5") || !reparsed.GetVideo("tt2934286:1:6") {
		t.Fatalf("reparsed watched bits differ")
	}
}

// F2P-037 / R031
func TestF2P037LegacyJSONRPC(t *testing.T) {
	requireTargetTech(t)
	got, err := addontransport.BuildLegacyReq(
		"https://legacywatchhub.strem.io/stremio/v1",
		types.ResourcePath{Resource: "stream", Type: "series", ID: "tt0386676:5:1"},
	)
	if err != nil {
		t.Fatalf("BuildLegacyReq: %v", err)
	}
	want := "https://legacywatchhub.strem.io/stremio/v1/q.json?b=eyJpZCI6MSwianNvbnJwYyI6IjIuMCIsIm1ldGhvZCI6InN0cmVhbS5maW5kIiwicGFyYW1zIjpbbnVsbCx7InF1ZXJ5Ijp7ImVwaXNvZGUiOjEsImltZGJfaWQiOiJ0dDAzODY2NzYiLCJzZWFzb24iOjUsInR5cGUiOiJzZXJpZXMifX1dfQ=="
	if got != want {
		t.Fatalf("legacy url mismatch:\n got: %s\nwant: %s", got, want)
	}
	mapped := (&addontransport.LegacyErr{Kind: addontransport.LegacyJSONRPC, Code: 5, Message: "boom"}).ToEnvError()
	if mapped.Code != 2 || mapped.Message != "Addon protocol violation: rpc error 5: boom" {
		t.Fatalf("legacy error mapping = %+v", mapped)
	}
}

// F2P-040 / R021
func TestF2P040NoRustSymbolsInProduction(t *testing.T) {
	requireTargetTech(t)
	dirs := []string{"runtime", "models", "types", "addon_transport", "analytics", "deep_links"}
	tokens := []string{`import "C"`, "//go:cgo", "stremio_core::", "#cgo", "cgo.Handle"}
	for _, d := range dirs {
		root := filepath.Join(targetRoot, d)
		err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
			if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".go") {
				return err
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			for _, tok := range tokens {
				if strings.Contains(string(data), tok) {
					t.Fatalf("Rust/cgo symbol %q found in production file %s", tok, path)
				}
			}
			return nil
		})
		if err != nil {
			t.Fatalf("walk %s: %v", root, err)
		}
	}
}

// F2P-028 / R032
func TestF2P028HTTPTransportAndUnsupported(t *testing.T) {
	requireTargetTech(t)
	env := newEnv()
	var requested string
	env.fetchFn = func(_, url string, _ map[string]string, _ []byte) ([]byte, *runtime.EnvError) {
		requested = url
		return []byte("{}"), nil
	}
	tr := addontransport.NewAddonHTTPTransport("https://x/manifest.json", env)
	if _, err := tr.Resource(types.ResourcePath{
		Resource: "catalog", Type: "movie", ID: "top",
		Extra: []types.ExtraValue{{Name: "genre", Value: "Action"}},
	}); err != nil {
		t.Fatalf("resource: %v", err)
	}
	if requested != "https://x/catalog/movie/top/genre=Action.json" {
		t.Fatalf("requested url = %q", requested)
	}
	bad := addontransport.NewAddonHTTPTransport("https://x/neither", env)
	if _, err := bad.Resource(types.ResourcePath{Resource: "catalog", Type: "movie", ID: "top"}); err == nil || err.Code != runtime.EnvErrorCodeAddonTransport {
		t.Fatalf("expected addon transport error, got %v", err)
	}
	if _, err := addontransport.NewUnsupportedTransport("x").Resource(types.ResourcePath{}); err == nil || err.Code != runtime.EnvErrorCodeAddonTransport {
		t.Fatalf("unsupported transport must return code 2, got %v", err)
	}
}

// F2P-031 / R027
func TestF2P031ActionEventWire(t *testing.T) {
	requireTargetTech(t)
	b, _ := json.Marshal(msg.NewAction("Unload", nil))
	if string(b) != `{"action":"Unload"}` {
		t.Fatalf("action wire = %s", b)
	}
	b, _ = json.Marshal(msg.LoadAction("Player", map[string]any{"stream": 1}))
	if string(b) != `{"action":"Load","args":{"model":"Player","args":{"stream":1}}}` {
		t.Fatalf("load action wire = %s", b)
	}
	b, _ = json.Marshal(msg.NewEvent("PlayerPlaying", nil))
	if string(b) != `{"event":"PlayerPlaying"}` {
		t.Fatalf("event wire = %s", b)
	}
}

// F2P-038 / R037
func TestF2P038ResourceLoadableStates(t *testing.T) {
	requireTargetTech(t)
	env := newEnv()
	req := types.ResourceRequest{Base: "b", Path: types.ResourcePath{Resource: "stream", Type: "series", ID: "tt1"}}
	convert := func(types.ResourceResponse) ([]string, error) { return []string{"a"}, nil }

	res := common.ResourceLoadable[[]string]{}
	eff := common.ResourceUpdate(env, &res, common.ResourceRequested(&req), convert)
	if res.Content == nil || !res.Content.IsLoading() {
		t.Fatalf("request must transition to Loading, got %+v", res.Content)
	}
	if !eff.HasChanged {
		t.Fatalf("request must report a changed field")
	}
	ok := common.ResourceResult{Response: &types.ResourceResponse{Raw: []byte("{}")}}
	if eff := common.ResourceUpdate(env, &res, common.ResourceRequestResult(&req, ok), convert); !eff.HasChanged {
		t.Fatalf("successful result must report a changed field")
	}
	if res.Content == nil || !res.Content.IsReady() {
		t.Fatalf("successful result must transition to Ready, got %+v", res.Content)
	}

	failing := common.ResourceLoadable[[]string]{}
	common.ResourceUpdate(env, &failing, common.ResourceRequested(&req), convert)
	bad := common.ResourceResult{Err: runtime.EnvErrorFetch("boom")}
	common.ResourceUpdate(env, &failing, common.ResourceRequestResult(&req, bad), convert)
	if failing.Content == nil || !failing.Content.IsErr() {
		t.Fatalf("fetch failure must produce an Err state, got %+v", failing.Content)
	}
}

// F2P-042 / R001
func TestF2P042EngineResponsibilities(t *testing.T) {
	requireTargetTech(t)
	model, rt, rx := loadPlayer(t)
	if model.Player.LibraryItem == nil {
		t.Fatalf("expected a populated library item")
	}
	video := types.Video{ID: "tt1:1:1", Title: "e1"}
	action := msg.NewAction(msg.ActionNamePlayer, map[string]any{
		"action": msg.ActionPlayerMarkVideoAsWatched,
		"args":   []any{video, true},
	})
	rt.Dispatch(runtime.RuntimeAction{Action: action})
	drain(rx)
	if model.Player.Watched == nil || !model.Player.Watched.GetVideo("tt1:1:1") {
		t.Fatalf("expected tt1:1:1 watched")
	}
	if model.Player.LibraryItem.State.Watched == nil {
		t.Fatalf("expected persisted watched state")
	}
}

// F2P-047 / R019
func TestF2P047DeepLinks(t *testing.T) {
	requireTargetTech(t)
	dl := deeplinks.NewMetaItemDeepLinks("movie", "tt123")
	if dl.MetaDetailsVideos == nil || *dl.MetaDetailsVideos != "stremio:///detail/movie/tt123" {
		t.Fatalf("meta item detail = %v", dl.MetaDetailsVideos)
	}
	withVideo := deeplinks.NewMetaItemDeepLinksWithVideo("series", "tt1", "s1e1")
	if withVideo.MetaDetailsStreams == nil || *withVideo.MetaDetailsStreams != "stremio:///detail/series/tt1/s1e1" {
		t.Fatalf("meta item streams = %v", withVideo.MetaDetailsStreams)
	}
	discover := deeplinks.NewDiscoverDeepLinks(types.ResourceRequest{
		Base: "https://v3-cinemeta.strem.io/manifest.json",
		Path: types.ResourcePath{Resource: "catalog", Type: "movie", ID: "top",
			Extra: []types.ExtraValue{{Name: "genre", Value: "Action"}}},
	})
	if discover.Discover != "stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json/movie/top?genre=Action" {
		t.Fatalf("discover = %q", discover.Discover)
	}
	if got := deeplinks.NewSearchHistoryItemDeepLinks("foo bar").Search; got != "stremio:///search?query=foo%20bar" {
		t.Fatalf("search = %q", got)
	}
	if got := deeplinks.NewCalendarDeepLinks(2024, 5).Calendar; got != "stremio:///calendar/2024/5" {
		t.Fatalf("calendar = %q", got)
	}
	typ := "movie"
	if got := deeplinks.NewLibraryDeepLinks("library", &typ, "popularity").Library; got != "stremio:///library/movie?sort=popularity" {
		t.Fatalf("library = %q", got)
	}
	if got := types.QueryParamsEncode([]types.QueryPair{{Key: "sort", Value: "popularity"}, {Key: "genre", Value: "A B"}}); got != "sort=popularity&genre=A%20B" {
		t.Fatalf("query params = %q", got)
	}
}

// F2P-048 / R027
func TestF2P048EffectSemantics(t *testing.T) {
	requireTargetTech(t)

	events := &fakeModel{effects: []runtime.Effect{runtime.MsgEffect(msg.EventMsg(msg.NewEvent("UserLoggedOut", nil)))}}
	eventsRT, rx := runtime.New(events, nil, 4, nil)
	eventsRT.Dispatch(runtime.RuntimeAction{Action: msg.NewAction(msg.ActionNameUnload, nil)})
	ev := <-rx
	if ev.Name != "CoreEvent" {
		t.Fatalf("event message must emit CoreEvent only, got %s", ev.Name)
	}
	if len(rx) != 0 {
		t.Fatalf("unexpected extra events")
	}

	model2 := &fakeModel{}
	_, rx2 := runtime.New(model2, []runtime.Effect{runtime.MsgEffect(msg.InternalMsg(msg.NewInternal(msg.InternalProfileChanged, nil)))}, 4, nil)
	if model2.updates != 1 {
		t.Fatalf("internal message must update the model, got %d", model2.updates)
	}
	if len(rx2) != 0 {
		t.Fatalf("expected no events for unchanged internal update")
	}

	panicking := &fakeModel{effects: []runtime.Effect{runtime.MsgEffect(msg.ActionMsg(msg.UnloadAction()))}}
	defer func() {
		if recover() == nil {
			t.Fatalf("an action returned by an effect must panic")
		}
	}()
	panicRT, _ := runtime.New(panicking, nil, 4, nil)
	panicRT.Dispatch(runtime.RuntimeAction{Action: msg.NewAction(msg.ActionNameUnload, nil)})
}

// F2P-026 / R026
func TestF2P026RuntimeEventWire(t *testing.T) {
	requireTargetTech(t)
	b, _ := json.Marshal(runtime.NewStateEvent([]string{"ctx", "player"}))
	if string(b) != `{"name":"NewState","args":["ctx","player"]}` {
		t.Fatalf("newstate wire = %s", b)
	}
	b, _ = json.Marshal(runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil)))
	if string(b) != `{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}` {
		t.Fatalf("coreevent wire = %s", b)
	}
}

// F2P-027 / R039
func TestF2P027ScopeUntouched(t *testing.T) {
	requireTargetTech(t)
	assertWasmUntouched(t)
}

// F2P-029 / R036
func TestF2P029EnvSurfaceAndMigration(t *testing.T) {
	requireTargetTech(t)
	assertEnvSurface(t)
	migrateToolchain(t)
}

// F2P-033 / R003
func TestF2P033RuntimeEventAndScope(t *testing.T) {
	requireTargetTech(t)
	b, _ := json.Marshal(runtime.NewStateEvent([]string{"ctx", "player"}))
	if string(b) != `{"name":"NewState","args":["ctx","player"]}` {
		t.Fatalf("newstate wire = %s", b)
	}
	assertWasmUntouched(t)
}

// F2P-035 / R003
func TestF2P035PublicSurfaceAndScope(t *testing.T) {
	requireTargetTech(t)
	_ = models.New
	_ = runtime.New
	_ = addontransport.NewAddonHTTPTransport
	_ = deeplinks.NewMetaItemDeepLinks
	if core.SCHEMA_VERSION != 25 {
		t.Fatalf("SCHEMA_VERSION = %d", core.SCHEMA_VERSION)
	}
	if core.PROFILE_STORAGE_KEY != "profile" || core.DISMISSED_EVENTS_STORAGE_KEY != "dismissed_events" {
		t.Fatalf("protected storage keys changed")
	}
	assertWasmUntouched(t)
}

// F2P-036 / R023
func TestF2P036EnvSurfaceAndChangeScope(t *testing.T) {
	requireTargetTech(t)
	assertEnvSurface(t)
	assertChangeScope(t)
}

// F2P-039 / R036
func TestF2P039PublicSurfaceAndMigration(t *testing.T) {
	requireTargetTech(t)
	_ = models.New
	_ = runtime.New
	_ = addontransport.NewAddonHTTPTransport
	migrateToolchain(t)
}

// F2P-043 / R039
func TestF2P043EnvSurfaceAndScope(t *testing.T) {
	requireTargetTech(t)
	assertEnvSurface(t)
	assertWasmUntouched(t)
}

// F2P-044 / R003
func TestF2P044IntegrityAndScope(t *testing.T) {
	requireTargetTech(t)
	// Non-weakened exact wire comparisons plus out-of-scope guard.
	b, _ := json.Marshal(runtime.NewStateEvent([]string{"ctx", "player"}))
	if string(b) != `{"name":"NewState","args":["ctx","player"]}` {
		t.Fatalf("newstate wire = %s", b)
	}
	b, _ = json.Marshal(runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil)))
	if string(b) != `{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}` {
		t.Fatalf("coreevent wire = %s", b)
	}
	assertWasmUntouched(t)
}

// F2P-045 / R028
func TestF2P045EnvErrorWire(t *testing.T) {
	requireTargetTech(t)
	b, _ := json.Marshal(runtime.EnvErrorFetch("x"))
	if string(b) != `{"code":1,"message":"Failed to fetch: x"}` {
		t.Fatalf("fetch wire = %s", b)
	}
	b, _ = json.Marshal(runtime.EnvErrorOther("x"))
	if string(b) != `{"code":1001,"message":"Other error: x"}` {
		t.Fatalf("other wire = %s", b)
	}
}

// F2P-046 / R028
func TestF2P046EnvSurfaceAndErrorWire(t *testing.T) {
	requireTargetTech(t)
	assertEnvSurface(t)
	b, _ := json.Marshal(runtime.EnvErrorFetch("x"))
	if string(b) != `{"code":1,"message":"Failed to fetch: x"}` {
		t.Fatalf("fetch wire = %s", b)
	}
}

// F2P-041 / R031
func TestF2P041LegacyRequestAndError(t *testing.T) {
	requireTargetTech(t)
	got, err := addontransport.BuildLegacyReq(
		"https://legacywatchhub.strem.io/stremio/v1",
		types.ResourcePath{Resource: "stream", Type: "series", ID: "tt0386676:5:1"},
	)
	if err != nil {
		t.Fatalf("BuildLegacyReq: %v", err)
	}
	want := "https://legacywatchhub.strem.io/stremio/v1/q.json?b=eyJpZCI6MSwianNvbnJwYyI6IjIuMCIsIm1ldGhvZCI6InN0cmVhbS5maW5kIiwicGFyYW1zIjpbbnVsbCx7InF1ZXJ5Ijp7ImVwaXNvZGUiOjEsImltZGJfaWQiOiJ0dDAzODY2NzYiLCJzZWFzb24iOjUsInR5cGUiOiJzZXJpZXMifX1dfQ=="
	if got != want {
		t.Fatalf("legacy url mismatch:\n got: %s\nwant: %s", got, want)
	}
	var resp addontransport.JSONRPCResp
	if err := json.Unmarshal([]byte(`{"error":{"code":5,"message":"boom"}}`), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	_, legacyErr := resp.Parse()
	if legacyErr == nil {
		t.Fatalf("expected a legacy rpc error")
	}
	mapped := legacyErr.ToEnvError()
	if mapped.Message != "Addon protocol violation: rpc error 5: boom" {
		t.Fatalf("mapped message = %q", mapped.Message)
	}
}

// fakeModel is the target-side runtime.Model used to observe effect semantics.
type fakeModel struct {
	effects []runtime.Effect
	fields  []string
	updates int
}

func (m *fakeModel) Update(*msg.Msg) ([]runtime.Effect, []string) {
	m.updates++
	return m.effects, m.fields
}

func (m *fakeModel) UpdateField(*msg.Msg, string) ([]runtime.Effect, []string) {
	m.updates++
	return m.effects, m.fields
}
