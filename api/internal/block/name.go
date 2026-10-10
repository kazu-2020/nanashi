// Package block has the rules for the blocks (docs/glossary.md).
package block

import (
	"errors"
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

// MaxNameLen is the maximum length of a block name, in code points.
const MaxNameLen = 200

// Name is a block name that ParseName normalized and checked. The uniqueness is not part of it: the engine checks it.
type Name struct{ s string }

// ParseName normalizes and checks a block name that a user gave (docs/spec/dimension_list.qnt). It changes each
// run of control characters to one space, then removes the white space at the start and at the end.
func ParseName(raw string) (Name, error) {
	var b strings.Builder
	ctrl := false
	for _, r := range raw {
		if unicode.IsControl(r) {
			if !ctrl {
				b.WriteRune(' ')
			}
			ctrl = true
			continue
		}
		ctrl = false
		b.WriteRune(r)
	}
	s := strings.TrimSpace(b.String())
	switch {
	case s == "":
		return Name{}, errors.New("名前が要る")
	case utf8.RuneCountInString(s) > MaxNameLen:
		return Name{}, fmt.Errorf("名前は %d 文字以下にする", MaxNameLen)
	}
	return Name{s}, nil
}

func (n Name) String() string { return n.s }
