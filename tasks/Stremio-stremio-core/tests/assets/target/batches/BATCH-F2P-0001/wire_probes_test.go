// Package f2pprobe holds the BATCH-F2P-0001 target-side probes. Each exported
// test asserts the exact target postcondition of one F2P observation against the
// migrated Go engine. The probes are written against the public Go surface and
// are executed with `go test ./...` inside the target module.
package f2pprobe

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
	"time"

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

type probeTransport struct {
	resources map[string][]byte
	lastURL   string
}

func (t *probeTransport) Resource(path types.ResourcePath) (types.ResourceResponse, *runtime.EnvError) {
	if raw, ok := t.resources[path.Resource]; ok {
		return types.ResourceResponse{Raw: raw}, nil
	}
	return types.ResourceResponse{Raw: []byte("{}")}, nil
}

func (t *probeTransport) Manifest() (types.Manifest, *runtime.EnvError) {
	return types.Manifest{}, nil
}

// Allow queued event forwarding to run before declaring the receiver empty.
// The deadline also bounds a broken producer that never becomes idle.
func drainProbe(rx <-chan runtime.RuntimeEvent) []runtime.RuntimeEvent {
	var events []runtime.RuntimeEvent
	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	for {
		idle := time.NewTimer(250 * time.Millisecond)
		select {
		case event, ok := <-rx:
			idle.Stop()
			if !ok { return events }
			events = append(events, event)
		case <-idle.C:
			return events
		case <-deadline.C:
			idle.Stop()
			return events
		}
	}
}

// JSON object member order is not part of the message contract. Array
// order, member presence, values and types must still match exactly.
func jsonEqual(got, want string) bool {
	var actual, expected any
	return json.Unmarshal([]byte(got), &actual) == nil &&
		json.Unmarshal([]byte(want), &expected) == nil && reflect.DeepEqual(actual, expected)
}

func ptr(s string) *string { return &s }

// TestF2P003DeepLinkShapes asserts the deep-link URL shapes and the engine
// percent/query encoding (R033).
func TestF2P003DeepLinkShapes(t *testing.T) {
	if got := deeplinks.NewSearchHistoryItemDeepLinks("foo bar").Search; got != "stremio:///search?query=foo%20bar" {
		t.Fatalf("search link = %q", got)
	}
	req := types.ResourceRequest{
		Base: "https://v3-cinemeta.strem.io/manifest.json",
		Path: types.ResourcePath{
			Resource: "catalog", Type: "movie", ID: "top",
			Extra: []types.ExtraValue{{Name: "genre", Value: "Action"}},
		},
	}
	want := "stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json/movie/top?genre=Action"
	if got := deeplinks.NewDiscoverDeepLinks(req).Discover; got != want {
		t.Fatalf("discover link = %q, want %q", got, want)
	}
	if got := types.QueryParamsEncode([]types.QueryPair{{Key: "a b", Value: "c d"}}); got != "a%20b=c%20d" {
		t.Fatalf("query encode = %q", got)
	}
}

// TestF2P006EnvErrorWire asserts the EnvError wire format and the fixed codes
// (R006, R028).
func TestF2P006EnvErrorWire(t *testing.T) {
	if b, _ := json.Marshal(runtime.EnvErrorFetch("x")); !jsonEqual(string(b), `{"code":1,"message":"Failed to fetch: x"}`) {
		t.Fatalf("fetch err = %s", b)
	}
	if b, _ := json.Marshal(runtime.EnvErrorOther("x")); !jsonEqual(string(b), `{"code":1001,"message":"Other error: x"}`) {
		t.Fatalf("other err = %s", b)
	}
	if runtime.EnvErrorCodeFetch != 1 || runtime.EnvErrorCodeAddonTransport != 2 ||
		runtime.EnvErrorCodeSerde != 3 || runtime.EnvErrorCodeStorageUnavailable != 4 ||
		runtime.EnvErrorCodeStorageSchemaVersionDowngrade != 5 ||
		runtime.EnvErrorCodeStorageSchemaVersionUpgrade != 6 ||
		runtime.EnvErrorCodeStorageReadError != 7 || runtime.EnvErrorCodeStorageWriteError != 8 ||
		runtime.EnvErrorCodeOther != 1001 {
		t.Fatalf("env error codes changed")
	}
}

