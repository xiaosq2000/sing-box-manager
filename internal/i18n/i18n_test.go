package i18n

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

func TestChineseFollowsSBCLangThenTheLocale(t *testing.T) {
	cases := []struct {
		env  map[string]string
		want bool
	}{
		{map[string]string{"LANG": "zh_CN.UTF-8"}, true},
		{map[string]string{"LANG": "zh_SG.UTF-8"}, true},
		{map[string]string{"LANG": "zh_TW.UTF-8"}, false},
		{map[string]string{"LANG": "en_US.UTF-8"}, false},
		{map[string]string{}, false},
		{map[string]string{"LC_ALL": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"}, false},
		{map[string]string{"LC_MESSAGES": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}, true},
		{map[string]string{"LC_ALL": "", "LANG": "zh_CN.UTF-8"}, true},
		{map[string]string{"SBC_LANG": "en", "LANG": "zh_CN.UTF-8"}, false},
		{map[string]string{"SBC_LANG": "zh", "LANG": "C"}, true},
	}
	for _, c := range cases {
		if got := Chinese(func(name string) string { return c.env[name] }); got != c.want {
			t.Errorf("%v: got %v", c.env, got)
		}
	}
}

func TestAMessageWithoutATranslationStaysInEnglish(t *testing.T) {
	SetChinese(true)
	defer SetChinese(false)
	if T("no such message") != "no such message" {
		t.Error("an unknown message changed")
	}
	if T("Nothing was changed.") == "Nothing was changed." {
		t.Error("a known message stayed in English")
	}
	if err := Errorf("port %d is in use", 1080); !strings.Contains(err.Error(), "1080") {
		t.Errorf("got %v", err)
	}
}

// translatable gives, for each call that takes a message, which argument it
// is: this package's helpers, and the CLI's prompt, which passes its text to T.
var translatable = map[string]int{"i18n.T": 0, "i18n.New": 0, "i18n.Errorf": 0, "prompt": 1}

// messages finds every message sbc's source passes to a translatable call.
func messages(t *testing.T) map[string]string {
	t.Helper()
	found := map[string]string{}
	fileSet := token.NewFileSet()
	for _, dir := range []string{"../../cmd", "../../internal"} {
		err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
			if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
				return err
			}
			file, err := parser.ParseFile(fileSet, path, nil, 0)
			if err != nil {
				return err
			}
			ast.Inspect(file, func(node ast.Node) bool {
				call, ok := node.(*ast.CallExpr)
				if !ok {
					return true
				}
				index, ok := translatable[name(call.Fun)]
				if !ok || len(call.Args) <= index {
					return true
				}
				if message, ok := literal(call.Args[index]); ok {
					found[message] = fileSet.Position(call.Pos()).String()
				}
				return true
			})
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	return found
}

func name(fun ast.Expr) string {
	switch f := fun.(type) {
	case *ast.Ident:
		return f.Name
	case *ast.SelectorExpr:
		if pkg, ok := f.X.(*ast.Ident); ok {
			return pkg.Name + "." + f.Sel.Name
		}
	}
	return ""
}

// literal reads a string literal, or a constant declared with one, such as the
// CLI's usage text.
func literal(expr ast.Expr) (string, bool) {
	if ident, ok := expr.(*ast.Ident); ok && ident.Obj != nil {
		if spec, ok := ident.Obj.Decl.(*ast.ValueSpec); ok && len(spec.Values) == 1 {
			expr = spec.Values[0]
		}
	}
	lit, ok := expr.(*ast.BasicLit)
	if !ok || lit.Kind != token.STRING {
		return "", false
	}
	value, err := strconv.Unquote(lit.Value)
	return value, err == nil
}

var verb = regexp.MustCompile(`%[-+# 0]*[0-9]*(\.[0-9]+)?[a-zA-Z%]`)

func TestEveryMessageHasATranslationWithTheSameValues(t *testing.T) {
	found := messages(t)
	var missing []string
	for message, where := range found {
		translated, ok := zh[message]
		if !ok {
			missing = append(missing, where+": "+strconv.Quote(message))
			continue
		}
		english := strings.Join(verb.FindAllString(message, -1), " ")
		chinese := strings.Join(verb.FindAllString(translated, -1), " ")
		if english != chinese {
			t.Errorf("%s: %q formats %q, but its translation formats %q", where, message, english, chinese)
		}
	}
	sort.Strings(missing)
	for _, line := range missing {
		t.Errorf("no translation: %s", line)
	}
	for message := range zh {
		if _, ok := found[message]; !ok {
			t.Errorf("translation of a message sbc no longer has: %q", message)
		}
	}
}
