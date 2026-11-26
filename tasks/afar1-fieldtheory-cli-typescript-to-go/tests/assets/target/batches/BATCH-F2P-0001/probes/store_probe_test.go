package store

// F2P target-side probe overlay for BATCH-F2P-0001.
//
// Copied into internal/store/ of a writable copy of the Go target and executed
// with `go test`. It asserts observable schema/index/query behavior against a
// real SQLite database built by the target, using helper names distinct from
// the repository's own schema_test.go.

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"fieldtheory/internal/model"
)

func f2pStr(s string) *string { return &s }

func f2pWrite(t *testing.T, dir string, records []model.BookmarkRecord) {
	t.Helper()
	var sb strings.Builder
	for _, r := range records {
		b, err := json.Marshal(r)
		if err != nil {
			t.Fatal(err)
		}
		sb.Write(b)
		sb.WriteByte('\n')
	}
	if err := os.WriteFile(filepath.Join(dir, "bookmarks.jsonl"), []byte(sb.String()), 0o600); err != nil {
		t.Fatal(err)
	}
}

func f2pRec(id, text, author, posted string) model.BookmarkRecord {
	return model.BookmarkRecord{
		ID:           id,
		TweetID:      "10" + id,
		URL:          "https://x.com/" + author + "/status/" + id,
		Text:         text,
		AuthorHandle: author,
		AuthorName:   author,
		PostedAt:     f2pStr(posted),
		SyncedAt:     "2024-02-01T00:00:00.000Z",
		Language:     "en",
	}
}

func f2pColumns(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.Query("PRAGMA table_info(bookmarks)")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		t.Fatal(err)
	}
	nameIdx := -1
	for i, c := range cols {
		if c == "name" {
			nameIdx = i
		}
	}
	if nameIdx < 0 {
		t.Fatal("PRAGMA table_info has no name column")
	}
	names := []string{}
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			t.Fatal(err)
		}
		switch v := vals[nameIdx].(type) {
		case string:
			names = append(names, v)
		case []byte:
			names = append(names, string(v))
		}
	}
	return names
}

func f2pFTSSQL(t *testing.T, db *sql.DB) string {
	t.Helper()
	var s sql.NullString
	if err := db.QueryRow("SELECT sql FROM sqlite_master WHERE name='bookmarks_fts'").Scan(&s); err != nil {
		t.Fatalf("read bookmarks_fts sql: %v", err)
	}
	if !s.Valid {
		t.Fatal("bookmarks_fts virtual table not found")
	}
	return s.String
}

// F2P-016 / F2P-014: the index exposes exactly the 37 documented columns in
// order, declares the content-linked FTS5 table with the FTS5 tokenizer, and
// ranks search results by ascending bm25(bookmarks_fts, 5.0, 1.0, 1.0, 3.0).
func TestF2P016SchemaFTSAndBm25Order(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("FT_DATA_DIR", dir)
	f2pWrite(t, dir, []model.BookmarkRecord{
		f2pRec("1", "machine learning machine learning machine learning", "alice", "2024-01-02T10:00:00.000Z"),
		f2pRec("2", "machine learning tutorial", "bob", "2024-01-03T10:00:00.000Z"),
	})

	dbPath, total, _, err := BuildIndex(false)
	if err != nil {
		t.Fatalf("BuildIndex: %v", err)
	}
	if total != 2 {
		t.Fatalf("expected 2 indexed records, got %d", total)
	}
	db, err := OpenDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	got := f2pColumns(t, db)
	if len(got) != len(ColumnNames) {
		t.Fatalf("expected %d columns, got %d: %v", len(ColumnNames), len(got), got)
	}
	for i := range ColumnNames {
		if got[i] != ColumnNames[i] {
			t.Fatalf("column %d mismatch: got %q want %q", i, got[i], ColumnNames[i])
		}
	}

	fts := f2pFTSSQL(t, db)
	for _, needle := range []string{"content=bookmarks", "content_rowid=rowid", "tokenize='porter unicode61'", "article_text"} {
		if !strings.Contains(fts, needle) {
			t.Fatalf("bookmarks_fts declaration missing %q: %s", needle, fts)
		}
	}

	var version string
	if err := db.QueryRow("SELECT value FROM meta WHERE key='schema_version'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != "6" {
		t.Fatalf("expected meta.schema_version=6, got %q", version)
	}

	results, err := SearchBookmarks(SearchOptions{Query: "machine learning", Limit: 20})
	if err != nil {
		t.Fatalf("SearchBookmarks: %v", err)
	}
	if len(results) < 2 {
		t.Fatalf("expected at least 2 search results, got %d", len(results))
	}
	if results[0].ID != "1" || results[1].ID != "2" {
		t.Fatalf("expected bm25 order [1 2], got [%s %s]", results[0].ID, results[1].ID)
	}
}

