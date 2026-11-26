// Package f2p0003 holds the target-side probes for batch BATCH-F2P-0003.
//
// Every probe executes the concrete entry point named by the matching F2P
// observation against the reference Go implementation. The probes must be
// copied into the migrated repository (the module root) before they are run,
// e.g. as <repo>/f2p0003/probes_test.go, so that they can import the module
// packages and the internal test environment.
package f2p0003

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	core "github.com/stremio/stremio-core"
	addontransport "github.com/stremio/stremio-core/addon_transport"
	deeplinks "github.com/stremio/stremio-core/deep_links"
	"github.com/stremio/stremio-core/internal/testenv"
	"github.com/stremio/stremio-core/models"
	"github.com/stremio/stremio-core/models/common"
	"github.com/stremio/stremio-core/runtime"
	"github.com/stremio/stremio-core/runtime/msg"
	"github.com/stremio/stremio-core/types"
)

// probeTransport is a deterministic addon transport for the probes.
type probeTransport struct {
	resources map[string][]byte
}

func (t probeTransport) Resource(path types.ResourcePath) (types.ResourceResponse, *runtime.EnvError) {
	if raw, ok := t.resources[path.Resource]; ok {
		return types.ResourceResponse{Raw: raw}, nil
	}
	return types.ResourceResponse{Raw: []byte("{}")}, nil
}

func (t probeTransport) Manifest() (types.Manifest, *runtime.EnvError) {
	return types.Manifest{}, nil
}

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

// repoRoot walks up from the working directory to the module root.
func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("go.mod not found above the probe package")
		}
		dir = parent
	}
}

func gitPorcelain(t *testing.T) []string {
	t.Helper()
	root := repoRoot(t)
	out, err := exec.Command("git", "-C", root, "status", "--porcelain").CombinedOutput()
	if err != nil {
		t.Fatalf("git status: %v: %s", err, out)
	}
	var paths []string
	for _, line := range strings.Split(strings.TrimRight(string(out), "\n"), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		rest := strings.TrimSpace(line[2:])
		if idx := strings.Index(rest, " -> "); idx >= 0 {
			rest = rest[idx+4:]
		}
		paths = append(paths, strings.Trim(rest, `"`))
	}
	return paths
}

// F2P-067 -------------------------------------------------------------------
func TestF2P067StreamsBucketWire(t *testing.T) {
	empty := types.NewStreamsBucket(nil)
	b, err := json.Marshal(empty)
	if err != nil {
		t.Fatalf("marshal empty: %v", err)
	}
	if string(b) != `{"uid":null,"items":[]}` {
		t.Fatalf("empty bucket wire = %s", b)
	}

	uid := "u1"
	bucket := types.NewStreamsBucket(&uid)
	bucket.Items[types.StreamsItemKey{MetaID: "tt1", VideoID: "tt1:1:1"}] = types.StreamsItem{
		Stream: types.Stream{Source: types.StreamSource{Kind: types.StreamSourceURL, URL: "http://stream"}},
		Type:   "movie", MetaID: "tt1", VideoID: "tt1:1:1",
	}
	b2, err := json.Marshal(bucket)
	if err != nil {
		t.Fatalf("marshal bucket: %v", err)
	}
	if !strings.HasPrefix(string(b2), `{"uid":"u1","items":[[`) {
		t.Fatalf("one-item bucket must be a key/value pair array, got %s", b2)
	}
	if strings.Contains(string(b2), `"items":{`) {
		t.Fatalf("one-item bucket must not be a JSON object, got %s", b2)
	}

	var back types.StreamsBucket
	if err := json.Unmarshal(b2, &back); err != nil {
		t.Fatalf("unmarshal bucket: %v", err)
	}
	b3, err := json.Marshal(back)
	if err != nil {
		t.Fatalf("re-marshal: %v", err)
	}
	if string(b3) != string(b2) {
		t.Fatalf("round-trip mismatch:\n got: %s\nwant: %s", b3, b2)
	}
}

// F2P-050 -------------------------------------------------------------------
func TestF2P050LoadableWire(t *testing.T) {
	if b, _ := json.Marshal(common.Loading[int]()); string(b) != `{"type":"Loading"}` {
		t.Fatalf("loading = %s", b)
	}
	if b, _ := json.Marshal(common.Ready(5)); string(b) != `{"type":"Ready","content":5}` {
		t.Fatalf("ready = %s", b)
	}
	b, _ := json.Marshal(common.Err[int](errors.New("boom")))
	if !strings.HasPrefix(string(b), `{"type":"Err","content":`) || !strings.HasSuffix(string(b), `}`) {
		t.Fatalf("err = %s", b)
	}
	if strings.Contains(string(b), `"Kind"`) || strings.Contains(string(b), `"Ready"`) {
		t.Fatalf("internal fields leaked: %s", b)
	}
}

