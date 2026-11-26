package assetstate

import (
	"encoding/json"
	"reflect"
	"testing"

	addontransport "github.com/stremio/stremio-core/addon_transport"
	"github.com/stremio/stremio-core/runtime"
	"github.com/stremio/stremio-core/runtime/msg"
	"github.com/stremio/stremio-core/types"
)

// JSON object member order is not part of the message contract. Array
// order, member presence, values and types must still match exactly.
func jsonEqual(got, want string) bool {
	var actual, expected any
	return json.Unmarshal([]byte(got), &actual) == nil &&
		json.Unmarshal([]byte(want), &expected) == nil && reflect.DeepEqual(actual, expected)
}

func mustJSON(t *testing.T, value any) string {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(encoded)
}

// TestRuntimeEventWireFormat pins the RuntimeEvent JSON envelope.
func TestRuntimeEventWireFormat(t *testing.T) {
	cases := []struct {
		name string
		got  any
		want string
	}{
		{
			name: "NewState",
			got:  runtime.NewStateEvent([]string{"ctx", "player"}),
			want: `{"name":"NewState","args":["ctx","player"]}`,
		},
		{
			name: "CoreEvent",
			got:  runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil)),
			want: `{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}`,
		},
	}
	for _, tc := range cases {
		if got := mustJSON(t, tc.got); !jsonEqual(got, tc.want) {
			t.Errorf("%s wire = %s, want %s", tc.name, got, tc.want)
		}
	}
}

// TestActionEventWireFormat pins the Action, ActionLoad and Event JSON tags.
func TestActionEventWireFormat(t *testing.T) {
	cases := []struct {
		name string
		got  any
		want string
	}{
		{
			name: "Unload",
			got:  msg.NewAction("Unload", nil),
			want: `{"action":"Unload"}`,
		},
		{
			name: "LoadPlayer",
			got:  msg.LoadAction("Player", map[string]any{"stream": 1}),
			want: `{"action":"Load","args":{"model":"Player","args":{"stream":1}}}`,
		},
		{
			name: "PlayerPlaying",
			got:  msg.NewEvent("PlayerPlaying", nil),
			want: `{"event":"PlayerPlaying"}`,
		},
	}
	for _, tc := range cases {
		if got := mustJSON(t, tc.got); !jsonEqual(got, tc.want) {
			t.Errorf("%s wire = %s, want %s", tc.name, got, tc.want)
		}
	}
}

// TestEnvErrorWireFormat pins the EnvError JSON envelope and fixed codes.
func TestEnvErrorWireFormat(t *testing.T) {
	cases := []struct {
		name string
		got  *runtime.EnvError
		want string
	}{
		{"fetch", runtime.EnvErrorFetch("x"), `{"code":1,"message":"Failed to fetch: x"}`},
		{"other", runtime.EnvErrorOther("x"), `{"code":1001,"message":"Other error: x"}`},
		{
			"downgrade",
			runtime.EnvErrorStorageSchemaVersionDowngrade(26, 25),
			`{"code":5,"message":"Downgrade storage schema version from 26 to 25 is not allowed"}`,
		},
	}
	for _, tc := range cases {
		if got := mustJSON(t, tc.got); !jsonEqual(got, tc.want) {
			t.Errorf("%s wire = %s, want %s", tc.name, got, tc.want)
		}
	}
}

// TestLegacyJSONRPCWireFormat pins the legacy request URL and the JSON-RPC to
// addon-transport error mapping.
func TestLegacyJSONRPCWireFormat(t *testing.T) {
	got, err := addontransport.BuildLegacyReq(
		"https://legacywatchhub.strem.io/stremio/v1",
		types.ResourcePath{Resource: "stream", Type: "series", ID: "tt0386676:5:1"},
	)
	if err != nil {
		t.Fatalf("BuildLegacyReq: %v", err)
	}
	want := "https://legacywatchhub.strem.io/stremio/v1/q.json?b=eyJpZCI6MSwianNvbnJwYyI6IjIuMCIsIm1ldGhvZCI6InN0cmVhbS5maW5kIiwicGFyYW1zIjpbbnVsbCx7InF1ZXJ5Ijp7ImVwaXNvZGUiOjEsImltZGJfaWQiOiJ0dDAzODY2NzYiLCJzZWFzb24iOjUsInR5cGUiOiJzZXJpZXMifX1dfQ=="
	if got != want {
		t.Fatalf("legacy request mismatch\n got: %s\nwant: %s", got, want)
	}

	mapped := (&addontransport.LegacyErr{Kind: addontransport.LegacyJSONRPC, Code: 5, Message: "boom"}).ToEnvError()
	if mapped.Code != 2 {
		t.Errorf("mapped code = %d, want 2", mapped.Code)
	}
	if mapped.Message != "Addon protocol violation: rpc error 5: boom" {
		t.Errorf("mapped message = %q", mapped.Message)
	}
	if wire := mustJSON(t, mapped); !jsonEqual(wire, `{"code":2,"message":"Addon protocol violation: rpc error 5: boom"}`) {
		t.Errorf("mapped wire = %s", wire)
	}
}