// F2P-019: FTS5 operators are sanitized into literal terms and an empty result
// set formats as "No results found.".
func TestF2P019SearchSanitizerAndEmpty(t *testing.T) {
	if got := SanitizeFTSQuery("machine -learning"); got != `"machine" "-learning"` {
		t.Fatalf("sanitize mismatch: got %q", got)
	}
	if got := FormatSearchResults(nil); got != "No results found." {
		t.Fatalf("empty formatting mismatch: got %q", got)
	}
}

// F2P-007 / F2P-019 / F2P-020 / F2P-023: refreshing an existing index that
// lacks the domains and article_text columns self-heals by inspecting column
// existence, preserves a previously stored primary_category, and resets it to
// "unclassified" only after --force.
func TestF2P043IndexRefreshAndSelfHealing(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("FT_DATA_DIR", dir)
	f2pWrite(t, dir, []model.BookmarkRecord{
		f2pRec("1", "machine learning", "alice", "2024-01-02T10:00:00.000Z"),
	})

	dbPath := filepath.Join(dir, "bookmarks.db")
	db, err := OpenDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}

	legacy := `CREATE TABLE bookmarks (
    id TEXT PRIMARY KEY, tweet_id TEXT NOT NULL, url TEXT NOT NULL, text TEXT NOT NULL,
    author_handle TEXT, author_name TEXT, author_profile_image_url TEXT, posted_at TEXT,
    bookmarked_at TEXT, synced_at TEXT NOT NULL, conversation_id TEXT, in_reply_to_status_id TEXT,
    quoted_status_id TEXT, language TEXT, like_count INTEGER, repost_count INTEGER, reply_count INTEGER,
    quote_count INTEGER, bookmark_count INTEGER, view_count INTEGER, media_count INTEGER DEFAULT 0,
    link_count INTEGER DEFAULT 0, links_json TEXT, tags_json TEXT, ingested_via TEXT, categories TEXT,
    primary_category TEXT, github_urls TEXT, primary_domain TEXT, quoted_tweet_json TEXT, article_title TEXT,
    article_site TEXT, enriched_at TEXT, folder_ids TEXT, folder_names TEXT
  )`
	if _, err := db.Exec(legacy); err != nil {
		t.Fatalf("create legacy table: %v", err)
	}
	if _, err := db.Exec(`CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO meta VALUES ('schema_version','5')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE VIRTUAL TABLE bookmarks_fts USING fts5(text, author_handle, author_name, content=bookmarks, content_rowid=rowid, tokenize='porter unicode61')`); err != nil {
		t.Fatalf("create legacy fts: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO bookmarks (id, tweet_id, url, text, synced_at, primary_category) VALUES ('1','101','https://x.com/a/status/1','machine learning','2024-02-01T00:00:00.000Z','tool')`); err != nil {
		t.Fatal(err)
	}
	db.Close()

	if _, _, _, err := BuildIndex(false); err != nil {
		t.Fatalf("refresh BuildIndex: %v", err)
	}
	db, err = OpenDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	cols := f2pColumns(t, db)
	for _, needle := range []string{"domains", "primary_domain", "article_text"} {
		found := false
		for _, c := range cols {
			if c == needle {
				found = true
			}
		}
		if !found {
			t.Fatalf("expected self-healing migration to add column %q; columns=%v", needle, cols)
		}
	}
	var pc string
	if err := db.QueryRow("SELECT primary_category FROM bookmarks WHERE id='1'").Scan(&pc); err != nil {
		t.Fatal(err)
	}
	if pc != "tool" {
		t.Fatalf("expected primary_category preserved as %q on refresh, got %q", "tool", pc)
	}
	fts := f2pFTSSQL(t, db)
	if !strings.Contains(fts, "article_text") {
		t.Fatalf("expected FTS rebuilt with article_text column: %s", fts)
	}
	db.Close()

	if _, _, _, err := BuildIndex(true); err != nil {
		t.Fatalf("force BuildIndex: %v", err)
	}
	db, err = OpenDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.QueryRow("SELECT primary_category FROM bookmarks WHERE id='1'").Scan(&pc); err != nil {
		t.Fatal(err)
	}
	if pc != "unclassified" {
		t.Fatalf("expected primary_category reset to %q after --force, got %q", "unclassified", pc)
	}
}

