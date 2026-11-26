// Command sqlq is a tiny read/exec helper used by the BATCH-STATE-0001 target
// probes to inspect the SQLite database produced by `ft index`. It is kept
// self-contained (only depending on the same modernc.org/sqlite driver as the
// Go target) so the probes never need a system sqlite3 binary.
package main

import (
	"database/sql"
	"fmt"
	"os"
	"strings"

	_ "modernc.org/sqlite"
)

func main() {
	if len(os.Args) < 4 {
		fmt.Fprintln(os.Stderr, "usage: sqlq <db> query|exec <sql>")
		os.Exit(2)
	}
	dbPath, mode, sqlText := os.Args[1], os.Args[2], os.Args[3]
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer db.Close()

	if mode == "exec" {
		for _, stmt := range strings.Split(sqlText, ";") {
			if strings.TrimSpace(stmt) == "" {
				continue
			}
			if _, err := db.Exec(stmt); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
		}
		return
	}

	rows, err := db.Query(sqlText)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer rows.Close()
	cols, _ := rows.Columns()
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		out := make([]string, len(cols))
		for i, v := range vals {
			switch t := v.(type) {
			case nil:
				out[i] = "NULL"
			case []byte:
				out[i] = string(t)
			default:
				out[i] = fmt.Sprintf("%v", t)
			}
		}
		fmt.Println(strings.Join(out, "\t"))
	}
}