// F2P-054 -------------------------------------------------------------------
func TestF2P054ResourceLoadableWire(t *testing.T) {
	content := common.Ready(map[string]any{"a": 1})
	resource := common.ResourceLoadable[map[string]any]{
		Request: types.ResourceRequest{
			Base: "https://addon/manifest.json",
			Path: types.ResourcePath{Resource: "meta", Type: "movie", ID: "tt1"},
		},
		Content: &content,
	}
	b, err := json.Marshal(resource)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(b), `"content":{"type":"Ready","content":{"a":1}}`) {
		t.Fatalf("resource loadable content tag missing: %s", b)
	}
}

// F2P-056 -------------------------------------------------------------------
func TestF2P056DeepLinkShapes(t *testing.T) {
	if got := deeplinks.NewSearchHistoryItemDeepLinks("foo bar").Search; got != "stremio:///search?query=foo%20bar" {
		t.Fatalf("search deep link = %q", got)
	}
	request := types.ResourceRequest{
		Base: "https://v3-cinemeta.strem.io/manifest.json",
		Path: types.ResourcePath{
			Resource: "catalog",
			Type:     "movie",
			ID:       "top",
			Extra:    []types.ExtraValue{{Name: "genre", Value: "Action"}},
		},
	}
	want := "stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json/movie/top?genre=Action"
	if got := deeplinks.NewDiscoverDeepLinks(request).Discover; got != want {
		t.Fatalf("discover deep link = %q, want %q", got, want)
	}
}

// F2P-066 -------------------------------------------------------------------
func TestF2P066QueryParamsEncode(t *testing.T) {
	got := types.QueryParamsEncode([]types.QueryPair{
		{Key: "sort", Value: "popularity"},
		{Key: "genre", Value: "A B"},
	})
	if got != "sort=popularity&genre=A%20B" {
		t.Fatalf("query params = %q", got)
	}
	if strings.Contains(got, "+") {
		t.Fatalf("space must be percent-encoded, not +: %q", got)
	}
}

// F2P-069 -------------------------------------------------------------------
func TestF2P069LegacyRequestAndError(t *testing.T) {
	url, envErr := addontransport.BuildLegacyReq(
		"https://legacywatchhub.strem.io/stremio/v1",
		types.ResourcePath{Resource: "stream", Type: "series", ID: "tt0386676:5:1"},
	)
	if envErr != nil {
		t.Fatalf("BuildLegacyReq: %v", envErr)
	}
	body := `{"id":1,"jsonrpc":"2.0","method":"stream.find","params":[null,{"query":{"episode":1,"imdb_id":"tt0386676","season":5,"type":"series"}}]}`
	want := "https://legacywatchhub.strem.io/stremio/v1/q.json?b=" + base64.StdEncoding.EncodeToString([]byte(body))
	if url != want {
		t.Fatalf("legacy url:\n got: %s\nwant: %s", url, want)
	}
	legacyErr := (&addontransport.LegacyErr{Kind: addontransport.LegacyJSONRPC, Code: 5, Message: "boom"}).ToEnvError()
	if legacyErr.Code != runtime.EnvErrorCodeAddonTransport {
		t.Fatalf("legacy error code = %d", legacyErr.Code)
	}
	if legacyErr.Message != "Addon protocol violation: rpc error 5: boom" {
		t.Fatalf("legacy error message = %q", legacyErr.Message)
	}
	b, _ := json.Marshal(legacyErr)
	if string(b) != `{"code":2,"message":"Addon protocol violation: rpc error 5: boom"}` {
		t.Fatalf("legacy error wire = %s", b)
	}
}

// F2P-059 -------------------------------------------------------------------
func TestF2P059ModelsNewSurface(t *testing.T) {
	env := testenv.New()
	model := models.New(env)
	if model.Ctx == nil || model.Player == nil || model.StreamingServer == nil {
		t.Fatalf("models.New must build the Ctx, Player and StreamingServer models")
	}
	rt, rx := runtime.New(model, nil, 16, env)
	if rt == nil || rx == nil {
		t.Fatalf("runtime.New must return a runtime and an event receiver")
	}
	if rt.Model() != model {
		t.Fatalf("runtime.Model accessor must return the constructed model")
	}
}