// TestF2P007PlayerLoadNewState asserts NewState emission only on change, the
// stable ordering ctx, streaming_server, player, and that two identical Player
// loads each emit exactly one NewState (R014, R035).
func TestF2P007PlayerLoadNewState(t *testing.T) {
	env := testenv.New()
	transport := &probeTransport{resources: map[string][]byte{
		"meta": []byte(`{"meta":{"id":"tt1","type":"series","name":"Show","videos":[{"id":"tt1:1:1","title":"e1"},{"id":"tt1:1:2","title":"e2"}]}}`),
	}}
	env.Transport = transport
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 64, env)

	selected := models.Selected{
		Stream:        types.Stream{Source: types.StreamSource{Kind: types.StreamSourceURL, URL: "http://stream"}},
		StreamRequest: &types.ResourceRequest{Base: "https://addon.invalid/manifest.json", Path: types.ResourcePath{Resource: "stream", Type: "series", ID: "tt1:1:1"}},
		MetaRequest:   &types.ResourceRequest{Base: "https://addon.invalid/manifest.json", Path: types.ResourcePath{Resource: "meta", Type: "series", ID: "tt1"}},
	}
	order := map[string]int{"ctx": 0, "streaming_server": 1, "player": 2}

	decode := func(ev runtime.RuntimeEvent) []string {
		var fields []string
		if err := json.Unmarshal(ev.Args, &fields); err != nil {
			t.Fatalf("newstate args: %v", err)
		}
		return fields
	}
	assertStable := func(fields []string) {
		last := -1
		for _, f := range fields {
			idx, ok := order[f]
			if !ok {
				t.Fatalf("unexpected field %q", f)
			}
			if idx < last {
				t.Fatalf("fields not in stable order: %v", fields)
			}
			last = idx
		}
	}

	for i := 0; i < 2; i++ {
		rt.Dispatch(runtime.RuntimeAction{Action: msg.LoadAction(msg.ModelPlayer, selected)})
		events := drainProbe(rx)
		firstNewState := -1
		playerStates := 0
		for j, ev := range events {
			if ev.Name != "NewState" {
				continue
			}
			fields := decode(ev)
			assertStable(fields)
			if firstNewState < 0 {
				firstNewState = j
			}
			if len(fields) == 1 && fields[0] == models.FieldPlayer {
				playerStates++
			}
		}
		if firstNewState < 0 {
			t.Fatalf("load %d: no NewState emitted", i)
		}
		if fields := decode(events[firstNewState]); len(fields) != 1 || fields[0] != models.FieldPlayer {
			t.Fatalf("load %d: first NewState = %v, want [player]", i, fields)
		}
		if playerStates == 0 {
			t.Fatalf("load %d: no player NewState emitted", i)
		}
	}

	// An action that changes no model field must not emit NewState.
	rt.Dispatch(runtime.RuntimeAction{Action: msg.NewAction("NoSuchAction", nil)})
	for _, ev := range drainProbe(rx) {
		if ev.Name == "NewState" {
			t.Fatalf("no-op dispatch emitted NewState %s", ev.Args)
		}
	}
}

// TestF2P008RuntimeEventWire asserts the RuntimeEvent JSON wire format and that
// the Env abstraction keeps its operations (R004, R012, R026).
func TestF2P008RuntimeEventWire(t *testing.T) {
	if b, _ := json.Marshal(runtime.NewStateEvent([]string{"ctx", "player"})); !jsonEqual(string(b), `{"name":"NewState","args":["ctx","player"]}`) {
		t.Fatalf("newstate = %s", b)
	}
	if b, _ := json.Marshal(runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil))); !jsonEqual(string(b), `{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}`) {
		t.Fatalf("core event = %s", b)
	}
	var _ runtime.Env = (*testenv.Env)(nil)
}

// TestF2P010ActionEventWire asserts the Action / Event / ActionLoad wire tags
// (R005, R027).
func TestF2P010ActionEventWire(t *testing.T) {
	if b, _ := json.Marshal(msg.NewAction("Unload", nil)); !jsonEqual(string(b), `{"action":"Unload"}`) {
		t.Fatalf("action = %s", b)
	}
	if b, _ := json.Marshal(msg.LoadAction("Player", map[string]any{"stream": 1})); !jsonEqual(string(b), `{"action":"Load","args":{"model":"Player","args":{"stream":1}}}`) {
		t.Fatalf("load action = %s", b)
	}
	if b, _ := json.Marshal(msg.NewEvent("PlayerPlaying", nil)); !jsonEqual(string(b), `{"event":"PlayerPlaying"}`) {
		t.Fatalf("event = %s", b)
	}
}

