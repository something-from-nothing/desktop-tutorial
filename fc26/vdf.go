package main

import (
	"errors"
	"strings"
)

// A small reader for Valve's text KeyValues format (.vdf and .acf files), enough for
// localconfig.vdf, libraryfolders.vdf and appmanifest_*.acf. Keys are compared without case,
// the way Steam does.

type vdfNode struct {
	Value    string
	Children []vdfPair
}

type vdfPair struct {
	Key  string
	Node *vdfNode
}

func (n *vdfNode) Get(key string) *vdfNode {
	if n == nil {
		return nil
	}
	for i := len(n.Children) - 1; i >= 0; i-- {
		if strings.EqualFold(n.Children[i].Key, key) {
			return n.Children[i].Node
		}
	}
	return nil
}

// Path walks down several keys; it returns nil if any step is missing.
func (n *vdfNode) Path(keys ...string) *vdfNode {
	for _, k := range keys {
		n = n.Get(k)
		if n == nil {
			return nil
		}
	}
	return n
}

func (n *vdfNode) Str(key string) string {
	if c := n.Get(key); c != nil {
		return c.Value
	}
	return ""
}

func parseVDF(src string) (*vdfNode, error) {
	p := &vdfParser{s: strings.TrimPrefix(src, "\xef\xbb\xbf")}
	root := &vdfNode{}
	if err := p.block(root, true); err != nil {
		return nil, err
	}
	return root, nil
}

type vdfParser struct {
	s string
	i int
}

func (p *vdfParser) skip() {
	for p.i < len(p.s) {
		c := p.s[p.i]
		switch {
		case c == ' ' || c == '\t' || c == '\r' || c == '\n':
			p.i++
		case c == '/' && p.i+1 < len(p.s) && p.s[p.i+1] == '/':
			for p.i < len(p.s) && p.s[p.i] != '\n' {
				p.i++
			}
		case c == '[':
			// Conditional like [$WIN32]: ignore it.
			for p.i < len(p.s) && p.s[p.i] != ']' && p.s[p.i] != '\n' {
				p.i++
			}
			if p.i < len(p.s) && p.s[p.i] == ']' {
				p.i++
			}
		default:
			return
		}
	}
}

func (p *vdfParser) token() (string, bool, error) {
	p.skip()
	if p.i >= len(p.s) {
		return "", false, nil
	}
	if p.s[p.i] == '"' {
		p.i++
		var b strings.Builder
		for p.i < len(p.s) {
			c := p.s[p.i]
			if c == '\\' && p.i+1 < len(p.s) {
				n := p.s[p.i+1]
				switch n {
				case 'n':
					b.WriteByte('\n')
				case 't':
					b.WriteByte('\t')
				case '\\', '"':
					b.WriteByte(n)
				default:
					b.WriteByte('\\')
					b.WriteByte(n)
				}
				p.i += 2
				continue
			}
			if c == '"' {
				p.i++
				return b.String(), true, nil
			}
			b.WriteByte(c)
			p.i++
		}
		return "", false, errors.New("vdf: unterminated string")
	}
	start := p.i
	for p.i < len(p.s) {
		c := p.s[p.i]
		if c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '{' || c == '}' || c == '"' {
			break
		}
		p.i++
	}
	return p.s[start:p.i], true, nil
}

func (p *vdfParser) block(n *vdfNode, top bool) error {
	for {
		p.skip()
		if p.i >= len(p.s) {
			if top {
				return nil
			}
			return errors.New("vdf: missing }")
		}
		if p.s[p.i] == '}' {
			p.i++
			if top {
				return errors.New("vdf: unexpected }")
			}
			return nil
		}
		key, ok, err := p.token()
		if err != nil {
			return err
		}
		if !ok {
			return nil
		}
		p.skip()
		if p.i < len(p.s) && p.s[p.i] == '{' {
			p.i++
			child := &vdfNode{}
			if err := p.block(child, false); err != nil {
				return err
			}
			n.Children = append(n.Children, vdfPair{key, child})
			continue
		}
		val, ok, err := p.token()
		if err != nil {
			return err
		}
		if !ok {
			return errors.New("vdf: key without value: " + key)
		}
		n.Children = append(n.Children, vdfPair{key, &vdfNode{Value: val}})
	}
}