// F2P-060 -------------------------------------------------------------------
func TestF2P060UpdateAndUpdateField(t *testing.T) {
	env := testenv.New()
	whole := models.New(env)
	field := models.New(env)

	action := msg.LoadAction(msg.ModelPlayer, map[string]any{"stream": map[string]any{"title": "x"}})
	wholeEffects, wholeFields := whole.Update(msg.ActionMsg(action))
	fieldEffects, fieldFields := field.UpdateField(msg.ActionMsg(action), models.FieldPlayer)

	if !reflect.DeepEqual(wholeFields, fieldFields) {
		t.Fatalf("changed fields differ: whole=%v field=%v", wholeFields, fieldFields)
	}
	if !reflect.DeepEqual(wholeFields, []string{models.FieldPlayer}) {
		t.Fatalf("changed fields = %v", wholeFields)
	}
	if len(wholeEffects) != len(fieldEffects) {
		t.Fatalf("effects differ: whole=%d field=%d", len(wholeEffects), len(fieldEffects))
	}
}

// F2P-061 -------------------------------------------------------------------
func TestF2P061NewStateStableOrderAndRepeats(t *testing.T) {
	env := testenv.New()
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 16, env)

	action := msg.LoadAction(msg.ModelPlayer, map[string]any{"stream": map[string]any{"title": "same"}})

	// No dispatch -> no NewState.
	if events := drain(rx); len(events) != 0 {
		t.Fatalf("expected no events before dispatch, got %+v", events)
	}

	for _, load := range []int{1, 2} {
		rt.Dispatch(runtime.RuntimeAction{Action: action})
		events := drain(rx)
		if len(events) != 1 || events[0].Name != "NewState" {
			t.Fatalf("load %d events = %+v", load, events)
		}
		var fields []string
		if err := json.Unmarshal(events[0].Args, &fields); err != nil {
			t.Fatalf("unmarshal fields: %v", err)
		}
		if len(fields) != 1 || fields[0] != models.FieldPlayer {
			t.Fatalf("load %d changed fields = %v", load, fields)
		}
	}

	// A dispatch that changes no field must not emit NewState.
	fresh := models.New(env)
	freshRT, freshRX := runtime.New(fresh, nil, 16, env)
	freshRT.Dispatch(runtime.RuntimeAction{Action: msg.UnloadAction()})
	if events := drain(freshRX); len(events) != 0 {
		t.Fatalf("expected no event for a no-op dispatch, got %+v", events)
	}
}

// F2P-063 -------------------------------------------------------------------
func TestF2P063EngineResponsibilities(t *testing.T) {
	env := testenv.New()
	env.Transport = probeTransport{resources: map[string][]byte{
		"meta":   []byte(`{"meta":{"id":"tt1","type":"series","name":"Show","videos":[{"id":"tt1:1:1","title":"e1"},{"id":"tt1:1:2","title":"e2"}]}}`),
		"stream": []byte(`{"streams":[{"name":"Next","url":"http://next"}]}`),
	}}
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 64, env)

	selected := models.Selected{
		Stream:        types.Stream{Source: types.StreamSource{Kind: types.StreamSourceURL, URL: "http://cur"}},
		StreamRequest: &types.ResourceRequest{Base: "b", Path: types.ResourcePath{Resource: "stream", Type: "series", ID: "tt1:1:1"}},
		MetaRequest:   &types.ResourceRequest{Base: "b", Path: types.ResourcePath{Resource: "meta", Type: "series", ID: "tt1"}},
	}
	rt.Dispatch(runtime.RuntimeAction{Action: msg.LoadAction(msg.ModelPlayer, selected)})
	drain(rx)

	if model.Player.LibraryItem == nil {
		t.Fatalf("Player load must populate the library item")
	}
	if model.Ctx.Profile.Settings.StreamingServerURL == "" {
		t.Fatalf("Ctx profile persistence surface must be present")
	}
	if _, err := json.Marshal(model.Ctx.DismissedEvents); err != nil {
		t.Fatalf("dismissed events persistence surface must be present: %v", err)
	}
}

// F2P-049 -------------------------------------------------------------------
func TestF2P049PlayerDeepLinksFromLoad(t *testing.T) {
	env := testenv.New()
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 16, env)
	rt.Dispatch(runtime.RuntimeAction{Action: msg.LoadAction(msg.ModelPlayer, map[string]any{"stream": map[string]any{"title": "x"}})})
	drain(rx)

	links := deeplinks.NewMetaItemDeepLinks("series", "tt1")
	if links.MetaDetailsVideos == nil || *links.MetaDetailsVideos != "stremio:///detail/series/tt1" {
		t.Fatalf("player deep link = %+v", links)
	}
}