// F2P-020: aggregate stats report totals, unique authors, the posted_at date
// range, top authors and the language breakdown.
func TestF2P020StatsContract(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("FT_DATA_DIR", dir)
	recs := []model.BookmarkRecord{
		f2pRec("1", "one", "alice", "2024-01-02T10:00:00.000Z"),
		f2pRec("2", "two", "bob", "2024-01-03T10:00:00Z"),
	}
	recs[1].Language = "fr"
	f2pWrite(t, dir, recs)
	if _, _, _, err := BuildIndex(false); err != nil {
		t.Fatal(err)
	}
	stats, err := GetStats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.TotalBookmarks != 2 {
		t.Fatalf("expected 2 total bookmarks, got %d", stats.TotalBookmarks)
	}
	if stats.UniqueAuthors != 2 {
		t.Fatalf("expected 2 unique authors, got %d", stats.UniqueAuthors)
	}
	if stats.DateRange.Earliest == nil || *stats.DateRange.Earliest != "2024-01-02" {
		t.Fatalf("unexpected earliest date: %v", stats.DateRange.Earliest)
	}
	if stats.DateRange.Latest == nil || *stats.DateRange.Latest != "2024-01-03" {
		t.Fatalf("unexpected latest date: %v", stats.DateRange.Latest)
	}
	if len(stats.LanguageBreakdown) == 0 {
		t.Fatal("expected a language breakdown")
	}
}

// F2P-014: folder counts report per-folder totals plus an untagged count, and
// category/domain distributions are sorted by count.
func TestF2P014FoldersAndDistributions(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("FT_DATA_DIR", dir)
	r1 := f2pRec("1", "one", "alice", "2024-01-02T10:00:00.000Z")
	r1.FolderIDs = []string{"f1"}
	r1.FolderNames = []string{"Foo"}
	r2 := f2pRec("2", "two", "bob", "2024-01-03T10:00:00.000Z")
	r2.FolderIDs = []string{"f1"}
	r2.FolderNames = []string{"Foo"}
	f2pWrite(t, dir, []model.BookmarkRecord{r1, r2})

	dbPath, _, _, err := BuildIndex(false)
	if err != nil {
		t.Fatal(err)
	}
	db, err := OpenDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE bookmarks SET primary_category='tool' WHERE id='1'"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE bookmarks SET primary_category='security' WHERE id='2'"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE bookmarks SET domains='ai', primary_domain='ai' WHERE id='1'"); err != nil {
		t.Fatal(err)
	}
	db.Close()

	folders, err := GetFolderCounts()
	if err != nil {
		t.Fatal(err)
	}
	if folders.Counts["Foo"] != 2 {
		t.Fatalf("expected folder Foo count 2, got %v", folders.Counts)
	}
	if folders.Untagged != 0 {
		t.Fatalf("expected 0 untagged, got %d", folders.Untagged)
	}

	cats, err := GetCategoryCounts()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := cats["unclassified"]; ok {
		t.Fatalf("category distribution must exclude the unclassified placeholder: %v", cats)
	}

	domains, err := GetDomainCounts()
	if err != nil {
		t.Fatal(err)
	}
	if domains["ai"] != 1 {
		t.Fatalf("expected domain ai count 1, got %v", domains)
	}
}
