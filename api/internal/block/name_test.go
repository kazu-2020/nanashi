package block

import (
	"strings"
	"testing"
)

func TestParseName(t *testing.T) {
	long := strings.Repeat("あ", MaxNameLen)
	for _, c := range []struct{ raw, want, err string }{
		{"部門", "部門", ""},
		{" \t部門\r\n", "部門", ""},
		{"営業\r\n部", "営業 部", ""},
		{"営業\t部", "営業 部", ""},
		{"営業  部", "営業  部", ""},
		{"営業\x00\x7f\u0085部", "営業 部", ""},
		{"　部門　", "部門", ""},
		{long, long, ""},
		{" " + long + "\n", long, ""},
		{"", "", "名前が要る"},
		{" \t\r\n　", "", "名前が要る"},
		{long + "あ", "", "200 文字以下"},
	} {
		got, err := ParseName(c.raw)
		if c.err != "" {
			if err == nil || !strings.Contains(err.Error(), c.err) {
				t.Errorf("ParseName(%q): got %q, %v, want an error with %q", c.raw, got, err, c.err)
			}
			continue
		}
		if err != nil || got.String() != c.want {
			t.Errorf("ParseName(%q): got %q, %v, want %q", c.raw, got, err, c.want)
		}
	}
}