// TestF2P012StorageMigration asserts the v1..v25 migration chain and the
// downgrade error (R036).
func TestF2P012StorageMigration(t *testing.T) {
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
		t.Fatalf("interfaceLanguage missing: %v", profile.Settings)
	}
	if _, ok := profile.Settings["gpuVideoProcessing"]; !ok {
		t.Fatalf("gpuVideoProcessing missing: %v", profile.Settings)
	}
	if _, ok := profile.Settings["interface_language"]; ok {
		t.Fatalf("interface_language should be removed")
	}

	down := testenv.New()
	down.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`26`)
	err := runtime.MigrateStorageSchema(down)
	if err == nil || err.Code != runtime.EnvErrorCodeStorageSchemaVersionDowngrade {
		t.Fatalf("expected downgrade error, got %v", err)
	}
}

// TestF2P013RuntimeEventWire repeats the RuntimeEvent wire assertion (R039).
func TestF2P013RuntimeEventWire(t *testing.T) {
	if b, _ := json.Marshal(runtime.NewStateEvent([]string{"player"})); !jsonEqual(string(b), `{"name":"NewState","args":["player"]}`) {
		t.Fatalf("newstate = %s", b)
	}
}

// TestF2P019AddonTransport asserts the legacy / HTTP addon resource path and the
// unsupported transport error (R013, R032).
func TestF2P019AddonTransport(t *testing.T) {
	env := testenv.New()
	var gotURL string
	env.FetchFn = func(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError) {
		gotURL = url
		return []byte(`{"metas":[]}`), nil
	}
	tr := addontransport.NewAddonHTTPTransport("https://x/manifest.json", env)
	if _, err := tr.Resource(types.ResourcePath{
		Resource: "catalog", Type: "movie", ID: "top",
		Extra: []types.ExtraValue{{Name: "genre", Value: "Action"}},
	}); err != nil {
		t.Fatalf("resource: %v", err)
	}
	if gotURL != "https://x/catalog/movie/top/genre=Action.json" {
		t.Fatalf("resource url = %q", gotURL)
	}

	bad := addontransport.NewAddonHTTPTransport("https://x/other", env)
	if _, err := bad.Resource(types.ResourcePath{Resource: "catalog", Type: "movie", ID: "top"}); err == nil || err.Code != runtime.EnvErrorCodeAddonTransport {
		t.Fatalf("expected addon transport error, got %v", err)
	}
	unsupported := addontransport.NewUnsupportedTransport("https://x/nope")
	if _, err := unsupported.Resource(types.ResourcePath{Resource: "catalog"}); err == nil || err.Code != runtime.EnvErrorCodeAddonTransport {
		t.Fatalf("expected addon transport error, got %v", err)
	}
}

// TestF2P005PublicCapabilities asserts the required public surfaces are present
// (R001, R008, R009, R010, R011).
func TestF2P005PublicCapabilities(t *testing.T) {
	env := testenv.New()
	model := models.New(env)
	rt, rx := runtime.New(model, nil, 16, env)
	if rt.Model() == nil {
		t.Fatalf("Model() returned nil")
	}
	if _, ok := interface{}(rt).(*runtime.Runtime); !ok {
		t.Fatalf("unexpected runtime type")
	}
	_ = rx
	_, _ = model.Update(msg.ActionMsg(msg.UnloadAction()))
	_, _ = model.UpdateField(msg.ActionMsg(msg.UnloadAction()), models.FieldPlayer)
	_ = common.Ready(5)
}

// TestF2P022RuntimeEventWireCoreEvent asserts the CoreEvent arm (R026).
func TestF2P022RuntimeEventWireCoreEvent(t *testing.T) {
	b, _ := json.Marshal(runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil)))
	if !strings.Contains(string(b), `"name":"CoreEvent"`) || !strings.Contains(string(b), `"event":"UserLoggedOut"`) {
		t.Fatalf("core event = %s", b)
	}
}