// F2P-053 -------------------------------------------------------------------
func TestF2P053PlayerLoadAndPersist(t *testing.T) {
	env := testenv.New()
	env.Transport = probeTransport{resources: map[string][]byte{
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

	if model.Player.LibraryItem == nil {
		t.Fatalf("expected a library item after Player load")
	}
	if model.Player.NextVideo == nil || model.Player.NextVideo.ID != "tt1:1:2" {
		t.Fatalf("expected next video tt1:1:2, got %+v", model.Player.NextVideo)
	}
}

// F2P-068 -------------------------------------------------------------------
func TestF2P068LoadableStates(t *testing.T) {
	env := testenv.New()
	request := types.ResourceRequest{Base: "https://addon/manifest.json", Path: types.ResourcePath{Resource: "meta", Type: "movie", ID: "tt1"}}
	resource := &common.ResourceLoadable[map[string]any]{}

	effects := common.ResourceUpdate(env, resource, common.ResourceRequested(&request), func(r types.ResourceResponse) (map[string]any, error) {
		var out map[string]any
		if err := json.Unmarshal(r.Raw, &out); err != nil {
			return nil, err
		}
		return out, nil
	})
	if resource.Content == nil || !resource.Content.IsLoading() {
		t.Fatalf("expected a loading state after the request")
	}
	if !effects.HasChanged {
		t.Fatalf("the request must mark the field as changed")
	}
	output := effects.Effects[0].Future.Run()
	if output.Kind != msg.KindInternal {
		t.Fatalf("future must resolve to an internal message")
	}
	result := output.Internal.Data.(msg.ResourceRequestResult)
	value := result.Value.(common.ResourceResult)
	readyEffects := common.ResourceUpdate(env, resource, common.ResourceRequestResult(&request, value), func(r types.ResourceResponse) (map[string]any, error) {
		var out map[string]any
		if err := json.Unmarshal(r.Raw, &out); err != nil {
			return nil, err
		}
		return out, nil
	})
	if resource.Content == nil || !resource.Content.IsReady() {
		t.Fatalf("expected a ready state after the result")
	}
	if !readyEffects.HasChanged {
		t.Fatalf("resolving a result must mark the field as changed")
	}
}

// F2P-062 -------------------------------------------------------------------
func TestF2P062RuntimeEventAndMigration(t *testing.T) {
	ev := runtime.NewStateEvent([]string{"ctx", "player"})
	b, _ := json.Marshal(ev)
	if string(b) != `{"name":"NewState","args":["ctx","player"]}` {
		t.Fatalf("runtime event wire = %s", b)
	}

	env := testenv.New()
	env.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`1`)
	env.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"settings":{"interface_language":"eng","streaming_server_url":"http://127.0.0.1:11470"}}`)
	if err := runtime.MigrateStorageSchema(env); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	var version uint32
	if err := json.Unmarshal(env.Storage[core.SCHEMA_VERSION_STORAGE_KEY], &version); err != nil {
		t.Fatalf("version: %v", err)
	}
	if version != core.SCHEMA_VERSION {
		t.Fatalf("version = %d, want %d", version, core.SCHEMA_VERSION)
	}
	var profile struct {
		Settings map[string]any `json:"settings"`
	}
	if err := json.Unmarshal(env.Storage[core.PROFILE_STORAGE_KEY], &profile); err != nil {
		t.Fatalf("profile: %v", err)
	}
	if _, ok := profile.Settings["interfaceLanguage"]; !ok {
		t.Fatalf("expected interfaceLanguage rename")
	}
	if _, ok := profile.Settings["gpuVideoProcessing"]; !ok {
		t.Fatalf("expected gpuVideoProcessing from v25")
	}
	if _, ok := profile.Settings["interface_language"]; ok {
		t.Fatalf("legacy interface_language must be removed")
	}

	down := testenv.New()
	down.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`26`)
	if err := runtime.MigrateStorageSchema(down); err == nil || err.Code != runtime.EnvErrorCodeStorageSchemaVersionDowngrade {
		t.Fatalf("expected downgrade error, got %v", err)
	}
}

// F2P-064 -------------------------------------------------------------------
func TestF2P064EnvErrorWire(t *testing.T) {
	fetch := runtime.EnvErrorFetch("x")
	b, _ := json.Marshal(fetch)
	if string(b) != `{"code":1,"message":"Failed to fetch: x"}` {
		t.Fatalf("fetch wire = %s", b)
	}
	other := runtime.EnvErrorOther("x")
	b, _ = json.Marshal(other)
	if string(b) != `{"code":1001,"message":"Other error: x"}` {
		t.Fatalf("other wire = %s", b)
	}
}

// F2P-051 -------------------------------------------------------------------
func TestF2P051EnvErrorCodes(t *testing.T) {
	cases := []struct {
		err  *runtime.EnvError
		code int
	}{
		{runtime.EnvErrorFetch("x"), 1},
		{runtime.EnvErrorAddonTransport("x"), 2},
		{runtime.EnvErrorSerde("x"), 3},
		{runtime.EnvErrorStorageUnavailable(), 4},
		{runtime.EnvErrorStorageSchemaVersionDowngrade(30, 25), 5},
		{runtime.EnvErrorStorageSchemaVersionUpgrade(runtime.EnvErrorOther("x")), 6},
		{runtime.EnvErrorStorageReadError("x"), 7},
		{runtime.EnvErrorStorageWriteError("x"), 8},
		{runtime.EnvErrorOther("x"), 1001},
	}
	for _, c := range cases {
		if c.err.Code != c.code {
			t.Errorf("code = %d, want %d", c.err.Code, c.code)
		}
		b, _ := json.Marshal(c.err)
		if !strings.HasPrefix(string(b), `{"code":`) || !strings.Contains(string(b), `,"message":`) {
			t.Errorf("wire = %s", b)
		}
		if strings.Contains(string(b), `"Code"`) || strings.Contains(string(b), `"Message"`) {
			t.Errorf("internal field names leaked: %s", b)
		}
	}
}

// F2P-071 -------------------------------------------------------------------
func TestF2P071RuntimeEventWire(t *testing.T) {
	state := runtime.NewStateEvent([]string{"ctx", "player"})
	b, _ := json.Marshal(state)
	if string(b) != `{"name":"NewState","args":["ctx","player"]}` {
		t.Fatalf("new state wire = %s", b)
	}
	coreEvent := runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil))
	b, _ = json.Marshal(coreEvent)
	if string(b) != `{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}` {
		t.Fatalf("core event wire = %s", b)
	}
}

// F2P-072 -------------------------------------------------------------------
func TestF2P072EnvErrorAndRuntimeEvent(t *testing.T) {
	b, _ := json.Marshal(runtime.EnvErrorFetch("x"))
	if string(b) != `{"code":1,"message":"Failed to fetch: x"}` {
		t.Fatalf("env error wire = %s", b)
	}
	b, _ = json.Marshal(runtime.NewStateEvent([]string{"ctx", "player"}))
	if string(b) != `{"name":"NewState","args":["ctx","player"]}` {
		t.Fatalf("runtime event wire = %s", b)
	}
}

// F2P-057 -------------------------------------------------------------------
func TestF2P057EnvOperations(t *testing.T) {
	envType := reflect.TypeOf((*runtime.Env)(nil)).Elem()
	required := []string{
		"Fetch", "GetStorage", "SetStorage",
		"ExecConcurrent", "ExecSequential",
		"Now", "LocalNow",
		"FlushAnalytics", "AnalyticsContext",
	}
	for _, name := range required {
		if _, ok := envType.MethodByName(name); !ok {
			t.Fatalf("runtime.Env is missing required operation %q", name)
		}
	}
}

// F2P-052 / F2P-058 / F2P-065 / F2P-070 --------------------------------
func assertInScopeChanges(t *testing.T) {
	t.Helper()
	for _, path := range gitPorcelain(t) {
		slash := filepath.ToSlash(path)
		if slash == "stremio-core-web" || strings.HasPrefix(slash, "stremio-core-web/") {
			t.Fatalf("out-of-scope change under stremio-core-web: %s", slash)
		}
		if strings.HasSuffix(slash, ".wasm") || strings.HasSuffix(slash, "bridge.js") || strings.HasSuffix(slash, "worker.js") || strings.HasSuffix(slash, "_bg.wasm") {
			t.Fatalf("out-of-scope wasm/browser change: %s", slash)
		}
	}
}

func TestF2P052ChangeScopeLimited(t *testing.T)  { assertInScopeChanges(t) }
func TestF2P058NoCapabilityRemoved(t *testing.T) { assertInScopeChanges(t) }
func TestF2P065RuntimeEventScope(t *testing.T)   { assertInScopeChanges(t) }
func TestF2P070EnvErrorScope(t *testing.T)       { assertInScopeChanges(t) }

// F2P-055 -------------------------------------------------------------------
func TestF2P055WebWasmUntouched(t *testing.T) {
	root := repoRoot(t)
	out, err := exec.Command("git", "-C", root, "status", "--porcelain", "--", "stremio-core-web").CombinedOutput()
	if err != nil {
		t.Fatalf("git status: %v: %s", err, out)
	}
	if strings.TrimSpace(string(out)) != "" {
		t.Fatalf("stremio-core-web must remain byte-identical, got:\n%s", out)
	}
}
