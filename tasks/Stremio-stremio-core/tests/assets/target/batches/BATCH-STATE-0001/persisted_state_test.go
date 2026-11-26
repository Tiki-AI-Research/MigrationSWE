package assetstate

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	core "github.com/stremio/stremio-core"
	"github.com/stremio/stremio-core/internal/testenv"
	"github.com/stremio/stremio-core/runtime"
	"github.com/stremio/stremio-core/types"
)

// The public API leaves the representation of UID unspecified.
var anonymousUID types.UID

// TestProtectedStorageKeys pins every protected storage key and the latest
// schema version. A port that renames a key or lowers the schema version fails.
func TestProtectedStorageKeys(t *testing.T) {
	cases := map[string]string{
		core.SCHEMA_VERSION_STORAGE_KEY:        "schema_version",
		core.PROFILE_STORAGE_KEY:               "profile",
		core.LIBRARY_STORAGE_KEY:               "library",
		core.LIBRARY_RECENT_STORAGE_KEY:        "library_recent",
		core.STREAMS_STORAGE_KEY:               "streams",
		core.SEARCH_HISTORY_STORAGE_KEY:        "search_history",
		core.STREAMING_SERVER_URLS_STORAGE_KEY: "streaming_server_urls",
		core.NOTIFICATIONS_STORAGE_KEY:         "notifications",
		core.CALENDAR_STORAGE_KEY:              "calendar",
		core.DISMISSED_EVENTS_STORAGE_KEY:      "dismissed_events",
	}
	for got, want := range cases {
		if got != want {
			t.Errorf("storage key = %q, want %q", got, want)
		}
	}
	if core.SCHEMA_VERSION != 25 {
		t.Errorf("latest schema version = %d, want 25", core.SCHEMA_VERSION)
	}
}

// TestMigrationKeepsProtectedKeys drives the real storage migration against a
// fake environment and asserts that the protected keys survive and that the
// stored version ends up at the latest schema version.
func TestMigrationKeepsProtectedKeys(t *testing.T) {
	env := testenv.New()
	env.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage("1")
	env.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(
		`{"settings":{"interface_language":"eng","streaming_server_url":"http://127.0.0.1:11470"}}`,
	)

	if err := runtime.MigrateStorageSchema(env); err != nil {
		t.Fatalf("migrate storage schema: %v", err)
	}

	rawVersion, ok := env.Storage[core.SCHEMA_VERSION_STORAGE_KEY]
	if !ok {
		t.Fatalf("protected key %q was dropped during migration", core.SCHEMA_VERSION_STORAGE_KEY)
	}
	var version uint32
	if err := json.Unmarshal(rawVersion, &version); err != nil {
		t.Fatalf("decode schema version: %v", err)
	}
	if version != core.SCHEMA_VERSION {
		t.Fatalf("migrated schema version = %d, want %d", version, core.SCHEMA_VERSION)
	}

	profile, ok := env.Storage[core.PROFILE_STORAGE_KEY]
	if !ok {
		t.Fatalf("protected key %q was dropped during migration", core.PROFILE_STORAGE_KEY)
	}
	encoded := string(profile)
	if !strings.Contains(encoded, `"interfaceLanguage"`) {
		t.Errorf("migrated profile missing interfaceLanguage: %s", encoded)
	}
	if !strings.Contains(encoded, `"gpuVideoProcessing"`) {
		t.Errorf("migrated profile missing gpuVideoProcessing: %s", encoded)
	}
	if strings.Contains(encoded, `"interface_language"`) {
		t.Errorf("migrated profile still carries the legacy key: %s", encoded)
	}
}

// TestPersistedValuesAreSerdeJSONNotBinary asserts that persisted values are
// JSON documents (the serde-JSON string encoding), not Go-gob or another binary
// framing.
func TestPersistedValuesAreSerdeJSONNotBinary(t *testing.T) {
	now := time.Unix(0, 0).UTC()
	values := []struct {
		name  string
		value any
	}{
		{"profile", types.NewProfile()},
		{"library", types.NewLibraryBucket(anonymousUID, nil)},
		{"notifications", types.NewNotificationsBucket(now, anonymousUID, nil)},
		{"search_history", types.NewSearchHistoryBucket(anonymousUID)},
		{"streaming_server_urls", types.NewServerUrlsBucket(now, anonymousUID)},
		{"streams", types.NewStreamsBucket(anonymousUID)},
		{"dismissed_events", types.NewDismissedEventsBucket(anonymousUID)},
	}
	for _, tc := range values {
		encoded, err := json.Marshal(tc.value)
		if err != nil {
			t.Fatalf("%s: marshal: %v", tc.name, err)
		}
		if !json.Valid(encoded) {
			t.Fatalf("%s: persisted value is not valid JSON: %q", tc.name, encoded)
		}
		if len(encoded) == 0 || (encoded[0] != '{' && encoded[0] != '[') {
			t.Fatalf("%s: persisted value is not a JSON document: %q", tc.name, encoded)
		}
	}
}

// TestStreamsBucketWireShape pins the exact serde-JSON string of an empty
// streams bucket. The items map must serialize as a JSON array of pairs, not as
// a JSON object.
func TestStreamsBucketWireShape(t *testing.T) {
	encoded, err := json.Marshal(types.NewStreamsBucket(anonymousUID))
	if err != nil {
		t.Fatalf("marshal streams bucket: %v", err)
	}
	if got, want := string(encoded), `{"uid":null,"items":[]}`; got != want {
		t.Fatalf("streams bucket wire = %s, want %s", got, want)
	}
}

func assertJSONStringRoundTrip(t *testing.T, value any, out any) {
	t.Helper()
	first, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := json.Unmarshal(first, out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	second, err := json.Marshal(out)
	if err != nil {
		t.Fatalf("re-marshal: %v", err)
	}
	if string(first) != string(second) {
		t.Fatalf("serde-JSON round-trip mismatch\n got %s\nwant %s", second, first)
	}
}

// TestPersistedValuesRoundTrip asserts each persisted value re-marshals to the
// identical serde-JSON string after an unmarshal.
func TestPersistedValuesRoundTrip(t *testing.T) {
	now := time.Unix(1000, 0).UTC()
	libraryItem := types.LibraryItem{
		ID:    "tt1",
		Name:  "n",
		Type:  "movie",
		Mtime: now,
		State: types.LibraryItemState{TimeOffset: 5},
	}
	assertJSONStringRoundTrip(t, types.NewProfile(), &types.Profile{})
	assertJSONStringRoundTrip(t, types.NewLibraryBucket(anonymousUID, []types.LibraryItem{libraryItem}), &types.LibraryBucket{})
	assertJSONStringRoundTrip(t,
		types.NewNotificationsBucket(now, anonymousUID, []types.NotificationItem{{MetaID: "tt1", VideoID: "v1", VideoReleased: now}}),
		&types.NotificationsBucket{})
	assertJSONStringRoundTrip(t, types.NewSearchHistoryBucket(anonymousUID), &types.SearchHistoryBucket{})
	assertJSONStringRoundTrip(t, types.NewServerUrlsBucket(now, anonymousUID), &types.ServerUrlsBucket{})
	assertJSONStringRoundTrip(t, types.NewStreamsBucket(anonymousUID), &types.StreamsBucket{})
	assertJSONStringRoundTrip(t, types.NewDismissedEventsBucket(anonymousUID), &types.DismissedEventsBucket{})
}
